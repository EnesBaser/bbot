#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Trading Bot v4.020 - Triple Auto Mode (Fixed & Improved)

FIXES vs v4.011:
  - max_positions eklendi MODERATE ve AGGRESSIVE'e
  - set_leverage(1) açıkça çağrılıyor (gerçek 1x)
  - threading.Lock ile thread-safety
  - sync_positions kapanan pozisyonları temizliyor
  - Bakiye kontrolü (yetersizse işlem açılmaz)
  - API rate-limit koruması (throttle)
  - Bare except kaldırıldı, hatalar loglanıyor
  - Pozisyon büyüklüğü: min $30 kontrolü
  - Frontend polling 5 saniyeye düşürüldü
  - Fiyat cache ile /api/status API yükü azaltıldı
  - Çift kapanma koruması
"""

import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from flask import Flask, render_template_string, jsonify, request
from pybit.unified_trading import HTTP
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import ADXIndicator, EMAIndicator
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# =============================================================================
# CONFIGURATION
# =============================================================================

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.info(f"🔑 API Key loaded: '{BYBIT_API_KEY[:8]}...' (len={len(BYBIT_API_KEY)})")

# -- Minimum işlem büyüklüğü (USD) --
MIN_POSITION_USD = 30.0

MODES = {
    "SAFE": {
        "position_size": 35,       # $35 — min $30 kontrolünden geçer
        "max_positions": 1,
        "volume_min": 10_000_000,
        "volatility_min": 2.5,
        "rsi_oversold": 25,
        "rsi_overbought": 75,
        "adx_min": 30,
        "tp_percent": 8.0,
        "sl_percent": -2.0,
        "rescan_interval": 60,
        "enabled": False,
    },
    "MODERATE": {
        "position_size": 45,       # $45
        "max_positions": 2,        # ✅ FIX: eksikti
        "volume_min": 5_000_000,
        "volatility_min": 2.0,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "adx_min": 25,
        "tp_percent": 6.0,
        "sl_percent": -2.5,
        "rescan_interval": 45,
        "enabled": False,
    },
    "AGGRESSIVE": {
        "position_size": 55,       # $55
        "max_positions": 3,        # ✅ FIX: eksikti
        "volume_min": 2_000_000,
        "volatility_min": 1.5,
        "rsi_oversold": 35,
        "rsi_overbought": 65,
        "adx_min": 20,
        "tp_percent": 5.0,
        "sl_percent": -3.0,
        "rescan_interval": 30,
        "enabled": False,
    }
}

TRAILING_LEVELS = [
    {"min": 1.0,  "max": 2.4,  "sl": -2.0},
    {"min": 2.5,  "max": 3.9,  "sl": 0.5},
    {"min": 4.0,  "max": 5.9,  "sl": 2.0},
    {"min": 6.0,  "max": 8.9,  "sl": 3.5},
    {"min": 9.0,  "max": 12.9, "sl": 6.0},
    {"min": 13.0, "max": 99.9, "sl": 9.0},
]

BLACKLIST_CONFIG = {
    "enabled": True,
    "loss_count": 3,
    "loss_amount": -30,
    "duration_hours": 24,
}

# =============================================================================
# THREAD-SAFE STATE
# =============================================================================

_state_lock = threading.Lock()

bot_state = {
    "running": False,
    "balance": 0.0,
    "available": 0.0,
    "positions": {},
    "blacklist": {},
    "loss_tracker": {},
    "last_scan": {},
    "stats": {
        "total_trades": 0,
        "winning_trades": 0,
        "total_pnl": 0.0,
    },
    "daily_pnl": 0.0,
    "daily_loss_limit": -5.0,
    "daily_loss_hit": False,
    "start_balance": 0.0,
    "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
    "trade_history": [],
}

# Fiyat cache — /api/status her saniye API çağırmasın
_price_cache = {}       # symbol -> {"price": float, "time": float}
PRICE_CACHE_TTL = 5     # saniye

app = Flask(__name__)
session_cache = None

# =============================================================================
# BYBIT SESSION
# =============================================================================

def get_session():
    global session_cache
    if session_cache is None:
        session_cache = HTTP(
            testnet=False,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
    return session_cache

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")

# =============================================================================
# BLACKLIST
# =============================================================================

def is_blacklisted(symbol):
    with _state_lock:
        if symbol not in bot_state["blacklist"]:
            return False
        expire = bot_state["blacklist"][symbol]["expire"]
        if datetime.now().timestamp() > expire:
            del bot_state["blacklist"][symbol]
            return False
        return True

def add_blacklist(symbol, reason):
    expire = (datetime.now() + timedelta(hours=BLACKLIST_CONFIG["duration_hours"])).timestamp()
    with _state_lock:
        bot_state["blacklist"][symbol] = {"expire": expire, "reason": reason}
    logging.warning(f"⛔ BLACKLIST: {symbol} - {reason}")
    send_telegram(f"⛔ <b>BLACKLIST</b>\n{symbol}\n{reason}")

def track_loss(symbol, pnl):
    with _state_lock:
        if symbol not in bot_state["loss_tracker"]:
            bot_state["loss_tracker"][symbol] = []
        bot_state["loss_tracker"][symbol].append({"time": datetime.now().timestamp(), "pnl": pnl})
        cutoff = datetime.now().timestamp() - 86400
        bot_state["loss_tracker"][symbol] = [
            l for l in bot_state["loss_tracker"][symbol] if l["time"] > cutoff
        ]
        if BLACKLIST_CONFIG["enabled"]:
            recent = [l for l in bot_state["loss_tracker"][symbol] if l["pnl"] < 0]
            total_loss = sum(l["pnl"] for l in recent)
            if len(recent) >= BLACKLIST_CONFIG["loss_count"]:
                # Release lock before add_blacklist (which acquires it)
                pass
            else:
                total_loss = None  # sentinel
                recent = None

    # Blacklist logic outside lock to avoid deadlock
    # Re-check with simpler approach:
    _do_blacklist_check(symbol)

def _do_blacklist_check(symbol):
    with _state_lock:
        entries = bot_state["loss_tracker"].get(symbol, [])
        recent = [l for l in entries if l["pnl"] < 0]
        total_loss = sum(l["pnl"] for l in recent)

    if BLACKLIST_CONFIG["enabled"]:
        if len(recent) >= BLACKLIST_CONFIG["loss_count"]:
            add_blacklist(symbol, f"{len(recent)} losses in 24h")
        elif total_loss <= BLACKLIST_CONFIG["loss_amount"]:
            add_blacklist(symbol, f"${abs(total_loss):.0f} total loss in 24h")

# =============================================================================
# BALANCE & POSITIONS
# =============================================================================

def sync_positions():
    """Bybit'teki gerçek pozisyonları bot_state ile senkronize et."""
    try:
        session = get_session()
        response = session.get_positions(category="linear", settleCoin="USDT")
        if response["retCode"] != 0:
            logging.error(f"Position sync failed: {response.get('retMsg', 'unknown')}")
            return False

        positions = response["result"]["list"]
        active_symbols_on_exchange = set()

        for pos in positions:
            symbol = pos["symbol"]
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            if size == 0:
                continue
            active_symbols_on_exchange.add(symbol)
            entry_price = float(pos.get("avgPrice", 0))

            with _state_lock:
                if symbol not in bot_state["positions"]:
                    bot_state["positions"][symbol] = {
                        "mode": "UNKNOWN",
                        "side": side,
                        "signal": "BUY" if side == "Buy" else "SELL",
                        "entry_price": entry_price,
                        "qty": size,
                        "tp_percent": 6.0,
                        "sl_percent": -2.5,
                        "current_sl": -2.5,
                        "open_time": datetime.now(),
                        "trailing_active": False,
                    }
                    logging.info(f"📥 Synced existing position: {symbol} {side} qty={size}")

        # ✅ FIX: Bybit'te kapanmış ama bot_state'te kalan pozisyonları temizle
        with _state_lock:
            stale = [
                s for s in bot_state["positions"]
                if s not in active_symbols_on_exchange
            ]
            for s in stale:
                logging.warning(f"🧹 Removing stale position (closed on exchange): {s}")
                del bot_state["positions"][s]

        logging.info(f"📊 Sync: {len(active_symbols_on_exchange)} active on exchange, {len(bot_state['positions'])} tracked")
        return True

    except Exception as e:
        logging.error(f"❌ Position sync error: {e}")
        return False

