#!/usr/bin/env python3
"""
XAUUSD Scalping Bot v5.1
=========================
✅ Data real-time dari MetaAPI History API
✅ Scan tiap 5 menit
✅ Trend + Pinbar WAJIB + min 1 dari 3 lainnya
✅ Notif Telegram + optional auto-execute
"""

import asyncio
import os
import logging
import aiohttp
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from metaapi_cloud_sdk import MetaApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

META_API_TOKEN  = os.environ["META_API_TOKEN"]
META_ACCOUNT_ID = os.environ["META_ACCOUNT_ID"]
NOTIF_BOT_TOKEN = os.environ["NOTIF_BOT_TOKEN"]
NOTIF_CHAT_ID   = os.environ["NOTIF_CHAT_ID"]

SYMBOL        = os.environ.get("SYMBOL", "XAUUSD")
TIMEFRAME     = os.environ.get("TIMEFRAME", "15m")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))
LOT_SIZE      = float(os.environ.get("LOT_SIZE", "0.01"))
RR            = float(os.environ.get("RR", "2.0"))
AUTO_EXECUTE  = os.environ.get("AUTO_EXECUTE", "false").lower() == "true"

EMA200_P = 200
MA99_P   = 99
BB_P     = 20
BB_DEV   = 2.0
ATR_P    = 14
VOL_MA_P = 20
VOL_MULT = 1.2
SR_LOOK  = 100

last_signal = {"direction": None, "price": 0.0}

async def send_notif(text: str):
    url = f"https://api.telegram.org/bot{NOTIF_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": NOTIF_CHAT_ID,
                "text": text,
                "parse_mode": "HTML"
            }) as resp:
                r = await resp.json()
                if not r.get("ok"):
                    log.error(f"Notif gagal: {r}")
    except Exception as e:
        log.error(f"Gagal kirim notif: {e}")

async def get_candles(account):
    try:
        # Pakai MetaApi History API
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=3)  # ambil 5 hari data

        candles = await account.get_historical_candles(
            symbol    = SYMBOL,
            timeframe = TIMEFRAME,
            start_time = start_time,
            limit     = 250
        )

        if not candles:
            raise Exception("Data kosong")

        rows = []
        for c in candles:
            rows.append({
                "time":   c.get("time"),
                "open":   float(c.get("open", 0)),
                "high":   float(c.get("high", 0)),
                "low":    float(c.get("low", 0)),
                "close":  float(c.get("close", 0)),
                "volume": float(c.get("tickVolume", 1)),
            })

        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        return df.sort_values("time").reset_index(drop=True)

    except Exception as e:
        raise Exception(f"Gagal ambil candles: {e}")

def compute_indicators(df):
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    df["ema200"]   = c.ewm(span=EMA200_P, adjust=False).mean()
    df["ma99"]     = c.rolling(MA99_P).mean()
    df["bb_mid"]   = c.rolling(BB_P).mean()
    bb_std         = c.rolling(BB_P).std()
    df["bb_upper"] = df["bb_mid"] + BB_DEV * bb_std
    df["bb_lower"] = df["bb_mid"] - BB_DEV * bb_std
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"]    = tr.rolling(ATR_P).mean()
    df["vol_ma"] = v.rolling(VOL_MA_P).mean()
    return df

def check_signal(df):
    df    = compute_indicators(df)
    i     = -2
    row   = df.iloc[i]
    close = row["close"]
    open_ = row["open"]
    high  = row["high"]
    low   = row["low"]
    atr   = row["atr"]
    r     = {}

    r["trend_up"]   = close > row["ema200"]
    r["trend_down"] = close < row["ema200"]
    r["vol_spike"]  = (row["vol_ma"] > 0) and (row["volume"] >= row["vol_ma"] * VOL_MULT)

    body     = abs(close - open_)
    upper_sh = high - max(close, open_)
    lower_sh = min(close, open_) - low
    min_body = atr * 0.05
    r["bull_pin"] = (body < min_body * 4) and (lower_sh >= body * 2) and (lower_sh > upper_sh)
    r["bear_pin"] = (body < min_body * 4) and (upper_sh >= body * 2) and (upper_sh > lower_sh)

    zone = atr * 0.3
    r["dyn_wall"] = (
        abs(low  - row["ma99"])     < zone or
        abs(high - row["ma99"])     < zone or
        abs(high - row["bb_upper"]) < zone or
        abs(low  - row["bb_lower"]) < zone
    )

    lookback     = df.iloc[i - SR_LOOK : i]
    sr_high      = lookback["high"].max()
    sr_low       = lookback["low"].min()
    r["near_sr"] = (abs(close - sr_high) < atr * 0.5 or abs(close - sr_low) < atr * 0.5)

    long_ok  = r["trend_up"]   and r["bull_pin"] and (r["vol_spike"] or r["dyn_wall"] or r["near_sr"])
    short_ok = r["trend_down"] and r["bear_pin"] and (r["vol_spike"] or r["dyn_wall"] or r["near_sr"])

    score = sum([r["trend_up"] or r["trend_down"], r["vol_spike"],
                 r["bull_pin"] or r["bear_pin"], r["dyn_wall"], r["near_sr"]])

    return {
        "long": long_ok, "short": short_ok,
        "score": score,  "price": close,
        "atr": atr,      "r": r,
        "time": df.iloc[i]["time"],
        "sl_dist": atr * 1.5,
    }

