#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Trading Bot v4.0 - Triple Auto Mode
- 3 Seviye: SAFE, MODERATE, AGGRESSIVE
- Her biri bağımsız çalışır
- Trailing Stop zorunlu
- Max win rate optimize
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

# API Keys (Railway environment variables'dan okunur)
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "").strip()
logging.info(f"🔑 API Key loaded: '{BYBIT_API_KEY[:8]}...' (len={len(BYBIT_API_KEY)})")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 🎯 3 MOD AYARLARI
MODES = {
    "SAFE": {
        "position_size": 15,  # $15 USD
        "max_positions": 1,  # Max 1 pozisyon
        "volume_min": 10_000_000,  # 10M+ volume
        "volatility_min": 2.5,  # %2.5+ hareket
        "rsi_oversold": 25,  # RSI < 25 = BUY
        "rsi_overbought": 75,  # RSI > 75 = SELL
        "adx_min": 30,  # Güçlü trend
        "tp_percent": 8.0,  # %8 TP
        "sl_percent": -2.0,  # %-2 SL
        "rescan_interval": 60,  # ⚡ 1 dakika (300'den)
        "enabled": False,
    },
    "MODERATE": {
        "position_size": 25,  # $25 USD
        "max_positions": 2,  # Max 2 pozisyon
        "volume_min": 5_000_000,  # 5M+ volume
        "volatility_min": 2.0,  # %2+ hareket
        "rsi_oversold": 30,  # RSI < 30 = BUY
        "rsi_overbought": 70,  # RSI > 70 = SELL
        "adx_min": 25,  # Orta trend
        "tp_percent": 6.0,  # %6 TP
        "sl_percent": -2.5,  # %-2.5 SL
        "rescan_interval": 45,  # ⚡ 45 saniye (240'tan)
        "enabled": False,
    },
    "AGGRESSIVE": {
        "position_size": 35,  # $35 USD
        "max_positions": 3,  # Max 3 pozisyon
        "volume_min": 2_000_000,  # 2M+ volume
        "volatility_min": 1.5,  # %1.5+ hareket
        "rsi_oversold": 35,  # RSI < 35 = BUY
        "rsi_overbought": 65,  # RSI > 65 = SELL
        "adx_min": 20,  # Zayıf trend bile OK
        "tp_percent": 5.0,  # %5 TP
        "sl_percent": -3.0,  # %-3 SL
        "rescan_interval": 30,  # ⚡ 30 saniye (180'den)
        "enabled": False,
    }
}

# 🔥 TRAILING STOP (Optimize edilmiş)
TRAILING_LEVELS = [
    {"min": 1.0, "max": 2.4, "sl": -2.0},   # Başlangıç
    {"min": 2.5, "max": 3.9, "sl": 0.5},    # Breakeven
    {"min": 4.0, "max": 5.9, "sl": 2.0},    # Garantili kar
    {"min": 6.0, "max": 8.9, "sl": 3.5},    # Yüksek kar
    {"min": 9.0, "max": 12.9, "sl": 6.0},   # Çok yüksek
    {"min": 13.0, "max": 99.9, "sl": 9.0},  # Maximum
]

# ⛔ BLACKLIST
BLACKLIST_CONFIG = {
    "enabled": True,
    "loss_count": 3,  # 3 zarar = ban
    "loss_amount": -30,  # $30 zarar = ban
    "duration_hours": 24,  # 24 saat
}

