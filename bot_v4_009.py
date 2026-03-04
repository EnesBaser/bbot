#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Trading Bot v4.010 - Triple Auto Mode
- FIX: gunicorn uyumlu thread başlatma (app module load'da)
- FIX: Daha hızlı tarama (batch tickers)
- FIX: İlk başlatmada modlar açık değil ama UI düzgün sync
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template_string, jsonify, request
from pybit.unified_trading import HTTP
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import ADXIndicator, EMAIndicator
import requests
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# =============================================================================
# CONFIGURATION
# =============================================================================

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "").strip()
logging.info(f"🔑 API Key loaded: '{BYBIT_API_KEY[:8]}...' (len={len(BYBIT_API_KEY)})")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MODES = {
    "SAFE": {
        "position_size": 15,
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
        "position_size": 25,
        "max_positions": 2,
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
        "position_size": 35,
        "max_positions": 3,
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
}

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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=5)
    except:
        pass

# =============================================================================
# BLACKLIST
# =============================================================================

def is_blacklisted(symbol):
    if symbol not in bot_state["blacklist"]:
        return False
    expire = bot_state["blacklist"][symbol]["expire"]
    if datetime.now().timestamp() > expire:
        del bot_state["blacklist"][symbol]
        return False
    return True

def add_blacklist(symbol, reason):
    expire = (datetime.now() + timedelta(hours=BLACKLIST_CONFIG["duration_hours"])).timestamp()
    bot_state["blacklist"][symbol] = {"expire": expire, "reason": reason}
    logging.warning(f"⛔ BLACKLIST: {symbol} - {reason}")
    send_telegram(f"⛔ <b>BLACKLIST</b>\n{symbol}\n{reason}")

def track_loss(symbol, pnl):
    if symbol not in bot_state["loss_tracker"]:
        bot_state["loss_tracker"][symbol] = []
    bot_state["loss_tracker"][symbol].append({"time": datetime.now().timestamp(), "pnl": pnl})
    cutoff = datetime.now().timestamp() - 86400
    bot_state["loss_tracker"][symbol] = [l for l in bot_state["loss_tracker"][symbol] if l["time"] > cutoff]
    if BLACKLIST_CONFIG["enabled"]:
        recent = [l for l in bot_state["loss_tracker"][symbol] if l["pnl"] < 0]
        total_loss = sum(l["pnl"] for l in recent)
        if len(recent) >= BLACKLIST_CONFIG["loss_count"]:
            add_blacklist(symbol, f"{len(recent)} losses")
        elif total_loss <= BLACKLIST_CONFIG["loss_amount"]:
            add_blacklist(symbol, f"${abs(total_loss):.0f} loss")

# =============================================================================
# BALANCE & POSITIONS
# =============================================================================

def sync_positions():
    try:
        session = get_session()
        response = session.get_positions(category="linear", settleCoin="USDT")
        logging.info(f"📥 Position sync: retCode={response.get('retCode')}, retMsg={response.get('retMsg', 'OK')}")
        if response["retCode"] != 0:
            return False
        positions = response["result"]["list"]
        logging.info(f"📊 Total positions from API: {len(positions)}")
        synced = 0
        for pos in positions:
            symbol = pos["symbol"]
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            if size == 0:
                continue
            entry_price = float(pos.get("avgPrice", 0))
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
                    "trailing_active": False
                }
                synced += 1
        logging.info(f"📊 Sync complete: {synced} new positions, {len(bot_state['positions'])} total")
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
        logging.info(f"💰 Wallet data: {data}")
        bot_state["balance"] = float(data.get("walletBalance", 0))
        equity = float(data.get("equity", 0))
        total_position_im = float(data.get("totalPositionIM", 0))
        total_order_im = float(data.get("totalOrderIM", 0))
        available = equity - total_position_im - total_order_im
        available_field = data.get("availableToWithdraw", "")
        if available_field and available_field != "":
            try:
                available = float(available_field)
            except:
                pass
        bot_state["available"] = max(0, available)
        logging.info(f"💰 Balance: ${bot_state['balance']:.2f} | Available: ${bot_state['available']:.2f} | Equity: ${equity:.2f}")
        return True
    except Exception as e:
        logging.error(f"❌ Balance error: {e}")
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
    except:
        return None

def calculate_indicators(df):
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
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
        else:
            if rsi > mode_config["rsi_overbought"] and stoch > 75:
                signal = "SELL"
                score += 2
            elif rsi < mode_config["rsi_oversold"] and stoch < 25:
                signal = "BUY"
                score += 2
        if score >= 2:
            return signal, score
        return None, 0
    except Exception as e:
        logging.error(f"Signal error: {e}")
        return None, 0