def update_balance():
    try:
        session = get_session()
        wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if wallet["retCode"] != 0:
            return False
        data = wallet["result"]["list"][0]["coin"][0]

        balance = float(data.get("walletBalance", 0))
        equity = float(data.get("equity", 0))
        total_position_im = float(data.get("totalPositionIM", 0))
        total_order_im = float(data.get("totalOrderIM", 0))
        available = equity - total_position_im - total_order_im

        available_field = data.get("availableToWithdraw", "")
        if available_field and available_field != "":
            try:
                available = float(available_field)
            except (ValueError, TypeError):
                pass

        with _state_lock:
            bot_state["balance"] = balance
            bot_state["available"] = max(0, available)

        logging.info(f"💰 Balance: ${balance:.2f} | Available: ${max(0, available):.2f}")
        return True

    except Exception as e:
        logging.error(f"❌ Balance error: {e}")
        return False

# =============================================================================
# LEVERAGE — Gerçek 1x garanti
# =============================================================================

_leverage_set = set()  # Daha önce ayarlanmış semboller

def ensure_leverage_1x(symbol):
    """Sembol için kaldıracı 1x olarak ayarla (bir kez)."""
    if symbol in _leverage_set:
        return True
    try:
        session = get_session()
        session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage="1",
            sellLeverage="1",
        )
        _leverage_set.add(symbol)
        logging.info(f"⚙️ Leverage set to 1x: {symbol}")
        return True
    except Exception as e:
        err_msg = str(e)
        # "leverage not modified" = zaten 1x, sorun yok
        if "not modified" in err_msg.lower() or "110043" in err_msg:
            _leverage_set.add(symbol)
            return True
        logging.error(f"❌ Leverage set failed {symbol}: {e}")
        return False

# =============================================================================
# KLINE & INDICATORS
# =============================================================================

def get_klines(symbol, interval="15", limit=100):
    try:
        session = get_session()
        resp = session.get_kline(
            category="linear", symbol=symbol, interval=interval, limit=limit
        )
        if resp["retCode"] != 0:
            return None
        data = resp["result"]["list"]
        df = pd.DataFrame(
            data,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float,
        })
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logging.error(f"Kline error {symbol}: {e}")
        return None

def get_cached_price(symbol):
    """Fiyatı cache'den al veya API'den çek."""
    now = time.time()
    if symbol in _price_cache:
        cached = _price_cache[symbol]
        if now - cached["time"] < PRICE_CACHE_TTL:
            return cached["price"]
    try:
        session = get_session()
        resp = session.get_tickers(category="linear", symbol=symbol)
        if resp["retCode"] == 0 and resp["result"]["list"]:
            price = float(resp["result"]["list"][0]["lastPrice"])
            _price_cache[symbol] = {"price": price, "time": now}
            return price
    except Exception as e:
        logging.error(f"Price fetch error {symbol}: {e}")
    return None