# Global state
bot_state = {
    "running": False,  # Bot çalışıyor mu?
    "balance": 0.0,
    "available": 0.0,
    "positions": {},  # {symbol: {mode, side, entry, qty, ...}}
    "blacklist": {},  # {symbol: {expire, reason}}
    "loss_tracker": {},  # {symbol: [{time, pnl}]}
    "last_scan": {},  # {mode: timestamp}
    "stats": {
        "total_trades": 0,
        "winning_trades": 0,
        "total_pnl": 0.0,
    },
    # 📉 GÜNLÜK KAYIP LİMİTİ
    "daily_pnl": 0.0,
    "daily_loss_limit": -5.0,  # %5 günlük kayıp
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
    
    # 24 saatten eski kayıtları sil
    cutoff = datetime.now().timestamp() - 86400
    bot_state["loss_tracker"][symbol] = [l for l in bot_state["loss_tracker"][symbol] if l["time"] > cutoff]
    
    # Auto blacklist
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
    """Bybit'ten aktif pozisyonları çek ve senkronize et"""
    try:
        session = get_session()
        
        # Tüm açık pozisyonları al (DOĞRU METOD)
        response = session.get_positions(category="linear", settleCoin="USDT")
        
        logging.info(f"📥 Position sync: retCode={response.get('retCode')}, retMsg={response.get('retMsg', 'OK')}")
        
        if response["retCode"] != 0:
            logging.error(f"❌ Position API error: {response['retMsg']}")
            return False
        
        positions = response["result"]["list"]
        logging.info(f"📊 Total positions from API: {len(positions)}")
        
        synced = 0
        
        for pos in positions:
            symbol = pos["symbol"]
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            
            logging.info(f"   {symbol}: size={size}, side={side}")
            
            if size == 0:
                continue  # Kapalı pozisyon
            
            entry_price = float(pos.get("avgPrice", 0))
            
            # State'e ekle (eğer yoksa)
            if symbol not in bot_state["positions"]:
                bot_state["positions"][symbol] = {
                    "mode": "UNKNOWN",  # Eski pozisyon, mod bilinmiyor
                    "side": side,
                    "signal": "BUY" if side == "Buy" else "SELL",
                    "entry_price": entry_price,
                    "qty": size,
                    "tp_percent": 6.0,  # Default
                    "sl_percent": -2.5,  # Default
                    "current_sl": -2.5,
                    "open_time": datetime.now(),
                    "trailing_active": False
                }
                synced += 1
                logging.info(f"✅ Synced: {symbol} {side} @ ${entry_price:.8f} (qty: {size})")
        
        logging.info(f"📊 Sync complete: {synced} new positions, {len(bot_state['positions'])} total")
        
        return True
        
    except Exception as e:
        logging.error(f"❌ Position sync error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False

def update_balance():
    try:
        session = get_session()
        wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        
        if wallet["retCode"] != 0:
            logging.error(f"❌ Balance API error: {wallet.get('retMsg', 'Unknown')}")
            return False
        
        # Debug: Response'u logla
        data = wallet["result"]["list"][0]["coin"][0]
        logging.info(f"💰 Wallet data: {data}")
        
        # Balance
        bot_state["balance"] = float(data.get("walletBalance", 0))
        
        # Available - Bybit boş string dönebiliyor, dikkat!
        equity = float(data.get("equity", 0))
        total_position_im = float(data.get("totalPositionIM", 0))
        total_order_im = float(data.get("totalOrderIM", 0))
        
        # Available = Equity - Used Margin
        available = equity - total_position_im - total_order_im
        
        # Alternatif: availableToWithdraw varsa kullan (ama boş string olabilir)
        available_field = data.get("availableToWithdraw", "")
        if available_field and available_field != "":
            try:
                available = float(available_field)
            except:
                pass  # Hesaplanan değeri kullan
        
        bot_state["available"] = max(0, available)  # Negatif olmasın
        
        logging.info(f"💰 Balance: ${bot_state['balance']:.2f} | Available: ${bot_state['available']:.2f} | Equity: ${equity:.2f}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Balance error: {e}")
        import traceback
        logging.error(traceback.format_exc())
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
# SIGNAL GENERATION (WIN RATE OPTIMIZE)
# =============================================================================

def generate_signal(df, mode_config):
    """
    Yüksek win rate için optimize edilmiş sinyal
    """
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
        
        # Trend belirleme
        uptrend = ema20 > ema50 and close > ema20
        downtrend = ema20 < ema50 and close < ema20
        
        # ADX güçlü mü?
        strong_trend = adx > mode_config["adx_min"]
        
        score = 0
        signal = None
        
        # 🟢 BUY Sinyali (Uptrend + Oversold)
        if uptrend and strong_trend:
            if rsi < mode_config["rsi_oversold"] or stoch < 20:
                signal = "BUY"
                score += 3
                if rsi < 20:  # Extreme oversold
                    score += 2
        
        # 🔴 SELL Sinyali (Downtrend + Overbought)
        elif downtrend and strong_trend:
            if rsi > mode_config["rsi_overbought"] or stoch > 80:
                signal = "SELL"
                score += 3
                if rsi > 80:  # Extreme overbought
                    score += 2
        
        # Sideways - Mean Reversion
        else:
            if rsi > mode_config["rsi_overbought"] and stoch > 75:
                signal = "SELL"
                score += 2
            elif rsi < mode_config["rsi_oversold"] and stoch < 25:
                signal = "BUY"
                score += 2
        
        # Minimum score: 2
        if score >= 2:
            return signal, score
        
        return None, 0
        
    except Exception as e:
        logging.error(f"Signal error: {e}")
        return None, 0

# =============================================================================
# COIN SCANNER
# =============================================================================

def scan_coins(mode_name):
    """Belirtilen mod için coin tara - HIZLI"""
    try:
        mode = MODES[mode_name]
        if not mode["enabled"]:
            return []
        
        # Cooldown kontrolü
        now = time.time()
        if mode_name in bot_state["last_scan"]:
            elapsed = now - bot_state["last_scan"][mode_name]
            if elapsed < mode["rescan_interval"]:
                return []
        
        bot_state["last_scan"][mode_name] = now
        
        # Aktif pozisyon sayısı
        mode_positions = [p for p in bot_state["positions"].values() if p["mode"] == mode_name]
        if len(mode_positions) >= mode["max_positions"]:
            return []
        
        logging.info(f"🔍 {mode_name}: Scanning coins...")
        
        # Tüm USDT çiftlerini al
        session = get_session()
        instruments = session.get_instruments_info(category="linear")
        if instruments["retCode"] != 0:
            return []
        
        symbols = [i["symbol"] for i in instruments["result"]["list"] 
                  if i["symbol"].endswith("USDT") and i["status"] == "Trading"]
        
        candidates = []
        scanned = 0
        
        # İlk 200 coin'i hızlıca tara
        for symbol in symbols[:200]:
            try:
                # Blacklist kontrolü
                if is_blacklisted(symbol):
                    continue
                
                # Aktif pozisyon var mı?
                if symbol in bot_state["positions"]:
                    continue
                
                # Ticker (tek API call)
                ticker = session.get_tickers(category="linear", symbol=symbol)
                if ticker["retCode"] != 0:
                    continue
                
                data = ticker["result"]["list"][0]
                volume = float(data.get("turnover24h", 0))
                price_change = float(data.get("price24hPcnt", 0)) * 100
                
                # Hızlı filtreler
                if volume < mode["volume_min"]:
                    continue
                if abs(price_change) < mode["volatility_min"]:
                    continue
                
                # Teknik analiz (sadece promising coinler için)
                df = get_klines(symbol, limit=50)  # 100'den 50'ye düşürdüm
                if df is None or len(df) < 50:
                    continue
                
                signal, score = generate_signal(df, mode)
                
                if signal and score >= 2:
                    candidates.append({
                        "symbol": symbol,
                        "signal": signal,
                        "score": score,
                        "volume": volume,
                        "price": float(data["lastPrice"])
                    })
                    
                scanned += 1
                
                # İlk 5 candidate bulunca dur (hız için)
                if len(candidates) >= 5:
                    break
                    
            except Exception as e:
                continue
        
        # Score'a göre sırala
        candidates.sort(key=lambda x: x["score"], reverse=True)
        
        logging.info(f"✅ {mode_name}: Scanned {scanned}, Found {len(candidates)} candidates")
        
        return candidates[:3]  # Top 3
        
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
    """Pozisyon aç"""
    try:
        mode = MODES[mode_name]
        session = get_session()
        
        # Fiyat al
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        price = float(df["close"].iloc[-1])
        
        # Qty hesapla
        step, min_qty = get_step_size(symbol)
        qty = normalize_qty(mode["position_size"], price, step, min_qty)
        
        if qty == 0:
            logging.error(f"❌ {symbol}: Qty = 0")
            return False
        
        # Leverage
        session.set_leverage(category="linear", symbol=symbol, 
                            buyLeverage="10", sellLeverage="10")
        
        # Order
        side = "Buy" if signal == "BUY" else "Sell"
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="GTC",
            positionIdx=0
        )
        
        if order["retCode"] != 0:
            logging.error(f"❌ Order failed: {order['retMsg']}")
            return False
        
        # State'e ekle
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
            "trailing_active": False
        }
        
        logging.info(f"✅ {mode_name} | {symbol} {signal} @ ${price:.4f} | Qty: {qty}")
        send_telegram(f"🟢 <b>{mode_name}</b>\n{symbol} {signal}\n💰 ${price:.4f}\n📊 Qty: {qty}")
        
        return True
        
    except Exception as e:
        logging.error(f"❌ Open error {symbol}: {e}")
        return False

