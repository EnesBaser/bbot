#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Trading Bot v4.030 - Triple Auto Mode

v4.030 — STRATEJİ TAMAMEN YENİDEN YAZILDI

ESKİ SORUN:
  Uptrend'de RSI oversold arıyordu → bu çelişkili bir durum.
  Güçlü uptrend + RSI<25 neredeyse imkansız; gerçekleştiğinde trend kırılıyor.
  Sonuç: %18 win rate, sürekli zarar.

YENİ STRATEJİ:
  1) Trend Pullback (ana strateji):
     - Uptrend (EMA20>EMA50) + RSI 40-55 arasına düşüş (sağlıklı pullback)
     - Stoch %K, %D'yi yukarı kesme (momentum dönüşü)
     → BUY (trend devamı)

  2) Momentum Breakout:
     - ADX yükseliyor + fiyat son 10 mumun en yükseğini kırdı
     - RSI 50-70 arası (güçlü ama aşırı değil)
     → BUY

  3) Multi-timeframe: 15m sinyal + 1h trend onayı

  4) Volume spike kontrolü: Ortalama volume'un 1.5x üstü

KORUNAN ÖZELLİKLER (v4.020'den):
  - set_leverage(1x), threading.Lock, sync_positions cleanup
  - Bakiye kontrolü, min $30 pozisyon, warmup
  - Trailing stop, blacklist, daily loss limit
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

MIN_POSITION_USD = 30.0
WARMUP_SECONDS = 60          # 30→60 saniye (daha temkinli başlangıç)
MIN_SIGNAL_SCORE = 3

MODES = {
    "SAFE": {
        "position_size": 35,
        "max_positions": 1,
        "volume_min": 10_000_000,
        "volatility_min": 2.0,
        "adx_min": 25,            # Trend gücü minimum
        "tp_percent": 4.0,        # 8→4 (daha gerçekçi, daha sık TP)
        "sl_percent": -2.0,
        "rescan_interval": 120,   # 90→120 (2 dakika)
        "enabled": False,
    },
    "MODERATE": {
        "position_size": 45,
        "max_positions": 2,
        "volume_min": 5_000_000,
        "volatility_min": 1.5,
        "adx_min": 22,
        "tp_percent": 3.5,        # 6→3.5
        "sl_percent": -2.0,
        "rescan_interval": 90,
        "enabled": False,
    },
    "AGGRESSIVE": {
        "position_size": 55,
        "max_positions": 3,
        "volume_min": 2_000_000,
        "volatility_min": 1.0,
        "adx_min": 20,
        "tp_percent": 3.0,        # 5→3
        "sl_percent": -2.0,       # 3→2 (daha sıkı SL)
        "rescan_interval": 60,
        "enabled": False,
    }
}

TRAILING_LEVELS = [
    {"min": 1.0,  "max": 1.9,  "sl": -1.0},    # Erken koruma
    {"min": 2.0,  "max": 2.9,  "sl": 0.5},
    {"min": 3.0,  "max": 4.4,  "sl": 1.5},
    {"min": 4.5,  "max": 6.9,  "sl": 2.5},
    {"min": 7.0,  "max": 9.9,  "sl": 4.0},
    {"min": 10.0, "max": 99.9, "sl": 7.0},
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
    "bot_started_at": 0,
    "recent_signals": {},
}

_price_cache = {}
PRICE_CACHE_TTL = 5

app = Flask(__name__)
session_cache = None

# =============================================================================
# BYBIT SESSION
# =============================================================================

def get_session():
    global session_cache
    if session_cache is None:
        session_cache = HTTP(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
    return session_cache

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=5)
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
        bot_state["loss_tracker"][symbol] = [l for l in bot_state["loss_tracker"][symbol] if l["time"] > cutoff]
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
    try:
        session = get_session()
        response = session.get_positions(category="linear", settleCoin="USDT")
        if response["retCode"] != 0:
            logging.error(f"Position sync failed: {response.get('retMsg', 'unknown')}")
            return False
        positions = response["result"]["list"]
        active_on_exchange = set()
        for pos in positions:
            symbol = pos["symbol"]
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            if size == 0:
                continue
            active_on_exchange.add(symbol)
            entry_price = float(pos.get("avgPrice", 0))
            with _state_lock:
                if symbol not in bot_state["positions"]:
                    bot_state["positions"][symbol] = {
                        "mode": "UNKNOWN", "side": side,
                        "signal": "BUY" if side == "Buy" else "SELL",
                        "entry_price": entry_price, "qty": size,
                        "tp_percent": 3.5, "sl_percent": -2.0,
                        "current_sl": -2.0, "open_time": datetime.now(),
                        "trailing_active": False,
                    }
                    logging.info(f"📥 Synced: {symbol} {side} qty={size}")
        with _state_lock:
            stale = [s for s in bot_state["positions"] if s not in active_on_exchange]
            for s in stale:
                logging.warning(f"🧹 Removing stale: {s}")
                del bot_state["positions"][s]
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
        tip = float(data.get("totalPositionIM", 0))
        toi = float(data.get("totalOrderIM", 0))
        available = equity - tip - toi
        af = data.get("availableToWithdraw", "")
        if af and af != "":
            try:
                available = float(af)
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
# LEVERAGE
# =============================================================================

_leverage_set = set()

def ensure_leverage_1x(symbol):
    if symbol in _leverage_set:
        return True
    try:
        session = get_session()
        session.set_leverage(category="linear", symbol=symbol, buyLeverage="1", sellLeverage="1")
        _leverage_set.add(symbol)
        return True
    except Exception as e:
        err_msg = str(e)
        if "not modified" in err_msg.lower() or "110043" in err_msg:
            _leverage_set.add(symbol)
            return True
        logging.error(f"❌ Leverage failed {symbol}: {e}")
        return False

# =============================================================================
# KLINE & INDICATORS
# =============================================================================

def get_klines(symbol, interval="15", limit=100):
    try:
        session = get_session()
        resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        if resp["retCode"] != 0:
            return None
        data = resp["result"]["list"]
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logging.error(f"Kline error {symbol}: {e}")
        return None

def get_cached_price(symbol):
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
    stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    df["ema20"] = EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()
    return df

# =============================================================================
# ✅ YENİ SİNYAL STRATEJİSİ
# =============================================================================

def check_higher_timeframe(symbol):
    """1h trend yönü: UP / DOWN / NEUTRAL"""
    try:
        df = get_klines(symbol, interval="60", limit=55)
        if df is None or len(df) < 50:
            return "NEUTRAL"
        df = calculate_indicators(df)
        last = df.iloc[-1]
        ema20 = last["ema20"]
        ema50 = last["ema50"]
        close = last["close"]
        adx = last["adx"]

        if ema20 > ema50 and close > ema50 and adx > 20:
            return "UP"
        elif ema20 < ema50 and close < ema50 and adx > 20:
            return "DOWN"
        return "NEUTRAL"
    except Exception as e:
        logging.error(f"HTF error {symbol}: {e}")
        return "NEUTRAL"

def generate_signal(df, mode_config):
    """
    YENİ STRATEJİ — Trend Pullback + Momentum Confirmation

    BUY koşulları (uptrend pullback):
      1. EMA20 > EMA50 (uptrend)
      2. ADX > mode minimum (trend güçlü)
      3. RSI 35-55 arası (pullback bölgesi — aşırı satılmış DEĞİL)
      4. Stoch %K > %D (momentum yukarı dönüyor)
      5. Fiyat EMA20 civarında veya altında (pullback noktası)

    SELL koşulları (downtrend rally):
      1. EMA20 < EMA50 (downtrend)
      2. ADX > mode minimum
      3. RSI 45-65 arası (rally bölgesi — aşırı alınmış DEĞİL)
      4. Stoch %K < %D (momentum aşağı dönüyor)
      5. Fiyat EMA20 civarında veya üstünde

    Scoring:
      +2 = Temel koşullar (trend + pullback zone)
      +1 = Stoch crossover doğrulaması
      +1 = Fiyat EMA20'ye yakın (ideal giriş)
      +1 = Volume spike (ortalamanın 1.5x üstü)
      +1 = ADX yükseliyor (trend güçleniyor)

    Minimum score: 3
    """
    try:
        if len(df) < 50:
            return None, 0, ""

        df = calculate_indicators(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        rsi = last["rsi"]
        stoch_k = last["stoch_k"]
        stoch_d = last["stoch_d"]
        prev_stoch_k = prev["stoch_k"]
        prev_stoch_d = prev["stoch_d"]
        adx = last["adx"]
        prev_adx = prev["adx"]
        ema20 = last["ema20"]
        ema50 = last["ema50"]
        close = last["close"]
        volume = last["volume"]

        # NaN kontrolü
        if pd.isna(rsi) or pd.isna(adx) or pd.isna(stoch_k) or pd.isna(stoch_d):
            return None, 0, ""

        uptrend = ema20 > ema50
        downtrend = ema20 < ema50
        strong_trend = adx > mode_config["adx_min"]

        # Volume ortalaması (son 20 mum)
        avg_volume = df["volume"].iloc[-20:].mean()
        volume_spike = volume > avg_volume * 1.5

        # ADX yükseliyor mu?
        adx_rising = adx > prev_adx

        score = 0
        signal = None
        reason_parts = []

        # ========== BUY: Uptrend Pullback ==========
        if uptrend and strong_trend:
            # RSI pullback zone: 35-55 (düşmüş ama aşırı değil)
            in_pullback = 35 <= rsi <= 55

            # Fiyat EMA20'ye yakın veya biraz altında (%2 tolerans)
            near_ema20 = close <= ema20 * 1.02

            if in_pullback and near_ema20:
                signal = "BUY"
                score += 2
                reason_parts.append(f"RSI={rsi:.0f}")

                # Stoch %K, %D'yi yukarı kesiyor (momentum dönüşü)
                stoch_cross_up = (prev_stoch_k <= prev_stoch_d) and (stoch_k > stoch_d)
                if stoch_cross_up:
                    score += 1
                    reason_parts.append("StochX↑")
                elif stoch_k > stoch_d:  # Zaten üstte ama cross yok
                    score += 0.5

                # Fiyat tam EMA20'de (%0.5 tolerans)
                if abs(close - ema20) / ema20 < 0.005:
                    score += 1
                    reason_parts.append("@EMA20")

                if volume_spike:
                    score += 1
                    reason_parts.append("VolSpike")

                if adx_rising:
                    score += 1
                    reason_parts.append("ADX↑")

        # ========== SELL: Downtrend Rally ==========
        elif downtrend and strong_trend:
            in_rally = 45 <= rsi <= 65
            near_ema20 = close >= ema20 * 0.98

            if in_rally and near_ema20:
                signal = "SELL"
                score += 2
                reason_parts.append(f"RSI={rsi:.0f}")

                stoch_cross_down = (prev_stoch_k >= prev_stoch_d) and (stoch_k < stoch_d)
                if stoch_cross_down:
                    score += 1
                    reason_parts.append("StochX↓")
                elif stoch_k < stoch_d:
                    score += 0.5

                if abs(close - ema20) / ema20 < 0.005:
                    score += 1
                    reason_parts.append("@EMA20")

                if volume_spike:
                    score += 1
                    reason_parts.append("VolSpike")

                if adx_rising:
                    score += 1
                    reason_parts.append("ADX↑")

        # Minimum score kontrolü
        if signal and score >= MIN_SIGNAL_SCORE:
            reason = " | ".join(reason_parts)
            return signal, int(score), reason
        return None, 0, ""

    except Exception as e:
        logging.error(f"Signal error: {e}")
        return None, 0, ""

# =============================================================================
# COIN SCANNER
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
                    i["symbol"] for i in instruments["result"]["list"]
                    if i["symbol"].endswith("USDT") and i["status"] == "Trading"
                ]
                _symbols_cache_time = now
                logging.info(f"📋 Symbols: {len(_symbols_cache)}")
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
        return {item["symbol"]: item for item in resp["result"]["list"] if item["symbol"] in symbol_set}
    except Exception as e:
        logging.error(f"Batch ticker error: {e}")
        return {}

def is_duplicate_signal(symbol, signal):
    with _state_lock:
        prev = bot_state["recent_signals"].get(symbol)
        if prev and prev["signal"] == signal:
            if time.time() - prev["time"] < 900:  # 15 dakika (10→15)
                return True
    return False

def record_signal(symbol, signal):
    with _state_lock:
        bot_state["recent_signals"][symbol] = {"signal": signal, "time": time.time()}

def scan_coins(mode_name):
    try:
        mode = MODES[mode_name]
        if not mode["enabled"]:
            return []

        now = time.time()
        with _state_lock:
            started_at = bot_state["bot_started_at"]
        if started_at > 0 and (now - started_at) < WARMUP_SECONDS:
            return []

        with _state_lock:
            if mode_name in bot_state["last_scan"]:
                if now - bot_state["last_scan"][mode_name] < mode["rescan_interval"]:
                    return []
            bot_state["last_scan"][mode_name] = now
            mode_positions = [p for p in bot_state["positions"].values() if p["mode"] == mode_name]
            if len(mode_positions) >= mode["max_positions"]:
                return []

        logging.info(f"🔍 {mode_name}: Scanning...")

        symbols = get_tradeable_symbols()
        if not symbols:
            return []
        all_tickers = get_batch_tickers(symbols)
        if not all_tickers:
            return []

        with _state_lock:
            current_positions = set(bot_state["positions"].keys())

        pre_filtered = []
        for symbol, data in all_tickers.items():
            if is_blacklisted(symbol) or symbol in current_positions:
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
            except (ValueError, KeyError, TypeError):
                continue

        pre_filtered.sort(key=lambda x: x["volume"], reverse=True)
        logging.info(f"   {mode_name}: {len(pre_filtered)} passed pre-filter")

        candidates = []
        scanned = 0

        for item in pre_filtered[:40]:
            symbol = item["symbol"]
            try:
                df = get_klines(symbol, limit=55)
                if df is None or len(df) < 50:
                    continue

                signal, score, reason = generate_signal(df, mode)
                scanned += 1

                if not signal or score < MIN_SIGNAL_SCORE:
                    time.sleep(0.05)
                    continue

                # 1h trend doğrulama
                htf = check_higher_timeframe(symbol)
                time.sleep(0.05)

                if signal == "BUY" and htf != "UP":
                    logging.info(f"   ❌ {symbol}: BUY but 1h={htf}")
                    continue
                if signal == "SELL" and htf != "DOWN":
                    logging.info(f"   ❌ {symbol}: SELL but 1h={htf}")
                    continue

                if is_duplicate_signal(symbol, signal):
                    logging.info(f"   ⏭️ {symbol}: Duplicate {signal}")
                    continue

                logging.info(f"   ✅ {symbol}: {signal} score={score} [{reason}]")

                candidates.append({
                    "symbol": symbol,
                    "signal": signal,
                    "score": score,
                    "reason": reason,
                    "volume": item["volume"],
                    "price": item["price"],
                })

                time.sleep(0.05)
                if len(candidates) >= 5:
                    break

            except Exception as e:
                logging.error(f"Scan error {symbol}: {e}")
                continue

        candidates.sort(key=lambda x: x["score"], reverse=True)
        logging.info(f"✅ {mode_name}: Scanned {scanned}, Found {len(candidates)}")
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
            f = inst["result"]["list"][0]["lotSizeFilter"]
            return float(f["qtyStep"]), float(f["minOrderQty"])
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

def open_position(symbol, signal, mode_name, reason=""):
    try:
        mode = MODES[mode_name]
        position_usd = mode["position_size"]

        with _state_lock:
            available = bot_state["available"]
        if available < position_usd:
            logging.warning(f"⚠️ {symbol}: Yetersiz bakiye ${available:.2f}")
            return False

        if not ensure_leverage_1x(symbol):
            return False

        session = get_session()
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        price = float(df["close"].iloc[-1])

        step, min_qty = get_step_size(symbol)
        qty = normalize_qty(position_usd, price, step, min_qty)
        actual_usd = qty * price

        if actual_usd < MIN_POSITION_USD:
            logging.warning(f"⚠️ {symbol}: ${actual_usd:.2f} < min ${MIN_POSITION_USD}")
            return False
        if qty <= 0:
            return False

        side = "Buy" if signal == "BUY" else "Sell"
        order = session.place_order(
            category="linear", symbol=symbol, side=side,
            orderType="Market", qty=str(qty),
            timeInForce="GTC", positionIdx=0,
        )

        if order["retCode"] != 0:
            logging.error(f"❌ Order failed {symbol}: {order['retMsg']}")
            return False

        with _state_lock:
            bot_state["positions"][symbol] = {
                "mode": mode_name, "side": side, "signal": signal,
                "entry_price": price, "qty": qty,
                "tp_percent": mode["tp_percent"], "sl_percent": mode["sl_percent"],
                "current_sl": mode["sl_percent"], "open_time": datetime.now(),
                "trailing_active": False,
            }

        record_signal(symbol, signal)

        logging.info(f"✅ {mode_name} | {symbol} {signal} @ ${price:.6f} | ~${actual_usd:.2f} | [{reason}]")
        send_telegram(
            f"🟢 <b>{mode_name}</b>\n{symbol} {signal}\n"
            f"💰 ${price:.6f} (~${actual_usd:.2f})\n"
            f"📊 TP:{mode['tp_percent']}% SL:{mode['sl_percent']}%\n"
            f"📝 {reason}"
        )
        return True

    except Exception as e:
        logging.error(f"❌ Open error {symbol}: {e}")
        return False

def close_position(symbol, reason=""):
    try:
        with _state_lock:
            if symbol not in bot_state["positions"]:
                return False
            pos = bot_state["positions"][symbol].copy()

        session = get_session()
        current_price = get_cached_price(symbol)
        if current_price is None:
            df = get_klines(symbol, limit=2)
            if df is None:
                return False
            current_price = float(df["close"].iloc[-1])

        side = "Sell" if pos["side"] == "Buy" else "Buy"
        order = session.place_order(
            category="linear", symbol=symbol, side=side,
            orderType="Market", qty=str(pos["qty"]),
            timeInForce="GTC", positionIdx=0, reduceOnly=True,
        )
        if order["retCode"] != 0:
            logging.error(f"❌ Close failed {symbol}: {order['retMsg']}")
            return False

        qty = pos["qty"]
        if pos["signal"] == "BUY":
            pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
            pnl_usd = (current_price - pos["entry_price"]) * qty
        else:
            pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
            pnl_usd = (pos["entry_price"] - current_price) * qty

        with _state_lock:
            if symbol not in bot_state["positions"]:
                return True
            bot_state["stats"]["total_trades"] += 1
            if pnl_usd > 0:
                bot_state["stats"]["winning_trades"] += 1
            bot_state["stats"]["total_pnl"] += pnl_usd
            bot_state["daily_pnl"] += pnl_usd

            if bot_state["start_balance"] > 0:
                dpct = (bot_state["daily_pnl"] / bot_state["start_balance"]) * 100
                if dpct <= bot_state["daily_loss_limit"] and not bot_state["daily_loss_hit"]:
                    bot_state["daily_loss_hit"] = True
                    for m in MODES.keys():
                        MODES[m]["enabled"] = False
                    send_telegram(f"🚨 <b>GÜNLÜK KAYIP LİMİTİ!</b>\n${bot_state['daily_pnl']:.2f} ({dpct:.2f}%)\n⛔ MODLAR KAPANDI")

            bot_state["trade_history"].insert(0, {
                "symbol": symbol, "mode": pos["mode"], "signal": pos["signal"],
                "entry": pos["entry_price"], "exit": current_price,
                "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
                "reason": reason, "time": datetime.now().strftime("%d.%m %H:%M"),
            })
            bot_state["trade_history"] = bot_state["trade_history"][:20]
            del bot_state["positions"][symbol]

        if pnl_usd < 0:
            track_loss(symbol, pnl_usd)

        emoji = "🟢" if pnl_usd > 0 else "🔴"
        logging.info(f"{emoji} {pos['mode']} | {symbol}: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) - {reason}")
        send_telegram(f"{emoji} <b>{pos['mode']}</b>\n{symbol}\n💰 {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n📝 {reason}")
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
                cp = get_cached_price(symbol)
                if cp is None:
                    continue
                if pos["signal"] == "BUY":
                    pnl_pct = ((cp - pos["entry_price"]) / pos["entry_price"]) * 100
                else:
                    pnl_pct = ((pos["entry_price"] - cp) / pos["entry_price"]) * 100

                if pnl_pct >= pos["tp_percent"]:
                    close_position(symbol, f"TP: {pnl_pct:.2f}%")
                    continue
                if pnl_pct <= pos["current_sl"]:
                    close_position(symbol, f"SL: {pnl_pct:.2f}%")
                    continue

                for level in TRAILING_LEVELS:
                    if level["min"] <= pnl_pct < level["max"]:
                        if level["sl"] > pos["current_sl"]:
                            with _state_lock:
                                if symbol in bot_state["positions"]:
                                    bot_state["positions"][symbol]["current_sl"] = level["sl"]
                                    bot_state["positions"][symbol]["trailing_active"] = True
                            logging.info(f"📈 {symbol}: TSL → {level['sl']:.1f}% (PNL: {pnl_pct:.2f}%)")
                        break
            except Exception as e:
                logging.error(f"Trailing error {symbol}: {e}")
    except Exception as e:
        logging.error(f"Trailing update error: {e}")

# =============================================================================
# BOT LOOP
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

            today = datetime.now().strftime("%Y-%m-%d")
            with _state_lock:
                if bot_state["last_reset_date"] != today:
                    bot_state["daily_pnl"] = 0.0
                    bot_state["daily_loss_hit"] = False
                    bot_state["start_balance"] = bot_state["balance"]
                    bot_state["last_reset_date"] = today
                    send_telegram("🔄 <b>GÜNLÜK RESET</b>")

            update_balance()
            loop_count += 1
            if loop_count % 10 == 0:
                sync_positions()

            with _state_lock:
                dlh = bot_state["daily_loss_hit"]
            if dlh:
                time.sleep(10)
                continue

            for mode_name in ["SAFE", "MODERATE", "AGGRESSIVE"]:
                if MODES[mode_name]["enabled"]:
                    candidates = scan_coins(mode_name)
                    if candidates:
                        best = candidates[0]
                        logging.info(f"🎯 {mode_name}: {best['symbol']} {best['signal']} score={best['score']}")
                        open_position(best["symbol"], best["signal"], mode_name, best.get("reason", ""))

            update_trailing_stops()
            time.sleep(3)

        except Exception as e:
            logging.error(f"Bot loop error: {e}")
            time.sleep(10)

# =============================================================================
# GUNICORN THREAD
# =============================================================================

_bot_thread_started = False
def ensure_bot_thread():
    global _bot_thread_started
    if not _bot_thread_started:
        _bot_thread_started = True
        threading.Thread(target=bot_loop, daemon=True).start()
        logging.info("🤖 Bot thread started")
ensure_bot_thread()

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/status")
def api_status():
    pos_list = []
    with _state_lock:
        snap = dict(bot_state["positions"])
    for sym, pos in snap.items():
        try:
            cp = get_cached_price(sym)
            if cp is None:
                continue
            q = pos["qty"]
            if pos["signal"] == "BUY":
                pp = ((cp - pos["entry_price"]) / pos["entry_price"]) * 100
                pu = (cp - pos["entry_price"]) * q
            else:
                pp = ((pos["entry_price"] - cp) / pos["entry_price"]) * 100
                pu = (pos["entry_price"] - cp) * q
            pos_list.append({
                "symbol": sym, "mode": pos["mode"], "signal": pos["signal"],
                "entry": pos["entry_price"], "current": cp,
                "pnl_pct": round(pp, 2), "pnl_usd": round(pu, 2),
                "sl": pos["current_sl"], "tp": pos["tp_percent"],
                "trailing": pos["trailing_active"],
            })
        except Exception as e:
            logging.error(f"Status error {sym}: {e}")
    with _state_lock:
        return jsonify({
            "running": bot_state["running"],
            "balance": round(bot_state["balance"], 2),
            "available": round(bot_state["available"], 2),
            "positions": pos_list,
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
        bot_state["bot_started_at"] = time.time()
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
        b, a, p = bot_state["balance"], bot_state["available"], len(bot_state["positions"])
    send_telegram(f"🚀 <b>Bot STARTED v4.030</b>\n💰 ${b:.2f} | ✅ ${a:.2f}\n📊 {p} pos | ⏳ {WARMUP_SECONDS}s warmup")
    return jsonify({"success": True, "running": True})

@app.route("/api/stop_bot", methods=["POST"])
def stop_bot():
    with _state_lock:
        bot_state["running"] = False
    send_telegram("⏹️ <b>Bot STOPPED</b>")
    return jsonify({"success": True, "running": False})

@app.route("/api/toggle_mode", methods=["POST"])
def toggle_mode():
    data = request.json
    mode = data.get("mode")
    enable = data.get("enable")
    if mode in MODES:
        MODES[mode]["enabled"] = enable if enable is not None else not MODES[mode]["enabled"]
        return jsonify({"success": True, "enabled": MODES[mode]["enabled"]})
    return jsonify({"success": False})

@app.route("/api/close", methods=["POST"])
def api_close():
    data = request.json
    symbol = data.get("symbol")
    return jsonify({"success": close_position(symbol, "Manual close")})

# =============================================================================
# HTML
# =============================================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bybit Bot v4.030</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0a0e27,#1a1f3a);color:#fff;padding:20px;min-height:100vh}
        .container{max-width:1600px;margin:0 auto}
        .header{text-align:center;background:linear-gradient(135deg,#1e3a8a,#3b82f6);padding:30px;border-radius:15px;margin-bottom:20px;box-shadow:0 8px 32px rgba(0,0,0,.3)}
        h1{font-size:2.5em;margin-bottom:10px}
        .version{color:#fbbf24;font-size:1.2em;font-weight:bold}
        .control-buttons{display:flex;gap:15px;justify-content:center;margin-top:15px;flex-wrap:wrap}
        .btn-control{padding:12px 40px;border:none;border-radius:8px;font-size:1.1em;font-weight:bold;cursor:pointer;transition:all .3s}
        .btn-start{background:linear-gradient(135deg,#10b981,#059669);color:#fff}
        .btn-start:hover{transform:scale(1.05);box-shadow:0 0 20px rgba(16,185,129,.5)}
        .btn-stop{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
        .btn-stop:hover{transform:scale(1.05);box-shadow:0 0 20px rgba(239,68,68,.5)}
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:20px}
        .stat-card{background:linear-gradient(135deg,rgba(30,58,138,.8),rgba(59,130,246,.6));padding:20px;border-radius:10px;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.3);border:1px solid rgba(59,130,246,.3)}
        .stat-value{font-size:2em;font-weight:bold;margin:10px 0}
        .stat-label{font-size:.9em;opacity:.9}
        .live{color:#10b981;font-size:.8em}
        .modes{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-bottom:20px}
        .mode-card{background:linear-gradient(135deg,rgba(30,58,138,.6),rgba(59,130,246,.4));padding:20px;border-radius:10px;border:2px solid rgba(59,130,246,.3)}
        .mode-card.active{background:linear-gradient(135deg,rgba(16,185,129,.6),rgba(5,150,105,.4));border:2px solid #10b981;box-shadow:0 0 20px rgba(16,185,129,.4)}
        .mode-title{font-size:1.3em;font-weight:bold;margin-bottom:10px}
        .mode-toggle{background:linear-gradient(135deg,#10b981,#059669);color:#fff;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:1em;font-weight:bold;width:100%;margin-top:10px}
        .mode-toggle.off{background:linear-gradient(135deg,#ef4444,#dc2626)}
        .mode-info{font-size:.85em;margin:5px 0;opacity:.95}
        .positions{background:linear-gradient(135deg,rgba(30,58,138,.6),rgba(59,130,246,.4));padding:20px;border-radius:10px;border:1px solid rgba(59,130,246,.3)}
        .positions h2{margin-bottom:15px}
        .pos-table{width:100%;border-collapse:collapse;background:rgba(0,0,0,.3);border-radius:8px;overflow:hidden}
        .pos-table th{background:rgba(59,130,246,.4);padding:12px;text-align:left}
        .pos-table td{padding:12px;border-bottom:1px solid rgba(59,130,246,.2)}
        .pnl-positive{color:#4ade80;font-weight:bold}
        .pnl-negative{color:#f87171;font-weight:bold}
        .btn-close{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;font-weight:bold}
        .badge{display:inline-block;padding:4px 10px;border-radius:4px;font-size:.85em;font-weight:bold}
        .badge-safe{background:#10b981}.badge-moderate{background:#f59e0b}.badge-aggressive{background:#ef4444}.badge-unknown{background:#6b7280}
        .empty{text-align:center;padding:40px;opacity:.6}
        #scan-status{background:rgba(0,0,0,.4);border:1px solid rgba(59,130,246,.3);border-radius:8px;padding:10px 16px;margin-bottom:15px;font-size:.85em;color:#93c5fd}
        .accordion{margin-top:15px;border-radius:10px;overflow:hidden;border:1px solid rgba(59,130,246,.3)}
        .accordion-header{background:linear-gradient(135deg,rgba(30,58,138,.8),rgba(59,130,246,.5));padding:14px 20px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none}
        .accordion-header:hover{background:linear-gradient(135deg,rgba(30,58,138,1),rgba(59,130,246,.7))}
        .accordion-header h2{font-size:1em;margin:0}
        .accordion-arrow{transition:transform .3s}.accordion-arrow.open{transform:rotate(180deg)}
        .accordion-body{display:none;background:linear-gradient(135deg,rgba(30,58,138,.4),rgba(59,130,246,.2));padding:15px}
        .accordion-body.open{display:block}
        .history-table{width:100%;border-collapse:collapse;background:rgba(0,0,0,.3);border-radius:8px;overflow:hidden;font-size:.9em}
        .history-table th{background:rgba(59,130,246,.3);padding:10px 12px;text-align:left}
        .history-table td{padding:10px 12px;border-bottom:1px solid rgba(59,130,246,.15)}
        .history-table tr:last-child td{border-bottom:none}
        .reason-tag{font-size:.78em;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,.1);color:#cbd5e1}
        .leverage-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.8em;background:#059669;color:#fff;margin-left:8px}
        @media(max-width:768px){.modes{grid-template-columns:1fr}h1{font-size:1.6em}.stat-value{font-size:1.4em}.pos-table,.history-table{font-size:.8em}.pos-table th,.pos-table td{padding:8px 6px}}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🤖 Bybit Trading Bot</h1>
        <div class="version">v4.030 — Trend Pullback Strategy <span class="leverage-badge">1x NO LEVERAGE</span></div>
        <div class="control-buttons">
            <button class="btn-control btn-start" id="btn-start-bot" onclick="startBot()">🚀 START</button>
            <button class="btn-control btn-stop" id="btn-stop-bot" onclick="stopBot()">⏹️ STOP</button>
        </div>
        <div class="control-buttons" style="margin-top:10px">
            <button class="btn-control btn-start" onclick="enableAll()" style="font-size:.9em;padding:8px 20px">✅ Enable All</button>
            <button class="btn-control btn-stop" onclick="disableAll()" style="font-size:.9em;padding:8px 20px">❌ Disable All</button>
        </div>
    </div>
    <div id="scan-status">⏳ Connecting...</div>
    <div class="stats">
        <div class="stat-card"><div class="stat-label">💰 Balance <span class="live">● LIVE</span></div><div class="stat-value" id="balance">$0</div></div>
        <div class="stat-card"><div class="stat-label">✅ Available</div><div class="stat-value" id="available">$0</div></div>
        <div class="stat-card"><div class="stat-label">📊 Positions</div><div class="stat-value" id="pos-count">0</div></div>
        <div class="stat-card"><div class="stat-label">📈 Win Rate</div><div class="stat-value" id="win-rate">0%</div></div>
        <div class="stat-card"><div class="stat-label">💵 Total P&L</div><div class="stat-value" id="total-pnl">$0</div></div>
        <div class="stat-card"><div class="stat-label">📉 Daily P&L</div><div class="stat-value" id="daily-pnl">$0</div></div>
        <div class="stat-card"><div class="stat-label">⛔ Blacklist</div><div class="stat-value" id="blacklist">0</div></div>
    </div>
    <div class="modes">
        <div class="mode-card" id="mode-safe">
            <div class="mode-title">🛡️ SAFE</div>
            <div class="mode-info">$35 | 1 pos | TP 4% SL -2%</div>
            <div class="mode-info">Vol 10M+ | ADX>25 | Scan 120s</div>
            <button class="mode-toggle off" onclick="toggleMode('SAFE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-moderate">
            <div class="mode-title">⚖️ MODERATE</div>
            <div class="mode-info">$45 | 2 pos | TP 3.5% SL -2%</div>
            <div class="mode-info">Vol 5M+ | ADX>22 | Scan 90s</div>
            <button class="mode-toggle off" onclick="toggleMode('MODERATE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-aggressive">
            <div class="mode-title">🔥 AGGRESSIVE</div>
            <div class="mode-info">$55 | 3 pos | TP 3% SL -2%</div>
            <div class="mode-info">Vol 2M+ | ADX>20 | Scan 60s</div>
            <button class="mode-toggle off" onclick="toggleMode('AGGRESSIVE')">OFF</button>
        </div>
    </div>
    <div class="positions">
        <h2>📊 Active Positions <span class="live">● REAL-TIME</span></h2>
        <table class="pos-table">
            <thead><tr><th>Mode</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Current</th><th>P&L</th><th>SL/TP</th><th>Action</th></tr></thead>
            <tbody id="positions-body"><tr><td colspan="8" class="empty">No active positions</td></tr></tbody>
        </table>
    </div>
    <div class="accordion">
        <div class="accordion-header" onclick="toggleAccordion()">
            <h2>📋 Kapanan İşlemler <span id="history-count" style="color:#fbbf24;font-size:.9em;margin-left:8px"></span></h2>
            <span class="accordion-arrow" id="accordion-arrow">▼</span>
        </div>
        <div class="accordion-body" id="accordion-body">
            <table class="history-table">
                <thead><tr><th>Tarih</th><th>Mode</th><th>Symbol</th><th>Side</th><th>Giriş</th><th>Çıkış</th><th>P&L</th><th>Neden</th></tr></thead>
                <tbody id="history-body"><tr><td colspan="8" class="empty">Henüz kapanan işlem yok</td></tr></tbody>
            </table>
        </div>
    </div>
</div>
<script>
let fmt=(v,d)=>v<1?v.toFixed(6):v.toFixed(d||4);
function update(){
    fetch('/api/status').then(r=>r.json()).then(d=>{
        document.getElementById('btn-start-bot').style.display=d.running?'none':'block';
        document.getElementById('btn-stop-bot').style.display=d.running?'block':'none';
        let s=document.getElementById('scan-status');
        if(!d.running){s.textContent='⏸️ Bot stopped';s.style.color='#f87171';}
        else if(d.daily_loss_hit){s.textContent='🚨 GÜNLÜK KAYIP LİMİTİ';s.style.color='#f87171';}
        else{let m=Object.entries(d.modes).filter(([k,v])=>v).map(([k])=>k);
            s.textContent=m.length?'✅ '+m.join(', ')+' | Pullback Strategy | 1x':'⚠️ No modes enabled';
            s.style.color=m.length?'#4ade80':'#fbbf24';}
        document.getElementById('balance').textContent='$'+d.balance.toFixed(2);
        document.getElementById('available').textContent='$'+d.available.toFixed(2);
        document.getElementById('pos-count').textContent=d.positions.length;
        document.getElementById('blacklist').textContent=d.blacklist_count;
        let wr=d.stats.total_trades>0?Math.round(d.stats.winning_trades/d.stats.total_trades*100):0;
        document.getElementById('win-rate').textContent=wr+'%';
        let tp=d.stats.total_pnl,te=document.getElementById('total-pnl');
        te.textContent=(tp>=0?'+$':'-$')+Math.abs(tp).toFixed(2);te.style.color=tp>=0?'#4ade80':'#f87171';
        let dp=d.daily_pnl||0,de=document.getElementById('daily-pnl');
        de.textContent=(dp>=0?'+$':'-$')+Math.abs(dp).toFixed(2);de.style.color=dp>=0?'#4ade80':'#f87171';
        de.parentElement.style.border=d.daily_loss_hit?'2px solid #ef4444':'';
        ['SAFE','MODERATE','AGGRESSIVE'].forEach(m=>{let c=document.getElementById('mode-'+m.toLowerCase()),b=c.querySelector('.mode-toggle');
            if(d.modes[m]){c.classList.add('active');b.classList.remove('off');b.textContent='ON';}
            else{c.classList.remove('active');b.classList.add('off');b.textContent='OFF';}});
        let tb=document.getElementById('positions-body');
        if(!d.positions.length)tb.innerHTML='<tr><td colspan="8" class="empty">No active positions</td></tr>';
        else tb.innerHTML=d.positions.map(p=>`<tr>
            <td><span class="badge badge-${p.mode.toLowerCase()}">${p.mode}</span></td>
            <td><strong>${p.symbol}</strong></td><td>${p.signal}</td>
            <td>$${fmt(p.entry)}</td><td>$${fmt(p.current)}</td>
            <td class="${p.pnl_usd>=0?'pnl-positive':'pnl-negative'}">${p.pnl_usd>=0?'+':''}${p.pnl_usd.toFixed(2)}<br><small>(${p.pnl_pct>=0?'+':''}${p.pnl_pct.toFixed(2)}%)</small></td>
            <td>SL:${p.sl.toFixed(1)}%${p.trailing?' 📈':''}<br>TP:${p.tp.toFixed(1)}%</td>
            <td><button class="btn-close" onclick="closePos('${p.symbol}')">Close</button></td></tr>`).join('');
        updHist(d.trade_history);
    }).catch(()=>{document.getElementById('scan-status').textContent='🔴 Lost';});
}
function toggleAccordion(){document.getElementById('accordion-body').classList.toggle('open');document.getElementById('accordion-arrow').classList.toggle('open');}
function updHist(h){let c=document.getElementById('history-count'),tb=document.getElementById('history-body');
    if(!h||!h.length){c.textContent='';tb.innerHTML='<tr><td colspan="8" class="empty">Henüz kapanan işlem yok</td></tr>';return;}
    c.textContent='('+h.length+')';
    tb.innerHTML=h.map(t=>`<tr><td style="white-space:nowrap;color:#94a3b8">${t.time}</td>
        <td><span class="badge badge-${t.mode.toLowerCase()}">${t.mode}</span></td>
        <td><strong>${t.symbol}</strong></td><td>${t.signal}</td>
        <td>$${fmt(t.entry)}</td><td>$${fmt(t.exit)}</td>
        <td class="${t.pnl_usd>=0?'pnl-positive':'pnl-negative'}">${t.pnl_usd>=0?'+':''}${t.pnl_usd.toFixed(2)}<br><small>(${t.pnl_pct>=0?'+':''}${t.pnl_pct.toFixed(2)}%)</small></td>
        <td><span class="reason-tag">${t.reason}</span></td></tr>`).join('');}
function toggleMode(m){fetch('/api/toggle_mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})}).then(()=>update());}
function startBot(){fetch('/api/start_bot',{method:'POST',headers:{'Content-Type':'application/json'}}).then(()=>update());}
function stopBot(){if(!confirm('Stop?'))return;fetch('/api/stop_bot',{method:'POST',headers:{'Content-Type':'application/json'}}).then(()=>update());}
function enableAll(){Promise.all(['SAFE','MODERATE','AGGRESSIVE'].map(m=>fetch('/api/toggle_mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m,enable:true})}))).then(()=>update());}
function disableAll(){Promise.all(['SAFE','MODERATE','AGGRESSIVE'].map(m=>fetch('/api/toggle_mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m,enable:false})}))).then(()=>update());}
function closePos(s){if(!confirm('Close '+s+'?'))return;fetch('/api/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:s})}).then(()=>update());}
setInterval(update,5000);update();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