def format_signal(sig):
    price   = sig["price"]
    sl_dist = sig["sl_dist"]
    tp_dist = sl_dist * RR
    r       = sig["r"]

    if sig["long"]:
        direction, sl, tp = "LONG 🟢", price - sl_dist, price + tp_dist
    else:
        direction, sl, tp = "SHORT 🔴", price + sl_dist, price - tp_dist

    kondisi = (
        f"  • Trend EMA200   : {'✅' if r['trend_up'] or r['trend_down'] else '❌'}\n"
        f"  • Volume Anomaly : {'✅' if r['vol_spike'] else '❌'}\n"
        f"  • Pinbar         : {'✅' if r['bull_pin'] or r['bear_pin'] else '❌'}\n"
        f"  • Dynamic Wall   : {'✅' if r['dyn_wall'] else '❌'}\n"
        f"  • Static S/R     : {'✅' if r['near_sr'] else '❌'}"
    )

    msg = (
        f"🔥 <b>XAUUSD SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {sig['time'].strftime('%d/%m %H:%M')}\n"
        f"⏱ TF: {TIMEFRAME.upper()} | MetaAPI Live\n"
        f"📍 Type: <b>{direction}</b>\n\n"
        f"💰 Entry : <b>{price:.2f}</b>\n"
        f"🛑 SL    : <b>{sl:.2f}</b>\n"
        f"🎯 TP    : <b>{tp:.2f}</b>\n"
        f"📊 R:R   : 1:{RR}\n"
        f"💼 Lot   : {LOT_SIZE}\n\n"
        f"📋 <b>Kondisi:</b>\n{kondisi}\n\n"
        f"{'✅ Auto-execute ON' if AUTO_EXECUTE else '⚠️ Manual execute'}"
    )
    return msg, sl, tp

async def execute_order(connection, sig):
    price   = sig["price"]
    sl_dist = sig["sl_dist"]
    tp_dist = sl_dist * RR
    if sig["long"]:
        order_type = "ORDER_TYPE_BUY"
        sl, tp = price - sl_dist, price + tp_dist
    else:
        order_type = "ORDER_TYPE_SELL"
        sl, tp = price + sl_dist, price - tp_dist
    try:
        result = await connection.create_market_order(
            symbol=SYMBOL, volume=LOT_SIZE, order_type=order_type,
            stop_loss=round(sl, 2), take_profit=round(tp, 2),
            comment="GoldBot Scalping v5"
        )
        await send_notif(
            f"✅ <b>ORDER DIEKSEKUSI!</b>\n"
            f"Type: <b>{'BUY' if sig['long'] else 'SELL'}</b>\n"
            f"Entry: <b>{price:.2f}</b> | SL: <b>{sl:.2f}</b> | TP: <b>{tp:.2f}</b>"
        )
    except Exception as e:
        await send_notif(f"❌ <b>EXECUTE GAGAL!</b>\n<code>{str(e)[:200]}</code>")

async def run_bot():
    global last_signal

    await send_notif(
        f"🚀 <b>GOLDBOT SCALPING v5.1 ONLINE!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Pair     : <b>{SYMBOL}</b>\n"
        f"⏱ Timeframe: <b>{TIMEFRAME.upper()}</b>\n"
        f"💰 Lot      : <b>{LOT_SIZE}</b>\n"
        f"🔄 Scan     : tiap <b>{SCAN_INTERVAL//60} menit</b>\n"
        f"🤖 Execute  : <b>{'AUTO' if AUTO_EXECUTE else 'NOTIF ONLY'}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Bot siap scan market!"
    )

    api     = MetaApi(META_API_TOKEN)
    account = await api.metatrader_account_api.get_account(META_ACCOUNT_ID)
    if account.state not in ("DEPLOYED", "DEPLOYING"):
        await account.deploy()
    await account.wait_connected()

    # Untuk execute order, pakai RPC connection
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    log.info("MetaAPI connected!")

    while True:
        try:
            now = datetime.now().strftime("%H:%M:%S")
            # Ambil candles via account (History API)
            df  = await get_candles(account)
            sig = check_signal(df)

            if sig["long"] or sig["short"]:
                direction  = "LONG" if sig["long"] else "SHORT"
                price_diff = abs(sig["price"] - last_signal["price"])
                is_same    = (last_signal["direction"] == direction and price_diff < 2.0)

                if not is_same:
                    msg, sl, tp = format_signal(sig)
                    await send_notif(msg)
                    last_signal = {"direction": direction, "price": sig["price"]}
                    log.info(f"[{now}] SIGNAL: {direction} | Score {sig['score']}/5 | {sig['price']:.2f}")
                    if AUTO_EXECUTE:
                        await execute_order(connection, sig)
                else:
                    log.info(f"[{now}] Skip duplikat | {direction}")
            else:
                log.info(f"[{now}] No signal | Score {sig['score']}/5 | {sig['price']:.2f}")

        except Exception as e:
            log.error(f"Error: {e}")
            await send_notif(f"⚠️ <b>Bot Error:</b>\n<code>{str(e)[:150]}</code>")

        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run_bot())