# =============================================================================
# COIN SCANNER - BATCH (HIZLI)
# =============================================================================

# Tüm USDT çiftlerini cache'le (her 10 dakikada bir yenile)
_symbols_cache = []
_symbols_cache_time = 0

def get_tradeable_symbols():
    """USDT çiftlerini cache'li getir"""
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if now - _symbols_cache_time > 600:  # 10 dakika
        try:
            session = get_session()
            instruments = session.get_instruments_info(category="linear")
            if instruments["retCode"] == 0:
                _symbols_cache = [
                    i["symbol"] for i in instruments["result"]["list"]
                    if i["symbol"].endswith("USDT") and i["status"] == "Trading"
                ]
                _symbols_cache_time = now
                logging.info(f"📋 Symbol cache updated: {len(_symbols_cache)} symbols")
        except Exception as e:
            logging.error(f"Symbol cache error: {e}")
    return _symbols_cache

def get_batch_tickers(symbols):
    """
    Tüm ticker'ları tek seferde çek (batch).
    Bybit linear tickers endpoint'i filtre olmadan TÜM çiftleri döner.
    """
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
    """Belirtilen mod için coin tara - BATCH + HIZLI"""
    try:
        mode = MODES[mode_name]
        if not mode["enabled"]:
            return []

        # Cooldown
        now = time.time()
        if mode_name in bot_state["last_scan"]:
            if now - bot_state["last_scan"][mode_name] < mode["rescan_interval"]:
                return []
        bot_state["last_scan"][mode_name] = now

        # Max pozisyon kontrolü
        mode_positions = [p for p in bot_state["positions"].values() if p["mode"] == mode_name]
        if len(mode_positions) >= mode["max_positions"]:
            return []

        logging.info(f"🔍 {mode_name}: Scanning coins (batch)...")

        symbols = get_tradeable_symbols()
        if not symbols:
            return []

        # ✅ TEK API ÇAĞRISIYLA TÜM TICKERları AL
        all_tickers = get_batch_tickers(symbols)
        if not all_tickers:
            return []

        # Ön filtre: volume + volatility (hızlı, API yok)
        pre_filtered = []
        for symbol, data in all_tickers.items():
            if is_blacklisted(symbol):
                continue
            if symbol in bot_state["positions"]:
                continue
            try:
                volume = float(data.get("turnover24h", 0))
                price_change = abs(float(data.get("price24hPcnt", 0)) * 100)
                if volume >= mode["volume_min"] and price_change >= mode["volatility_min"]:
                    pre_filtered.append({
                        "symbol": symbol,
                        "price": float(data["lastPrice"]),
                        "volume": volume,
                        "change": price_change
                    })
            except:
                continue

        # Volume'a göre sırala (en likit önce)
        pre_filtered.sort(key=lambda x: x["volume"], reverse=True)

        logging.info(f"   {mode_name}: {len(pre_filtered)} coins passed pre-filter")

        candidates = []
        scanned = 0

        # Top 50'yi teknik analiz ile tara
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
                        "price": item["price"]
                    })
                scanned += 1
                if len(candidates) >= 5:
                    break
            except:
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
    except:
        pass
    return 0.01, 0.01

def normalize_qty(amount_usd, price, step, min_qty):
    qty = amount_usd / price
    qty = round(qty / step) * step
    return max(qty, min_qty)

def open_position(symbol, signal, mode_name):
    try:
        mode = MODES[mode_name]
        session = get_session()
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        price = float(df["close"].iloc[-1])
        step, min_qty = get_step_size(symbol)
        qty = normalize_qty(mode["position_size"], price, step, min_qty)
        if qty == 0:
            logging.error(f"❌ {symbol}: Qty = 0")
            return False
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage="10", sellLeverage="10")
        side = "Buy" if signal == "BUY" else "Sell"
        order = session.place_order(
            category="linear", symbol=symbol, side=side,
            orderType="Market", qty=str(qty),
            timeInForce="GTC", positionIdx=0
        )
        if order["retCode"] != 0:
            logging.error(f"❌ Order failed: {order['retMsg']}")
            return False
        bot_state["positions"][symbol] = {
            "mode": mode_name, "side": side, "signal": signal,
            "entry_price": price, "qty": qty,
            "tp_percent": mode["tp_percent"], "sl_percent": mode["sl_percent"],
            "current_sl": mode["sl_percent"], "open_time": datetime.now(),
            "trailing_active": False
        }
        logging.info(f"✅ {mode_name} | {symbol} {signal} @ ${price:.4f} | Qty: {qty}")
        send_telegram(f"🟢 <b>{mode_name}</b>\n{symbol} {signal}\n💰 ${price:.4f}\n📊 Qty: {qty}")
        return True
    except Exception as e:
        logging.error(f"❌ Open error {symbol}: {e}")
        return False