def calculate_indicators(df):
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    stoch = StochasticOscillator(
        df["high"], df["low"], df["close"], window=14, smooth_window=3
    )
    df["stoch"] = stoch.stoch()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    df["ema20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()
    return df

# =============================================================================
# SIGNAL GENERATION
# =============================================================================

def generate_signal(df, mode_config):
    try:
        if len(df) < 50:
            return None, 0
        df = calculate_indicators(df)
        last = df.iloc[-1]
        rsi = last["rsi"]
        stoch = last["stoch"]
        adx = last["adx"]
        ema20 = last["ema20"]
        ema50 = last["ema50"]
        close = last["close"]

        uptrend = ema20 > ema50 and close > ema20
        downtrend = ema20 < ema50 and close < ema20
        strong_trend = adx > mode_config["adx_min"]

        score = 0
        signal = None

        # Trend-following sinyaller
        if uptrend and strong_trend:
            if rsi < mode_config["rsi_oversold"] or stoch < 20:
                signal = "BUY"
                score += 3
                if rsi < 20:
                    score += 2
        elif downtrend and strong_trend:
            if rsi > mode_config["rsi_overbought"] or stoch > 80:
                signal = "SELL"
                score += 3
                if rsi > 80:
                    score += 2
        # Trend yoksa — yalnızca güçlü sinyal varsa (mean-reversion)
        else:
            if rsi > mode_config["rsi_overbought"] and stoch > 80 and adx > 15:
                signal = "SELL"
                score += 2
            elif rsi < mode_config["rsi_oversold"] and stoch < 20 and adx > 15:
                signal = "BUY"
                score += 2

        if score >= 2:
            return signal, score
        return None, 0

    except Exception as e:
        logging.error(f"Signal error: {e}")
        return None, 0

# =============================================================================
# COIN SCANNER — BATCH (HIZLI)
# =============================================================================

_symbols_cache = []
_symbols_cache_time = 0

def get_tradeable_symbols():
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if now - _symbols_cache_time > 600:
        try:
            session = get_session()
            instruments = session.get_instruments_info(category="linear")
            if instruments["retCode"] == 0:
                _symbols_cache = [
                    i["symbol"]
                    for i in instruments["result"]["list"]
                    if i["symbol"].endswith("USDT") and i["status"] == "Trading"
                ]
                _symbols_cache_time = now
                logging.info(f"📋 Symbol cache updated: {len(_symbols_cache)} symbols")
        except Exception as e:
            logging.error(f"Symbol cache error: {e}")
    return _symbols_cache

def get_batch_tickers(symbols):
    try:
        session = get_session()
        resp = session.get_tickers(category="linear")
        if resp["retCode"] != 0:
            return {}
        symbol_set = set(symbols)
        result = {}
        for item in resp["result"]["list"]:
            if item["symbol"] in symbol_set:
                result[item["symbol"]] = item
        return result
    except Exception as e:
        logging.error(f"Batch ticker error: {e}")
        return {}

def scan_coins(mode_name):
    try:
        mode = MODES[mode_name]
        if not mode["enabled"]:
            return []

        now = time.time()
        with _state_lock:
            if mode_name in bot_state["last_scan"]:
                if now - bot_state["last_scan"][mode_name] < mode["rescan_interval"]:
                    return []
            bot_state["last_scan"][mode_name] = now

            mode_positions = [
                p for p in bot_state["positions"].values() if p["mode"] == mode_name
            ]
            if len(mode_positions) >= mode["max_positions"]:
                return []

        logging.info(f"🔍 {mode_name}: Scanning coins (batch)...")

        symbols = get_tradeable_symbols()
        if not symbols:
            return []

        all_tickers = get_batch_tickers(symbols)
        if not all_tickers:
            return []

        # Ön filtre: volume + volatility
        with _state_lock:
            current_positions = set(bot_state["positions"].keys())

        pre_filtered = []
        for symbol, data in all_tickers.items():
            if is_blacklisted(symbol):
                continue
            if symbol in current_positions:
                continue
            try:
                volume = float(data.get("turnover24h", 0))
                price_change = abs(float(data.get("price24hPcnt", 0)) * 100)
                if volume >= mode["volume_min"] and price_change >= mode["volatility_min"]:
                    pre_filtered.append({
                        "symbol": symbol,
                        "price": float(data["lastPrice"]),
                        "volume": volume,
                        "change": price_change,
                    })
            except (ValueError, KeyError, TypeError) as e:
                logging.debug(f"Pre-filter skip {symbol}: {e}")
                continue

        pre_filtered.sort(key=lambda x: x["volume"], reverse=True)
        logging.info(f"   {mode_name}: {len(pre_filtered)} coins passed pre-filter")

        candidates = []
        scanned = 0

        for item in pre_filtered[:50]:
            symbol = item["symbol"]
            try:
                df = get_klines(symbol, limit=55)
                if df is None or len(df) < 50:
                    continue
                signal, score = generate_signal(df, mode)
                if signal and score >= 2:
                    candidates.append({
                        "symbol": symbol,
                        "signal": signal,
                        "score": score,
                        "volume": item["volume"],
                        "price": item["price"],
                    })
                scanned += 1

                # ✅ Rate limit koruması
                time.sleep(0.05)

                if len(candidates) >= 5:
                    break
            except Exception as e:
                logging.error(f"Scan error {symbol}: {e}")
                continue

        candidates.sort(key=lambda x: x["score"], reverse=True)
        logging.info(f"✅ {mode_name}: Scanned {scanned}, Found {len(candidates)} candidates")
        return candidates[:3]

    except Exception as e:
        logging.error(f"Scan error {mode_name}: {e}")
        return []

# =============================================================================
# TRADE EXECUTION
# =============================================================================

def get_step_size(symbol):
    try:
        session = get_session()
        inst = session.get_instruments_info(category="linear", symbol=symbol)
        if inst["retCode"] == 0:
            filters = inst["result"]["list"][0]["lotSizeFilter"]
            return float(filters["qtyStep"]), float(filters["minOrderQty"])
    except Exception as e:
        logging.error(f"Step size error {symbol}: {e}")
    return 0.01, 0.01

def normalize_qty(amount_usd, price, step, min_qty):
    d_amount = Decimal(str(amount_usd))
    d_price = Decimal(str(price))
    d_step = Decimal(str(step))
    d_min = Decimal(str(min_qty))

    raw_qty = d_amount / d_price
    qty = (raw_qty // d_step) * d_step
    qty = max(qty, d_min)
    return float(qty)

def open_position(symbol, signal, mode_name):
    try:
        mode = MODES[mode_name]
        position_usd = mode["position_size"]

        # ✅ Bakiye kontrolü
        with _state_lock:
            available = bot_state["available"]
        if available < position_usd:
            logging.warning(
                f"⚠️ {symbol}: Yetersiz bakiye! "
                f"Available=${available:.2f}, Need=${position_usd}"
            )
            return False

        # ✅ Kaldıracı 1x olarak ayarla
        if not ensure_leverage_1x(symbol):
            logging.error(f"❌ {symbol}: Leverage 1x ayarlanamadı, işlem iptal")
            return False

        session = get_session()
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        price = float(df["close"].iloc[-1])

        step, min_qty = get_step_size(symbol)
        qty = normalize_qty(position_usd, price, step, min_qty)
        actual_usd = qty * price

        # ✅ Minimum USD kontrolü
        if actual_usd < MIN_POSITION_USD:
            logging.warning(
                f"⚠️ {symbol}: İşlem çok küçük! "
                f"qty={qty} = ${actual_usd:.2f} (min ${MIN_POSITION_USD})"
            )
            return False

        if qty <= 0:
            logging.error(f"❌ {symbol}: Qty = 0")
            return False

        logging.info(
            f"📐 {symbol}: price=${price} step={step} min_qty={min_qty} "
            f"→ qty={qty} (${actual_usd:.2f} USD)"
        )

        side = "Buy" if signal == "BUY" else "Sell"
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="GTC",
            positionIdx=0,
        )

        if order["retCode"] != 0:
            logging.error(f"❌ Order failed {symbol}: {order['retMsg']}")
            return False

        with _state_lock:
            bot_state["positions"][symbol] = {
                "mode": mode_name,
                "side": side,
                "signal": signal,
                "entry_price": price,
                "qty": qty,
                "tp_percent": mode["tp_percent"],
                "sl_percent": mode["sl_percent"],
                "current_sl": mode["sl_percent"],
                "open_time": datetime.now(),
                "trailing_active": False,
            }

        logging.info(
            f"✅ {mode_name} | {symbol} {signal} @ ${price:.6f} | "
            f"Qty: {qty} | ~${actual_usd:.2f} | Leverage: 1x"
        )
        send_telegram(
            f"🟢 <b>{mode_name}</b>\n"
            f"{symbol} {signal}\n"
            f"💰 ${price:.6f}\n"
            f"📊 Qty: {qty} (~${actual_usd:.2f})\n"
            f"⚙️ Leverage: 1x"
        )
        return True

    except Exception as e:
        logging.error(f"❌ Open error {symbol}: {e}")
        return False

def close_position(symbol, reason=""):
    try:
        # ✅ Çift kapanma koruması
        with _state_lock:
            if symbol not in bot_state["positions"]:
                return False
            pos = bot_state["positions"][symbol].copy()

        session = get_session()

        # Güncel fiyat
        current_price = get_cached_price(symbol)
        if current_price is None:
            df = get_klines(symbol, limit=2)
            if df is None:
                return False
            current_price = float(df["close"].iloc[-1])

        side = "Sell" if pos["side"] == "Buy" else "Buy"
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(pos["qty"]),
            timeInForce="GTC",
            positionIdx=0,
            reduceOnly=True,
        )

        if order["retCode"] != 0:
            logging.error(f"❌ Close order failed {symbol}: {order['retMsg']}")
            return False

        qty = pos["qty"]
        if pos["signal"] == "BUY":
            pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
            pnl_usd = (current_price - pos["entry_price"]) * qty
        else:
            pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
            pnl_usd = (pos["entry_price"] - current_price) * qty

        with _state_lock:
            # Tekrar kontrol — başka thread silmiş olabilir
            if symbol not in bot_state["positions"]:
                return True

            bot_state["stats"]["total_trades"] += 1
            if pnl_usd > 0:
                bot_state["stats"]["winning_trades"] += 1
            bot_state["stats"]["total_pnl"] += pnl_usd
            bot_state["daily_pnl"] += pnl_usd

            # Günlük kayıp limiti kontrolü
            if bot_state["start_balance"] > 0:
                daily_pnl_pct = (bot_state["daily_pnl"] / bot_state["start_balance"]) * 100
                if daily_pnl_pct <= bot_state["daily_loss_limit"] and not bot_state["daily_loss_hit"]:
                    bot_state["daily_loss_hit"] = True
                    for m in MODES.keys():
                        MODES[m]["enabled"] = False
                    send_telegram(
                        f"🚨 <b>GÜNLÜK KAYIP LİMİTİ!</b>\n"
                        f"💰 Günlük P&L: ${bot_state['daily_pnl']:.2f} ({daily_pnl_pct:.2f}%)\n"
                        f"⛔ TÜM MODLAR DURDURULDU!"
                    )
                    logging.warning(f"🚨 GÜNLÜK KAYIP LİMİTİ! {daily_pnl_pct:.2f}%")

            # Kapanan işlemi geçmişe ekle
            bot_state["trade_history"].insert(0, {
                "symbol": symbol,
                "mode": pos["mode"],
                "signal": pos["signal"],
                "entry": pos["entry_price"],
                "exit": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "reason": reason,
                "time": datetime.now().strftime("%d.%m %H:%M"),
            })
            bot_state["trade_history"] = bot_state["trade_history"][:20]

            del bot_state["positions"][symbol]

        if pnl_usd < 0:
            track_loss(symbol, pnl_usd)

        emoji = "🟢" if pnl_usd > 0 else "🔴"
        logging.info(f"{emoji} {pos['mode']} | {symbol} closed: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) - {reason}")
        send_telegram(
            f"{emoji} <b>{pos['mode']}</b>\n"
            f"{symbol} CLOSED\n"
            f"💰 {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n"
            f"📝 {reason}"
        )
        return True

    except Exception as e:
        logging.error(f"❌ Close error {symbol}: {e}")
        return False