def close_position(symbol, reason=""):
    """Pozisyon kapat"""
    try:
        if symbol not in bot_state["positions"]:
            return False
        
        pos = bot_state["positions"][symbol]
        session = get_session()
        
        # Fiyat al
        df = get_klines(symbol, limit=2)
        if df is None:
            return False
        exit_price = float(df["close"].iloc[-1])
        
        # Close order
        side = "Sell" if pos["side"] == "Buy" else "Buy"
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(pos["qty"]),
            timeInForce="GTC",
            positionIdx=0,
            reduceOnly=True
        )
        
        if order["retCode"] != 0:
            return False
        
        # PNL hesapla (DOĞRU YÖNTEM: qty * price_diff)
        qty = pos["qty"]
        if pos["signal"] == "BUY":
            pnl_pct = ((exit_price - pos["entry_price"]) / pos["entry_price"]) * 100
            pnl_usd = (exit_price - pos["entry_price"]) * qty  # Gerçek PNL
        else:
            pnl_pct = ((pos["entry_price"] - exit_price) / pos["entry_price"]) * 100
            pnl_usd = (pos["entry_price"] - exit_price) * qty  # Gerçek PNL
        
        # Stats güncelle
        bot_state["stats"]["total_trades"] += 1
        if pnl_usd > 0:
            bot_state["stats"]["winning_trades"] += 1
        bot_state["stats"]["total_pnl"] += pnl_usd
        
        # 📉 GÜNLÜK P&L GÜNCELLE
        bot_state["daily_pnl"] += pnl_usd
        
        # GÜNLÜK KAYIP LİMİTİ KONTROLÜ
        if bot_state["start_balance"] > 0:
            daily_pnl_pct = (bot_state["daily_pnl"] / bot_state["start_balance"]) * 100
            
            logging.info(f"📊 Daily P&L: ${bot_state['daily_pnl']:.2f} ({daily_pnl_pct:+.2f}%) | Limit: {bot_state['daily_loss_limit']}%")
            
            # Limit aşıldı mı?
            if daily_pnl_pct <= bot_state["daily_loss_limit"] and not bot_state["daily_loss_hit"]:
                bot_state["daily_loss_hit"] = True
                
                # TÜM MODLARI KAPAT
                for mode_name in MODES.keys():
                    MODES[mode_name]["enabled"] = False
                
                # Telegram uyarısı
                send_telegram(
                    f"🚨 <b>GÜNLÜK KAYIP LİMİTİ!</b>\n"
                    f"💰 Günlük P&L: ${bot_state['daily_pnl']:.2f} ({daily_pnl_pct:.2f}%)\n"
                    f"📉 Limit: {bot_state['daily_loss_limit']}%\n"
                    f"⛔ TÜM MODLAR DURDURULDU!\n"
                    f"🔄 Yarın otomatik reset"
                )
                
                logging.warning(f"🚨 GÜNLÜK KAYIP LİMİTİ AŞILDI! {daily_pnl_pct:.2f}% <= {bot_state['daily_loss_limit']}%")
                logging.warning("⛔ TÜM MODLAR DURDURULDU!")
        
        # Loss tracking
        if pnl_usd < 0:
            track_loss(symbol, pnl_usd)
        
        # State'ten çıkar
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
    """Tüm pozisyonların trailing stop'unu güncelle"""
    try:
        session = get_session()
        
        for symbol, pos in list(bot_state["positions"].items()):
            try:
                # Güncel fiyat
                df = get_klines(symbol, limit=2)
                if df is None:
                    continue
                current_price = float(df["close"].iloc[-1])
                
                # PNL hesapla
                if pos["signal"] == "BUY":
                    pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                else:
                    pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                
                # TP kontrolü
                if pnl_pct >= pos["tp_percent"]:
                    close_position(symbol, f"TP Hit: {pnl_pct:.2f}%")
                    continue
                
                # SL kontrolü
                if pnl_pct <= pos["current_sl"]:
                    close_position(symbol, f"SL Hit: {pnl_pct:.2f}%")
                    continue
                
                # Trailing SL güncelle
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
                continue
                
    except Exception as e:
        logging.error(f"Update trailing error: {e}")