def close_position(symbol, reason=""):
    try:
        if symbol not in bot_state["positions"]:
            return False
        pos = bot_state["positions"][symbol]
        session = get_session()
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        exit_price = float(df["close"].iloc[-1])
        side = "Sell" if pos["side"] == "Buy" else "Buy"
        order = session.place_order(
            category="linear", symbol=symbol, side=side,
            orderType="Market", qty=str(pos["qty"]),
            timeInForce="GTC", positionIdx=0, reduceOnly=True
        )
        if order["retCode"] != 0:
            return False
        qty = pos["qty"]
        if pos["signal"] == "BUY":
            pnl_pct = ((exit_price - pos["entry_price"]) / pos["entry_price"]) * 100
            pnl_usd = (exit_price - pos["entry_price"]) * qty
        else:
            pnl_pct = ((pos["entry_price"] - exit_price) / pos["entry_price"]) * 100
            pnl_usd = (pos["entry_price"] - exit_price) * qty
        bot_state["stats"]["total_trades"] += 1
        if pnl_usd > 0:
            bot_state["stats"]["winning_trades"] += 1
        bot_state["stats"]["total_pnl"] += pnl_usd
        bot_state["daily_pnl"] += pnl_usd
        if bot_state["start_balance"] > 0:
            daily_pnl_pct = (bot_state["daily_pnl"] / bot_state["start_balance"]) * 100
            logging.info(f"📊 Daily P&L: ${bot_state['daily_pnl']:.2f} ({daily_pnl_pct:+.2f}%) | Limit: {bot_state['daily_loss_limit']}%")
            if daily_pnl_pct <= bot_state["daily_loss_limit"] and not bot_state["daily_loss_hit"]:
                bot_state["daily_loss_hit"] = True
                for m in MODES.keys():
                    MODES[m]["enabled"] = False
                send_telegram(
                    f"🚨 <b>GÜNLÜK KAYIP LİMİTİ!</b>\n"
                    f"💰 Günlük P&L: ${bot_state['daily_pnl']:.2f} ({daily_pnl_pct:.2f}%)\n"
                    f"📉 Limit: {bot_state['daily_loss_limit']}%\n"
                    f"⛔ TÜM MODLAR DURDURULDU!"
                )
                logging.warning(f"🚨 GÜNLÜK KAYIP LİMİTİ! {daily_pnl_pct:.2f}%")
        if pnl_usd < 0:
            track_loss(symbol, pnl_usd)
        del bot_state["positions"][symbol]
        emoji = "🟢" if pnl_usd > 0 else "🔴"
        logging.info(f"{emoji} {pos['mode']} | {symbol} closed: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) - {reason}")
        send_telegram(f"{emoji} <b>{pos['mode']}</b>\n{symbol} CLOSED\n💰 {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n📝 {reason}")
        return True
    except Exception as e:
        logging.error(f"❌ Close error {symbol}: {e}")
        return False

# =============================================================================
# TRAILING STOP
# =============================================================================

def update_trailing_stops():
    try:
        for symbol, pos in list(bot_state["positions"].items()):
            try:
                df = get_klines(symbol, limit=2)
                if df is None:
                    continue
                current_price = float(df["close"].iloc[-1])
                if pos["signal"] == "BUY":
                    pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                else:
                    pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                if pnl_pct >= pos["tp_percent"]:
                    close_position(symbol, f"TP Hit: {pnl_pct:.2f}%")
                    continue
                if pnl_pct <= pos["current_sl"]:
                    close_position(symbol, f"SL Hit: {pnl_pct:.2f}%")
                    continue
                for level in TRAILING_LEVELS:
                    if level["min"] <= pnl_pct < level["max"]:
                        new_sl = level["sl"]
                        if new_sl > pos["current_sl"]:
                            pos["current_sl"] = new_sl
                            pos["trailing_active"] = True
                            logging.info(f"📈 {symbol}: Trailing SL → {new_sl:.1f}% (PNL: {pnl_pct:.2f}%)")
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
            if not bot_state["running"]:
                time.sleep(2)
                continue

            # Günlük reset
            today = datetime.now().strftime("%Y-%m-%d")
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

            if bot_state["daily_loss_hit"]:
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
# ✅ GUNICORN UYUMLU THREAD BAŞLATMA
# Module import edildiğinde (gunicorn dahil) thread başlar
# =============================================================================