# =============================================================================
# TRAILING STOP
# =============================================================================

def update_trailing_stops():
    try:
        with _state_lock:
            snapshot = list(bot_state["positions"].items())

        for symbol, pos in snapshot:
            try:
                current_price = get_cached_price(symbol)
                if current_price is None:
                    continue

                if pos["signal"] == "BUY":
                    pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                else:
                    pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100

                # TP hit
                if pnl_pct >= pos["tp_percent"]:
                    close_position(symbol, f"TP Hit: {pnl_pct:.2f}%")
                    continue

                # SL hit
                if pnl_pct <= pos["current_sl"]:
                    close_position(symbol, f"SL Hit: {pnl_pct:.2f}%")
                    continue

                # Trailing SL güncelle
                for level in TRAILING_LEVELS:
                    if level["min"] <= pnl_pct < level["max"]:
                        new_sl = level["sl"]
                        if new_sl > pos["current_sl"]:
                            with _state_lock:
                                if symbol in bot_state["positions"]:
                                    bot_state["positions"][symbol]["current_sl"] = new_sl
                                    bot_state["positions"][symbol]["trailing_active"] = True
                            logging.info(
                                f"📈 {symbol}: Trailing SL → {new_sl:.1f}% (PNL: {pnl_pct:.2f}%)"
                            )
                        break

            except Exception as e:
                logging.error(f"Trailing error {symbol}: {e}")

    except Exception as e:
        logging.error(f"Update trailing error: {e}")