# =============================================================================
# MAIN BOT LOOP
# =============================================================================

def bot_loop():
    """Ana bot döngüsü"""
    logging.info("🤖 Bot loop started")
    
    loop_count = 0
    
    while True:
        try:
            # Bot çalışmıyorsa bekle
            if not bot_state["running"]:
                time.sleep(2)
                continue
            
            # Günlük reset kontrolü (gece 00:00)
            today = datetime.now().strftime("%Y-%m-%d")
            if bot_state["last_reset_date"] != today:
                bot_state["daily_pnl"] = 0.0
                bot_state["daily_loss_hit"] = False
                bot_state["start_balance"] = bot_state["balance"]
                bot_state["last_reset_date"] = today
                logging.info("🔄 DAILY RESET - New day started!")
                send_telegram("🔄 <b>GÜNLÜK RESET</b>\nYeni gün başladı, kayıp limiti sıfırlandı!")
            
            # Balance her loop'ta güncelle
            update_balance()
            
            # Her 10 loop'ta bir pozisyon sync (30 saniye)
            loop_count += 1
            if loop_count % 10 == 0:
                sync_positions()
            
            # GÜNLÜK KAYIP LİMİTİ AŞILDIYSA TARAMA YAPMA
            if bot_state["daily_loss_hit"]:
                time.sleep(10)
                continue
            
            # Her mod için tara (sadece enabled olanlar)
            for mode_name in ["SAFE", "MODERATE", "AGGRESSIVE"]:
                if MODES[mode_name]["enabled"]:
                    candidates = scan_coins(mode_name)
                    
                    # En iyi candidate'i trade et
                    if candidates:
                        best = candidates[0]
                        open_position(best["symbol"], best["signal"], mode_name)
            
            # Trailing stop güncelle
            update_trailing_stops()
            
            time.sleep(3)  # 3 saniye bekle (daha hızlı loop)
            
        except Exception as e:
            logging.error(f"Bot loop error: {e}")
            time.sleep(10)

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/status')
def api_status():
    # Position'ları PNL ile zenginleştir
    positions_with_pnl = []
    for symbol, pos in bot_state["positions"].items():
        try:
            df = get_klines(symbol, limit=2)
            if df is not None:
                current_price = float(df["close"].iloc[-1])
                qty = pos["qty"]
                
                if pos["signal"] == "BUY":
                    pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
                    pnl_usd = (current_price - pos["entry_price"]) * qty  # Gerçek PNL
                else:
                    pnl_pct = ((pos["entry_price"] - current_price) / pos["entry_price"]) * 100
                    pnl_usd = (pos["entry_price"] - current_price) * qty  # Gerçek PNL
                
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
    """Botu başlat"""
    bot_state["running"] = True
    
    # Balance ve pozisyonları sync et
    update_balance()
    sync_positions()
    
    # Günlük reset kontrolü
    today = datetime.now().strftime("%Y-%m-%d")
    if bot_state["last_reset_date"] != today:
        bot_state["daily_pnl"] = 0.0
        bot_state["daily_loss_hit"] = False
        bot_state["last_reset_date"] = today
        logging.info("🔄 Daily reset - new day started")
    
    # Start balance kaydet (eğer bugün ilk başlatma ise)
    if bot_state["start_balance"] == 0.0:
        bot_state["start_balance"] = bot_state["balance"]
    
    logging.info("🚀 Bot STARTED")
    logging.info(f"💰 Balance: ${bot_state['balance']:.2f}")
    logging.info(f"✅ Available: ${bot_state['available']:.2f}")
    logging.info(f"📊 Active positions: {len(bot_state['positions'])}")
    logging.info(f"📉 Daily P&L: ${bot_state['daily_pnl']:.2f} | Limit: {bot_state['daily_loss_limit']}%")
    
    send_telegram(f"🚀 <b>Bot STARTED</b>\n💰 ${bot_state['balance']:.2f}\n✅ ${bot_state['available']:.2f}\n📊 {len(bot_state['positions'])} positions\n📉 Daily P&L: ${bot_state['daily_pnl']:.2f}")
    
    return jsonify({"success": True, "running": True})