_bot_thread_started = False

def ensure_bot_thread():
    global _bot_thread_started
    if not _bot_thread_started:
        _bot_thread_started = True
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        logging.info("🤖 Bot thread started (gunicorn-safe)")

ensure_bot_thread()  # ← Gunicorn worker import ettiğinde çalışır

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/status')
def api_status():
    positions_with_pnl = []
    for symbol, pos in bot_state["positions"].items():
        try:
            df = get_klines(symbol, limit=2)
            if df is not None:
                current_price = float(df["close"].iloc[-1])
                qty = pos["qty"]
                if pos["signal"] == "BUY":
                    pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                    pnl_usd = (current_price - pos["entry_price"]) * qty
                else:
                    pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                    pnl_usd = (pos["entry_price"] - current_price) * qty
                positions_with_pnl.append({
                    "symbol": symbol, "mode": pos["mode"], "signal": pos["signal"],
                    "entry": pos["entry_price"], "current": current_price,
                    "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
                    "sl": pos["current_sl"], "tp": pos["tp_percent"],
                    "trailing": pos["trailing_active"]
                })
        except:
            pass
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
        "daily_loss_limit": bot_state["daily_loss_limit"]
    })

@app.route('/api/start_bot', methods=['POST'])
def start_bot():
    bot_state["running"] = True
    update_balance()
    sync_positions()
    today = datetime.now().strftime("%Y-%m-%d")
    if bot_state["last_reset_date"] != today:
        bot_state["daily_pnl"] = 0.0
        bot_state["daily_loss_hit"] = False
        bot_state["last_reset_date"] = today
    if bot_state["start_balance"] == 0.0:
        bot_state["start_balance"] = bot_state["balance"]
    logging.info("🚀 Bot STARTED")
    logging.info(f"💰 Balance: ${bot_state['balance']:.2f}")
    logging.info(f"✅ Available: ${bot_state['available']:.2f}")
    logging.info(f"📊 Active positions: {len(bot_state['positions'])}")
    logging.info(f"📉 Daily P&L: ${bot_state['daily_pnl']:.2f} | Limit: {bot_state['daily_loss_limit']}%")
    send_telegram(f"🚀 <b>Bot STARTED</b>\n💰 ${bot_state['balance']:.2f}\n✅ ${bot_state['available']:.2f}\n📊 {len(bot_state['positions'])} positions")
    return jsonify({"success": True, "running": True})

@app.route('/api/stop_bot', methods=['POST'])
def stop_bot():
    bot_state["running"] = False
    logging.info("⏹️ Bot STOPPED")
    send_telegram("⏹️ <b>Bot STOPPED</b>")
    return jsonify({"success": True, "running": False})

@app.route('/api/toggle_mode', methods=['POST'])
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

@app.route('/api/close', methods=['POST'])
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
    <title>Bybit Bot v4.010</title>
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
        .control-buttons { display: flex; gap: 15px; justify-content: center; margin-top: 15px; }
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

        /* Scan status bar */
        #scan-status {
            background: rgba(0,0,0,0.4);
            border: 1px solid rgba(59,130,246,0.3);
            border-radius: 8px;
            padding: 10px 16px;
            margin-bottom: 15px;
            font-size: 0.85em;
            color: #93c5fd;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🤖 Bybit Trading Bot</h1>
        <div class="version">v4.010 - Triple Auto Mode (Batch Scanner)</div>
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
            <div class="mode-info">Size: $15 | Max: 1 pos</div>
            <div class="mode-info">Vol: 10M+ | Vola: 2.5%+</div>
            <div class="mode-info">TP: 8% | SL: -2%</div>
            <button class="mode-toggle off" onclick="toggleMode('SAFE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-moderate">
            <div class="mode-title">⚖️ MODERATE</div>
            <div class="mode-info">Size: $25 | Max: 2 pos</div>
            <div class="mode-info">Vol: 5M+ | Vola: 2%+</div>
            <div class="mode-info">TP: 6% | SL: -2.5%</div>
            <button class="mode-toggle off" onclick="toggleMode('MODERATE')">OFF</button>
        </div>
        <div class="mode-card" id="mode-aggressive">
            <div class="mode-title">🔥 AGGRESSIVE</div>
            <div class="mode-info">Size: $35 | Max: 3 pos</div>
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
</div>