# =============================================================================
# MAIN BOT LOOP
# =============================================================================

def bot_loop():
    logging.info("🤖 Bot loop started")
    loop_count = 0

    while True:
        try:
            with _state_lock:
                running = bot_state["running"]

            if not running:
                time.sleep(2)
                continue

            # Günlük reset
            today = datetime.now().strftime("%Y-%m-%d")
            with _state_lock:
                if bot_state["last_reset_date"] != today:
                    bot_state["daily_pnl"] = 0.0
                    bot_state["daily_loss_hit"] = False
                    bot_state["start_balance"] = bot_state["balance"]
                    bot_state["last_reset_date"] = today
                    logging.info("🔄 DAILY RESET")
                    send_telegram("🔄 <b>GÜNLÜK RESET</b>\nYeni gün başladı!")

            update_balance()

            loop_count += 1
            if loop_count % 10 == 0:
                sync_positions()

            with _state_lock:
                daily_loss_hit = bot_state["daily_loss_hit"]

            if daily_loss_hit:
                time.sleep(10)
                continue

            for mode_name in ["SAFE", "MODERATE", "AGGRESSIVE"]:
                if MODES[mode_name]["enabled"]:
                    candidates = scan_coins(mode_name)
                    if candidates:
                        best = candidates[0]
                        open_position(best["symbol"], best["signal"], mode_name)

            update_trailing_stops()
            time.sleep(3)

        except Exception as e:
            logging.error(f"Bot loop error: {e}")
            time.sleep(10)

# =============================================================================
# GUNICORN-SAFE THREAD START
# =============================================================================

_bot_thread_started = False

def ensure_bot_thread():
    global _bot_thread_started
    if not _bot_thread_started:
        _bot_thread_started = True
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        logging.info("🤖 Bot thread started (gunicorn-safe)")

ensure_bot_thread()

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/status")
def api_status():
    positions_with_pnl = []

    with _state_lock:
        positions_snapshot = dict(bot_state["positions"])

    for symbol, pos in positions_snapshot.items():
        try:
            current_price = get_cached_price(symbol)
            if current_price is None:
                continue
            qty = pos["qty"]
            if pos["signal"] == "BUY":
                pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                pnl_usd = (current_price - pos["entry_price"]) * qty
            else:
                pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                pnl_usd = (pos["entry_price"] - current_price) * qty
            positions_with_pnl.append({
                "symbol": symbol,
                "mode": pos["mode"],
                "signal": pos["signal"],
                "entry": pos["entry_price"],
                "current": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "sl": pos["current_sl"],
                "tp": pos["tp_percent"],
                "trailing": pos["trailing_active"],
            })
        except Exception as e:
            logging.error(f"Status calc error {symbol}: {e}")

    with _state_lock:
        return jsonify({
            "running": bot_state["running"],
            "balance": round(bot_state["balance"], 2),
            "available": round(bot_state["available"], 2),
            "positions": positions_with_pnl,
            "modes": {k: v["enabled"] for k, v in MODES.items()},
            "stats": bot_state["stats"],
            "blacklist_count": len(bot_state["blacklist"]),
            "daily_pnl": round(bot_state["daily_pnl"], 2),
            "daily_loss_hit": bot_state["daily_loss_hit"],
            "daily_loss_limit": bot_state["daily_loss_limit"],
            "trade_history": bot_state["trade_history"],
        })

@app.route("/api/start_bot", methods=["POST"])
def start_bot():
    with _state_lock:
        bot_state["running"] = True

    update_balance()
    sync_positions()

    with _state_lock:
        today = datetime.now().strftime("%Y-%m-%d")
        if bot_state["last_reset_date"] != today:
            bot_state["daily_pnl"] = 0.0
            bot_state["daily_loss_hit"] = False
            bot_state["last_reset_date"] = today
        if bot_state["start_balance"] == 0.0:
            bot_state["start_balance"] = bot_state["balance"]

        bal = bot_state["balance"]
        avl = bot_state["available"]
        pos_count = len(bot_state["positions"])
        dpnl = bot_state["daily_pnl"]

    logging.info(f"🚀 Bot STARTED | Balance: ${bal:.2f} | Available: ${avl:.2f} | Positions: {pos_count}")
    send_telegram(
        f"🚀 <b>Bot STARTED</b>\n"
        f"💰 ${bal:.2f}\n"
        f"✅ ${avl:.2f}\n"
        f"📊 {pos_count} positions"
    )
    return jsonify({"success": True, "running": True})

@app.route("/api/stop_bot", methods=["POST"])
def stop_bot():
    with _state_lock:
        bot_state["running"] = False
    logging.info("⏹️ Bot STOPPED")
    send_telegram("⏹️ <b>Bot STOPPED</b>")
    return jsonify({"success": True, "running": False})

@app.route("/api/toggle_mode", methods=["POST"])
def toggle_mode():
    data = request.json
    mode = data.get("mode")
    enable = data.get("enable")
    if mode in MODES:
        if enable is not None:
            MODES[mode]["enabled"] = enable
        else:
            MODES[mode]["enabled"] = not MODES[mode]["enabled"]
        status = "ON" if MODES[mode]["enabled"] else "OFF"
        logging.info(f"⚙️ {mode}: {status}")
        return jsonify({"success": True, "enabled": MODES[mode]["enabled"]})
    return jsonify({"success": False})