@app.route('/api/stop_bot', methods=['POST'])
def stop_bot():
    """Botu durdur"""
    bot_state["running"] = False
    logging.info("⏹️ Bot STOPPED")
    send_telegram("⏹️ <b>Bot STOPPED</b>")
    return jsonify({"success": True, "running": False})

@app.route('/api/toggle_mode', methods=['POST'])
def toggle_mode():
    data = request.json
    mode = data.get("mode")
    enable = data.get("enable")  # Opsiyonel: true/false
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
# HTML TEMPLATE (Minimal)
# =============================================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Bybit Bot v4.0</title>
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
        .control-buttons {
            display: flex;
            gap: 15px;
            justify-content: center;
            margin-top: 15px;
        }
        .btn-control {
            padding: 12px 40px;
            border: none;
            border-radius: 8px;
            font-size: 1.1em;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s;
        }
        .btn-start {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
        }
        .btn-start:hover { transform: scale(1.05); box-shadow: 0 0 20px rgba(16, 185, 129, 0.5); }
        .btn-stop {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
        }
        .btn-stop:hover { transform: scale(1.05); box-shadow: 0 0 20px rgba(239, 68, 68, 0.5); }
        
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: linear-gradient(135deg, rgba(30, 58, 138, 0.8) 0%, rgba(59, 130, 246, 0.6) 100%);
            padding: 20px;
            border-radius: 10px;
            text-align: center;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
            border: 1px solid rgba(59, 130, 246, 0.3);
        }
        .stat-value { 
            font-size: 2em; 
            font-weight: bold; 
            margin: 10px 0;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        .stat-label { font-size: 0.9em; opacity: 0.9; }
        .live { color: #10b981; font-size: 0.8em; }
        
        .modes {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
            margin-bottom: 20px;
        }
        .mode-card {
            background: linear-gradient(135deg, rgba(30, 58, 138, 0.6) 0%, rgba(59, 130, 246, 0.4) 100%);
            padding: 20px;
            border-radius: 10px;
            border: 2px solid rgba(59, 130, 246, 0.3);
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }
        .mode-card.active { 
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.6) 0%, rgba(5, 150, 105, 0.4) 100%);
            border: 2px solid #10b981;
            box-shadow: 0 0 20px rgba(16, 185, 129, 0.4);
        }
        .mode-title { font-size: 1.3em; font-weight: bold; margin-bottom: 10px; }
        .mode-toggle {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1em;
            font-weight: bold;
            width: 100%;
            margin-top: 10px;
            transition: all 0.3s;
        }
        .mode-toggle:hover { transform: scale(1.05); }
        .mode-toggle.off { 
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
        }
        .mode-info { font-size: 0.85em; margin: 5px 0; opacity: 0.95; }
        
        .positions {
            background: linear-gradient(135deg, rgba(30, 58, 138, 0.6) 0%, rgba(59, 130, 246, 0.4) 100%);
            padding: 20px;
            border-radius: 10px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
            border: 1px solid rgba(59, 130, 246, 0.3);
        }
        .positions h2 { margin-bottom: 15px; }
        .pos-table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(0,0,0,0.3);
            border-radius: 8px;
            overflow: hidden;
        }
        .pos-table th {
            background: rgba(59, 130, 246, 0.4);
            padding: 12px;
            text-align: left;
            font-weight: bold;
        }
        .pos-table td {
            padding: 12px;
            border-bottom: 1px solid rgba(59, 130, 246, 0.2);
        }
        .pos-table tr:hover {
            background: rgba(59, 130, 246, 0.2);
        }
        .pnl-positive { color: #4ade80; font-weight: bold; font-size: 1.1em; }
        .pnl-negative { color: #f87171; font-weight: bold; font-size: 1.1em; }
        .btn-close {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s;
        }
        .btn-close:hover { transform: scale(1.05); }
        .badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.85em;
            font-weight: bold;
        }
        .badge-safe { background: #10b981; }
        .badge-moderate { background: #f59e0b; }
        .badge-aggressive { background: #ef4444; }
        .empty { text-align: center; padding: 40px; opacity: 0.6; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Bybit Trading Bot</h1>
            <div class="version">v4.0 - Triple Auto Mode</div>
            <div class="control-buttons">
                <button class="btn-control btn-start" id="btn-start-bot" onclick="startBot()">🚀 START BOT</button>
                <button class="btn-control btn-stop" id="btn-stop-bot" onclick="stopBot()">⏹️ STOP BOT</button>
            </div>
            <div class="control-buttons" style="margin-top: 10px;">
                <button class="btn-control btn-start" onclick="enableAllModes()" style="font-size: 0.9em; padding: 8px 20px;">✅ Enable All Modes</button>
                <button class="btn-control btn-stop" onclick="disableAllModes()" style="font-size: 0.9em; padding: 8px 20px;">❌ Disable All Modes</button>
            </div>
        </div>
        
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
                        <th>Mode</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>P&L</th>
                        <th>SL/TP</th>
                        <th>Action</th>
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
                    let startBtn = document.getElementById('btn-start-bot');
                    let stopBtn = document.getElementById('btn-stop-bot');
                    if (data.running) {
                        startBtn.style.display = 'none';
                        stopBtn.style.display = 'block';
                    } else {
                        startBtn.style.display = 'block';
                        stopBtn.style.display = 'none';
                    }
                    
                    // Stats
                    document.getElementById('balance').textContent = '$' + data.balance.toFixed(2);
                    document.getElementById('available').textContent = '$' + data.available.toFixed(2);
                    document.getElementById('pos-count').textContent = data.positions.length;
                    document.getElementById('blacklist').textContent = data.blacklist_count;
                    
                    let winRate = data.stats.total_trades > 0 
                        ? Math.round((data.stats.winning_trades / data.stats.total_trades) * 100)
                        : 0;
                    document.getElementById('win-rate').textContent = winRate + '%';
                    document.getElementById('total-pnl').textContent = '$' + data.stats.total_pnl.toFixed(2);
                    
                    // Daily P&L (renkli)
                    let dailyPnl = data.daily_pnl || 0;
                    let dailyEl = document.getElementById('daily-pnl');
                    dailyEl.textContent = '$' + dailyPnl.toFixed(2);
                    if (dailyPnl >= 0) {
                        dailyEl.style.color = '#4ade80';
                    } else {
                        dailyEl.style.color = '#f87171';
                    }
                    
                    // Daily loss hit uyarısı
                    if (data.daily_loss_hit) {
                        dailyEl.parentElement.style.border = '2px solid #ef4444';
                        dailyEl.parentElement.style.animation = 'pulse 2s infinite';
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
                                <td>
                                    SL: ${pos.sl.toFixed(1)}% ${pos.trailing ? '📈' : ''}<br>
                                    TP: ${pos.tp.toFixed(1)}%
                                </td>
                                <td>
                                    <button class="btn-close" onclick="closePosition('${pos.symbol}')">Close</button>
                                </td>
                            </tr>
                        `).join('');
                    }
                });
        }
        
        function toggleMode(mode) {
            fetch('/api/toggle_mode', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode: mode})
            }).then(() => update());
        }
        
        function startBot() {
            fetch('/api/start_bot', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            }).then(() => update());
        }
        
        function stopBot() {
            if (!confirm('Stop bot? Active positions will remain open.')) return;
            fetch('/api/stop_bot', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            }).then(() => update());
        }
        
        function enableAllModes() {
            ['SAFE', 'MODERATE', 'AGGRESSIVE'].forEach(mode => {
                fetch('/api/toggle_mode', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: mode, enable: true})
                });
            });
            setTimeout(update, 500);
        }
        
        function disableAllModes() {
            ['SAFE', 'MODERATE', 'AGGRESSIVE'].forEach(mode => {
                fetch('/api/toggle_mode', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: mode, enable: false})
                });
            });
            setTimeout(update, 500);
        }
        
        function closePosition(symbol) {
            if (!confirm('Close ' + symbol + '?')) return;
            fetch('/api/close', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({symbol: symbol})
            }).then(() => update());
        }
        
        setInterval(update, 1000);  // ⚡ 1 saniye real-time update!
        update();
    </script>
</body>
</html>
"""

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"🚀 Bot starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

# =============================================================================
# LOGIN HTML
# =============================================================================