<script>
function update() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            // Bot status
            document.getElementById('btn-start-bot').style.display = data.running ? 'none' : 'block';
            document.getElementById('btn-stop-bot').style.display = data.running ? 'block' : 'none';

            // Scan status bar
            let statusEl = document.getElementById('scan-status');
            if (!data.running) {
                statusEl.textContent = '⏸️ Bot stopped — Press START BOT to begin trading';
                statusEl.style.color = '#f87171';
            } else if (data.daily_loss_hit) {
                statusEl.textContent = '🚨 GÜNLÜK KAYIP LİMİTİ AŞILDI — Tüm modlar kapalı!';
                statusEl.style.color = '#f87171';
            } else {
                let enabledModes = Object.entries(data.modes).filter(([k,v]) => v).map(([k]) => k);
                if (enabledModes.length === 0) {
                    statusEl.textContent = '⚠️ Bot running but no modes enabled — Enable a mode to start scanning';
                    statusEl.style.color = '#fbbf24';
                } else {
                    statusEl.textContent = '✅ Bot running | Active modes: ' + enabledModes.join(', ') + ' | Scanning every cycle...';
                    statusEl.style.color = '#4ade80';
                }
            }

            // Stats
            document.getElementById('balance').textContent = '$' + data.balance.toFixed(2);
            document.getElementById('available').textContent = '$' + data.available.toFixed(2);
            document.getElementById('pos-count').textContent = data.positions.length;
            document.getElementById('blacklist').textContent = data.blacklist_count;
            let winRate = data.stats.total_trades > 0
                ? Math.round((data.stats.winning_trades / data.stats.total_trades) * 100) : 0;
            document.getElementById('win-rate').textContent = winRate + '%';
            document.getElementById('total-pnl').textContent = '$' + data.stats.total_pnl.toFixed(2);
            let dailyPnl = data.daily_pnl || 0;
            let dailyEl = document.getElementById('daily-pnl');
            dailyEl.textContent = '$' + dailyPnl.toFixed(2);
            dailyEl.style.color = dailyPnl >= 0 ? '#4ade80' : '#f87171';
            if (data.daily_loss_hit) {
                dailyEl.parentElement.style.border = '2px solid #ef4444';
            }

            // Modes
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

            // Positions
            let tbody = document.getElementById('positions-body');
            if (data.positions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" class="empty">No active positions</td></tr>';
            } else {
                tbody.innerHTML = data.positions.map(pos => `
                    <tr>
                        <td><span class="badge badge-${pos.mode.toLowerCase()}">${pos.mode}</span></td>
                        <td><strong>${pos.symbol}</strong></td>
                        <td>${pos.signal}</td>
                        <td>$${pos.entry.toFixed(4)}</td>
                        <td>$${pos.current.toFixed(4)}</td>
                        <td class="${pos.pnl_usd >= 0 ? 'pnl-positive' : 'pnl-negative'}">
                            ${pos.pnl_usd >= 0 ? '+' : ''}${pos.pnl_usd.toFixed(2)} USDT<br>
                            <small>(${pos.pnl_pct >= 0 ? '+' : ''}${pos.pnl_pct.toFixed(2)}%)</small>
                        </td>
                        <td>SL: ${pos.sl.toFixed(1)}% ${pos.trailing ? '📈' : ''}<br>TP: ${pos.tp.toFixed(1)}%</td>
                        <td><button class="btn-close" onclick="closePosition('${pos.symbol}')">Close</button></td>
                    </tr>
                `).join('');
            }
        })
        .catch(() => {
            document.getElementById('scan-status').textContent = '🔴 Connection lost...';
        });
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
    ['SAFE', 'MODERATE', 'AGGRESSIVE'].forEach(mode => {
        fetch('/api/toggle_mode', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode, enable: true})
        });
    });
    setTimeout(update, 500);
}
function disableAllModes() {
    ['SAFE', 'MODERATE', 'AGGRESSIVE'].forEach(mode => {
        fetch('/api/toggle_mode', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: mode, enable: false})
        });
    });
    setTimeout(update, 500);
}
function closePosition(symbol) {
    if (!confirm('Close ' + symbol + '?')) return;
    fetch('/api/close', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({symbol: symbol})
    }).then(() => update());
}

setInterval(update, 1000);
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