@app.route("/api/close", methods=["POST"])
def api_close():
    data = request.json
    symbol = data.get("symbol")
    if close_position(symbol, "Manual close"):
        return jsonify({"success": True})
    return jsonify({"success": False})

# =============================================================================
# HTML TEMPLATE
# =============================================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bybit Bot v4.020</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%);
            color: #fff;
            padding: 20px;
            min-height: 100vh;
        }
        .container { max-width: 1600px; margin: 0 auto; }
        .header {
            text-align: center;
            background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%);
            padding: 30px;
            border-radius: 15px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }
        h1 { font-size: 2.5em; margin-bottom: 10px; }
        .version { color: #fbbf24; font-size: 1.2em; font-weight: bold; }
        .control-buttons { display: flex; gap: 15px; justify-content: center; margin-top: 15px; flex-wrap: wrap; }
        .btn-control {
            padding: 12px 40px; border: none; border-radius: 8px;
            font-size: 1.1em; font-weight: bold; cursor: pointer; transition: all 0.3s;
        }
        .btn-start { background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: white; }
        .btn-start:hover { transform: scale(1.05); box-shadow: 0 0 20px rgba(16,185,129,0.5); }
        .btn-stop { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); color: white; }
        .btn-stop:hover { transform: scale(1.05); box-shadow: 0 0 20px rgba(239,68,68,0.5); }
        .stats {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px; margin-bottom: 20px;
        }
        .stat-card {
            background: linear-gradient(135deg, rgba(30,58,138,0.8) 0%, rgba(59,130,246,0.6) 100%);
            padding: 20px; border-radius: 10px; text-align: center;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3); border: 1px solid rgba(59,130,246,0.3);
        }
        .stat-value { font-size: 2em; font-weight: bold; margin: 10px 0; }
        .stat-label { font-size: 0.9em; opacity: 0.9; }
        .live { color: #10b981; font-size: 0.8em; }
        .modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px; }
        .mode-card {
            background: linear-gradient(135deg, rgba(30,58,138,0.6) 0%, rgba(59,130,246,0.4) 100%);
            padding: 20px; border-radius: 10px; border: 2px solid rgba(59,130,246,0.3);
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }
        .mode-card.active {
            background: linear-gradient(135deg, rgba(16,185,129,0.6) 0%, rgba(5,150,105,0.4) 100%);
            border: 2px solid #10b981; box-shadow: 0 0 20px rgba(16,185,129,0.4);
        }
        .mode-title { font-size: 1.3em; font-weight: bold; margin-bottom: 10px; }
        .mode-toggle {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white; border: none; padding: 10px 20px; border-radius: 5px;
            cursor: pointer; font-size: 1em; font-weight: bold; width: 100%;
            margin-top: 10px; transition: all 0.3s;
        }
        .mode-toggle:hover { transform: scale(1.05); }
        .mode-toggle.off { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); }
        .mode-info { font-size: 0.85em; margin: 5px 0; opacity: 0.95; }
        .positions {
            background: linear-gradient(135deg, rgba(30,58,138,0.6) 0%, rgba(59,130,246,0.4) 100%);
            padding: 20px; border-radius: 10px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3); border: 1px solid rgba(59,130,246,0.3);
        }
        .positions h2 { margin-bottom: 15px; }
        .pos-table { width: 100%; border-collapse: collapse; background: rgba(0,0,0,0.3); border-radius: 8px; overflow: hidden; }
        .pos-table th { background: rgba(59,130,246,0.4); padding: 12px; text-align: left; font-weight: bold; }
        .pos-table td { padding: 12px; border-bottom: 1px solid rgba(59,130,246,0.2); }
        .pos-table tr:hover { background: rgba(59,130,246,0.2); }
        .pnl-positive { color: #4ade80; font-weight: bold; font-size: 1.1em; }
        .pnl-negative { color: #f87171; font-weight: bold; font-size: 1.1em; }
        .btn-close {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white; border: none; padding: 8px 16px; border-radius: 5px;
            cursor: pointer; font-weight: bold; transition: all 0.3s;
        }
        .btn-close:hover { transform: scale(1.05); }
        .badge { display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 0.85em; font-weight: bold; }
        .badge-safe { background: #10b981; }
        .badge-moderate { background: #f59e0b; }
        .badge-aggressive { background: #ef4444; }
        .badge-unknown { background: #6b7280; }
        .empty { text-align: center; padding: 40px; opacity: 0.6; }
        #scan-status {
            background: rgba(0,0,0,0.4);
            border: 1px solid rgba(59,130,246,0.3);
            border-radius: 8px;
            padding: 10px 16px;
            margin-bottom: 15px;
            font-size: 0.85em;
            color: #93c5fd;
        }
        .accordion { margin-top: 15px; border-radius: 10px; overflow: hidden; border: 1px solid rgba(59,130,246,0.3); }
        .accordion-header {
            background: linear-gradient(135deg, rgba(30,58,138,0.8) 0%, rgba(59,130,246,0.5) 100%);
            padding: 14px 20px; cursor: pointer;
            display: flex; justify-content: space-between; align-items: center;
            user-select: none; transition: background 0.2s;
        }
        .accordion-header:hover { background: linear-gradient(135deg, rgba(30,58,138,1) 0%, rgba(59,130,246,0.7) 100%); }
        .accordion-header h2 { font-size: 1em; margin: 0; }
        .accordion-arrow { font-size: 1em; transition: transform 0.3s; display: inline-block; }
        .accordion-arrow.open { transform: rotate(180deg); }
        .accordion-body {
            display: none;
            background: linear-gradient(135deg, rgba(30,58,138,0.4) 0%, rgba(59,130,246,0.2) 100%);
            padding: 15px;
        }
        .accordion-body.open { display: block; }
        .history-table {
            width: 100%; border-collapse: collapse;
            background: rgba(0,0,0,0.3); border-radius: 8px; overflow: hidden; font-size: 0.9em;
        }
        .history-table th { background: rgba(59,130,246,0.3); padding: 10px 12px; text-align: left; font-weight: bold; }
        .history-table td { padding: 10px 12px; border-bottom: 1px solid rgba(59,130,246,0.15); }
        .history-table tr:last-child td { border-bottom: none; }
        .history-table tr:hover { background: rgba(59,130,246,0.15); }
        .reason-tag { font-size: 0.78em; padding: 2px 7px; border-radius: 4px; background: rgba(255,255,255,0.1); color: #cbd5e1; }
        .leverage-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; background: #059669; color: white; margin-left: 8px; }
        @media (max-width: 768px) {
            .modes { grid-template-columns: 1fr; }
            h1 { font-size: 1.6em; }
            .stat-value { font-size: 1.4em; }
            .pos-table, .history-table { font-size: 0.8em; }
            .pos-table th, .pos-table td { padding: 8px 6px; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🤖 Bybit Trading Bot</h1>
        <div class="version">v4.020 - Triple Auto Mode <span class="leverage-badge">1x NO LEVERAGE</span></div>
        <div class="control-buttons">
            <button class="btn-control btn-start" id="btn-start-bot" onclick="startBot()">🚀 START BOT</button>
            <button class="btn-control btn-stop" id="btn-stop-bot" onclick="stopBot()">⏹️ STOP BOT</button>
        </div>
        <div class="control-buttons" style="margin-top:10px;">
            <button class="btn-control btn-start" onclick="enableAllModes()" style="font-size:0.9em;padding:8px 20px;">✅ Enable All Modes</button>
            <button class="btn-control btn-stop" onclick="disableAllModes()" style="font-size:0.9em;padding:8px 20px;">❌ Disable All Modes</button>
        </div>
    </div>

    <div id="scan-status">⏳ Connecting...</div>

    <div class="stats">
        <div class="stat-card">
            <div class="stat-label">💰 Balance <span class="live">● LIVE</span></div>
            <div class="stat-value" id="balance">$0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">✅ Available <span class="live">● LIVE</span></div>
            <div class="stat-value" id="available">$0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">📊 Positions</div>
            <div class="stat-value" id="pos-count">0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">📈 Win Rate</div>
            <div class="stat-value" id="win-rate">0%</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">💵 Total P&L</div>
            <div class="stat-value" id="total-pnl">$0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">📉 Daily P&L</div>
            <div class="stat-value" id="daily-pnl">$0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">⛔ Blacklist</div>
            <div class="stat-value" id="blacklist">0</div>
        </div>
    </div>

    <div class="modes">
        <div class="mode-card" id="mode-safe">
            <div class="mode-title">🛡️ SAFE</div>
            <div class="mode-info">Size: $35 | Max: 1 pos</div>
            <div class="mode-info">Vol: 10M+ | Vola: 2.5%+</div>
            <div class="mode-info">TP: 8% | SL: -2%</div>
            <button class="mode-toggle off" onclick="toggleMode('SAFE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-moderate">
            <div class="mode-title">⚖️ MODERATE</div>
            <div class="mode-info">Size: $45 | Max: 2 pos</div>
            <div class="mode-info">Vol: 5M+ | Vola: 2%+</div>
            <div class="mode-info">TP: 6% | SL: -2.5%</div>
            <button class="mode-toggle off" onclick="toggleMode('MODERATE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-aggressive">
            <div class="mode-title">🔥 AGGRESSIVE</div>
            <div class="mode-info">Size: $55 | Max: 3 pos</div>
            <div class="mode-info">Vol: 2M+ | Vola: 1.5%+</div>
            <div class="mode-info">TP: 5% | SL: -3%</div>
            <button class="mode-toggle off" onclick="toggleMode('AGGRESSIVE')">OFF</button>
        </div>
    </div>

    <div class="positions">
        <h2>📊 Active Positions <span class="live">● REAL-TIME</span></h2>
        <table class="pos-table">
            <thead>
                <tr>
                    <th>Mode</th><th>Symbol</th><th>Side</th>
                    <th>Entry</th><th>Current</th><th>P&L</th>
                    <th>SL/TP</th><th>Action</th>
                </tr>
            </thead>
            <tbody id="positions-body">
                <tr><td colspan="8" class="empty">No active positions</td></tr>
            </tbody>
        </table>
    </div>

    <div class="accordion">
        <div class="accordion-header" onclick="toggleAccordion()">
            <h2>📋 Son Kapanan İşlemler <span id="history-count" style="color:#fbbf24;font-size:0.9em;margin-left:8px;"></span></h2>
            <span class="accordion-arrow" id="accordion-arrow">▼</span>
        </div>
        <div class="accordion-body" id="accordion-body">
            <table class="history-table">
                <thead>
                    <tr>
                        <th>Tarih</th><th>Mode</th><th>Symbol</th><th>Side</th>
                        <th>Giriş</th><th>Çıkış</th><th>P&L</th><th>Neden</th>
                    </tr>
                </thead>
                <tbody id="history-body">
                    <tr><td colspan="8" class="empty">Henüz kapanan işlem yok</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>

<script>
function update() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            document.getElementById('btn-start-bot').style.display = data.running ? 'none' : 'block';
            document.getElementById('btn-stop-bot').style.display = data.running ? 'block' : 'none';

            let statusEl = document.getElementById('scan-status');
            if (!data.running) {
                statusEl.textContent = '⏸️ Bot stopped — Press START BOT to begin trading';
                statusEl.style.color = '#f87171';
            } else if (data.daily_loss_hit) {
                statusEl.textContent = '🚨 GÜNLÜK KAYIP LİMİTİ — Tüm modlar kapalı!';
                statusEl.style.color = '#f87171';
            } else {
                let enabledModes = Object.entries(data.modes).filter(([k,v]) => v).map(([k]) => k);
                if (enabledModes.length === 0) {
                    statusEl.textContent = '⚠️ Bot running but no modes enabled — Enable a mode to start scanning';
                    statusEl.style.color = '#fbbf24';
                } else {
                    statusEl.textContent = '✅ Bot running | Modes: ' + enabledModes.join(', ') + ' | Leverage: 1x | Scanning...';
                    statusEl.style.color = '#4ade80';
                }
            }

            document.getElementById('balance').textContent = '$' + data.balance.toFixed(2);
            document.getElementById('available').textContent = '$' + data.available.toFixed(2);
            document.getElementById('pos-count').textContent = data.positions.length;
            document.getElementById('blacklist').textContent = data.blacklist_count;
            let winRate = data.stats.total_trades > 0
                ? Math.round((data.stats.winning_trades / data.stats.total_trades) * 100) : 0;
            document.getElementById('win-rate').textContent = winRate + '%';
            let tpnl = data.stats.total_pnl;
            let tpnlEl = document.getElementById('total-pnl');
            tpnlEl.textContent = (tpnl >= 0 ? '+$' : '-$') + Math.abs(tpnl).toFixed(2);
            tpnlEl.style.color = tpnl >= 0 ? '#4ade80' : '#f87171';
            let dailyPnl = data.daily_pnl || 0;
            let dailyEl = document.getElementById('daily-pnl');
            dailyEl.textContent = (dailyPnl >= 0 ? '+$' : '-$') + Math.abs(dailyPnl).toFixed(2);
            dailyEl.style.color = dailyPnl >= 0 ? '#4ade80' : '#f87171';
            if (data.daily_loss_hit) dailyEl.parentElement.style.border = '2px solid #ef4444';
            else dailyEl.parentElement.style.border = '';

            ['SAFE', 'MODERATE', 'AGGRESSIVE'].forEach(mode => {
                let card = document.getElementById('mode-' + mode.toLowerCase());
                let btn = card.querySelector('.mode-toggle');
                if (data.modes[mode]) {
                    card.classList.add('active');
                    btn.classList.remove('off');
                    btn.textContent = 'ON';
                } else {
                    card.classList.remove('active');
                    btn.classList.add('off');
                    btn.textContent = 'OFF';
                }
            });

            let tbody = document.getElementById('positions-body');
            if (data.positions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" class="empty">No active positions</td></tr>';
            } else {
                tbody.innerHTML = data.positions.map(pos => `
                    <tr>
                        <td><span class="badge badge-${pos.mode.toLowerCase()}">${pos.mode}</span></td>
                        <td><strong>${pos.symbol}</strong></td>
                        <td>${pos.signal}</td>
                        <td>$${pos.entry < 1 ? pos.entry.toFixed(6) : pos.entry.toFixed(4)}</td>
                        <td>$${pos.current < 1 ? pos.current.toFixed(6) : pos.current.toFixed(4)}</td>
                        <td class="${pos.pnl_usd >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                            ${pos.pnl_usd >= 0 ? '+' : ''}${pos.pnl_usd.toFixed(2)} USDT<br>
                            <small>(${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%)</small>
                        </td>
                        <td>SL: ${pos.sl.toFixed(1)}% ${pos.trailing ? '📈' : ''}<br>TP: ${pos.tp.toFixed(1)}%</td>
                        <td><button class="btn-close" onclick="closePosition('${pos.symbol}')">Close</button></td>
                    </tr>
                `).join('');
            }
            updateHistory(data.trade_history);
        })
        .catch(() => {
            document.getElementById('scan-status').textContent = '🔴 Connection lost...';
            document.getElementById('scan-status').style.color = '#f87171';
        });
}

function toggleAccordion() {
    document.getElementById('accordion-body').classList.toggle('open');
    document.getElementById('accordion-arrow').classList.toggle('open');
}

function updateHistory(history) {
    let countEl = document.getElementById('history-count');
    let tbody = document.getElementById('history-body');
    if (!history || history.length === 0) {
        countEl.textContent = '';
        tbody.innerHTML = '<tr><td colspan="8" class="empty">Henüz kapanan işlem yok</td></tr>';
        return;
    }
    countEl.textContent = '(' + history.length + ')';
    tbody.innerHTML = history.map(t => {
        let pnlClass = t.pnl_usd >= 0 ? 'pnl-positive' : 'pnl-negative';
        let pnlSign = t.pnl_usd >= 0 ? '+' : '';
        return `
            <tr>
                <td style="white-space:nowrap;color:#94a3b8;">${t.time}</td>
                <td><span class="badge badge-${t.mode.toLowerCase()}">${t.mode}</span></td>
                <td><strong>${t.symbol}</strong></td>
                <td>${t.signal}</td>
                <td>$${t.entry < 1 ? t.entry.toFixed(6) : t.entry.toFixed(4)}</td>
                <td>$${t.exit < 1 ? t.exit.toFixed(6) : t.exit.toFixed(4)}</td>
                <td class="${pnlClass}">
                    ${pnlSign}${t.pnl_usd.toFixed(2)} USDT<br>
                    <small>(${pnlSign}${t.pnl_pct.toFixed(2)}%)</small>
                </td>
                <td><span class="reason-tag">${t.reason}</span></td>
            </tr>
        `;
    }).join('');
}

function toggleMode(mode) {
    fetch('/api/toggle_mode', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode})
    }).then(() => update());
}
function startBot() {
    fetch('/api/start_bot', {method: 'POST', headers: {'Content-Type': 'application/json'}}).then(() => update());
}
function stopBot() {
    if (!confirm('Stop bot? Active positions will remain open.')) return;
    fetch('/api/stop_bot', {method: 'POST', headers: {'Content-Type': 'application/json'}}).then(() => update());
}
function enableAllModes() {
    Promise.all(['SAFE', 'MODERATE', 'AGGRESSIVE'].map(mode =>
        fetch('/api/toggle_mode', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode, enable: true})
        })
    )).then(() => update());
}
function disableAllModes() {
    Promise.all(['SAFE', 'MODERATE', 'AGGRESSIVE'].map(mode =>
        fetch('/api/toggle_mode', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode, enable: false})
        })
    )).then(() => update());
}
function closePosition(symbol) {
    if (!confirm('Close ' + symbol + '?')) return;
    fetch('/api/close', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({symbol: symbol})
    }).then(() => update());
}

setInterval(update, 5000);
update();
</script>
</body>
</html>
"""

# =============================================================================
# MAIN (local development)
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"🚀 Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
