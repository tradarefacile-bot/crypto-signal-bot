"""
Crypto Signal Bot — Kraken
Strategia: EMA 9/21 crossover + Volume spike + ADX filter
Exchange: Kraken (dati + ordini)
Notifiche: Telegram con conferma manuale
Risk management: 2% del saldo per trade
"""

import os, json, asyncio, time, hashlib, hmac, base64, urllib.parse
import pandas as pd
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Variabili d'ambiente ──────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
KRAKEN_API_KEY   = os.environ["KRAKEN_API_KEY"]
KRAKEN_API_SECRET = os.environ["KRAKEN_API_SECRET"]
TRADES_FILE      = os.environ.get("TRADES_FILE", "/data/trades.json")

# ── Parametri strategia ───────────────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "LINKUSDT", "ADAUSDT", "AVAXUSDT"
]

KRAKEN_MAP = {
    "BTCUSDT":  "XBTUSD",  "ETHUSDT":  "ETHUSD",
    "SOLUSDT":  "SOLUSD",  "XRPUSDT":  "XRPUSD",
    "DOGEUSDT": "DOGEUSD", "LINKUSDT": "LINKUSD",
    "ADAUSDT":  "ADAUSD",  "AVAXUSDT": "AVAXUSD",
}

INTERVAL         = 15     # minuti
EMA_FAST         = 9
EMA_SLOW         = 21
VOLUME_MULT      = 2.0    # spike volume: 2x media
ADX_PERIOD       = 14
ADX_THRESHOLD    = 25     # >25 = trend, <25 = laterale
ATR_PERIOD       = 14
ATR_SL_MULT      = 1.5   # SL = prezzo ± 1.5x ATR
ATR_TP_MULT      = 3.0   # TP = prezzo ± 3.0x ATR  → R/R 1:2
RISK_PCT         = 0.02   # 2% del saldo per trade
SCAN_INTERVAL    = 60     # secondi tra scansioni
MONITOR_INTERVAL = 30     # secondi tra check SL/TP

# ── Stato in memoria ─────────────────────────────────────────────────────────
pending: dict  = {}   # segnali in attesa di conferma
positions: dict = {}  # posizioni aperte


# ═══════════════════════════════════════════════════════════════════════════════
# KRAKEN API
# ═══════════════════════════════════════════════════════════════════════════════

def _kraken_sign(path, data):
    post = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + post).encode()
    msg = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(KRAKEN_API_SECRET), msg, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def kraken_private(path, params=None):
    data = {"nonce": str(int(time.time() * 1000))}
    if params:
        data.update(params)
    headers = {"API-Key": KRAKEN_API_KEY, "API-Sign": _kraken_sign(path, data)}
    r = requests.post("https://api.kraken.com" + path, headers=headers, data=data, timeout=10)
    return r.json()

def kraken_public(path, params=None):
    r = requests.get("https://api.kraken.com" + path, params=params or {}, timeout=10)
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════════
# SALDO E ORDINI
# ═══════════════════════════════════════════════════════════════════════════════

def get_balance() -> float:
    """Legge il saldo disponibile su Kraken (USDC, USD, ZUSD)."""
    try:
        resp = kraken_private("/0/private/Balance")
        if resp.get("error"):
            logger.error(f"Balance error: {resp['error']}")
            return 0.0
        b = resp.get("result", {})
        logger.info(f"Kraken balances: {b}")
        val = float(b.get("ZUSD", 0) or b.get("USD", 0) or b.get("USDC", 0) or 0)
        return val
    except Exception as e:
        logger.error(f"get_balance error: {e}")
        return 0.0

def place_order(symbol: str, side: str, qty: float) -> dict:
    """Esegue un ordine market su Kraken."""
    pair = KRAKEN_MAP.get(symbol, symbol)
    resp = kraken_private("/0/private/AddOrder", {
        "ordertype": "market",
        "type": side.lower(),  # "buy" o "sell"
        "volume": str(qty),
        "pair": pair,
    })
    logger.info(f"Order {side} {symbol} qty={qty}: {resp}")
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# DATI DI MERCATO
# ═══════════════════════════════════════════════════════════════════════════════

def get_ohlc(symbol: str) -> pd.DataFrame | None:
    """Scarica candele OHLCV da Kraken."""
    try:
        pair = KRAKEN_MAP.get(symbol)
        if not pair:
            return None
        resp = kraken_public("/0/public/OHLC", {"pair": pair, "interval": INTERVAL})
        if resp.get("error"):
            logger.error(f"OHLC error {symbol}: {resp['error']}")
            return None
        key = [k for k in resp["result"] if k != "last"][0]
        data = resp["result"][key]
        df = pd.DataFrame(data, columns=["ts","open","high","low","close","vwap","volume","count"])
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        return df
    except Exception as e:
        logger.error(f"get_ohlc {symbol}: {e}")
        return None

def get_price(symbol: str) -> float | None:
    """Prezzo corrente da Kraken."""
    try:
        pair = KRAKEN_MAP.get(symbol)
        resp = kraken_public("/0/public/Ticker", {"pair": pair})
        key = list(resp["result"].keys())[0]
        return float(resp["result"][key]["c"][0])
    except Exception as e:
        logger.error(f"get_price {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# INDICATORI
# ═══════════════════════════════════════════════════════════════════════════════

def calc_atr(df: pd.DataFrame) -> float:
    h, l, c = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)
    return tr.rolling(ATR_PERIOD).mean().iloc[-1]

def calc_adx(df: pd.DataFrame) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    dm_p = h.diff().clip(lower=0)
    dm_m = (-l.diff()).clip(lower=0)
    dm_p = dm_p.where(dm_p > (-l.diff()), 0)
    dm_m = dm_m.where(dm_m > h.diff(), 0)
    atr_s  = tr.rolling(ADX_PERIOD).mean()
    di_p   = 100 * dm_p.rolling(ADX_PERIOD).mean() / atr_s
    di_m   = 100 * dm_m.rolling(ADX_PERIOD).mean() / atr_s
    dx     = 100 * (di_p - di_m).abs() / (di_p + di_m)
    return dx.rolling(ADX_PERIOD).mean().iloc[-1]

def check_signal(df: pd.DataFrame) -> tuple[str | None, float, float, float, float]:
    """
    Ritorna (signal, price, sl, tp, adx) oppure (None, ...) se nessun segnale.
    """
    df = df.copy()
    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()

    adx   = calc_adx(df)
    atr   = calc_atr(df)
    price = df["close"].iloc[-1]
    prev  = df.iloc[-2]
    curr  = df.iloc[-1]
    vol_spike = curr["volume"] > curr["vol_ma"] * VOLUME_MULT

    if adx < ADX_THRESHOLD:
        return None, price, 0, 0, adx

    if prev["ema_f"] < prev["ema_s"] and curr["ema_f"] > curr["ema_s"] and vol_spike:
        sl = price - ATR_SL_MULT * atr
        tp = price + ATR_TP_MULT * atr
        return "BUY", price, sl, tp, adx

    if prev["ema_f"] > prev["ema_s"] and curr["ema_f"] < curr["ema_s"] and vol_spike:
        sl = price + ATR_SL_MULT * atr
        tp = price - ATR_TP_MULT * atr
        return "SELL", price, sl, tp, adx

    return None, price, 0, 0, adx


# ═══════════════════════════════════════════════════════════════════════════════
# DIARIO TRADE
# ═══════════════════════════════════════════════════════════════════════════════

def load_trades() -> list:
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_trade(symbol, signal, entry, exit_price, qty, reason, entry_time, adx):
    try:
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        trades = load_trades()
        pnl = (exit_price - entry) * qty if signal == "BUY" else (entry - exit_price) * qty
        now = datetime.now()
        # Calcola durata
        duration = "N/A"
        try:
            dt = datetime.strptime(entry_time, "%d/%m/%Y %H:%M")
            mins = int((now - dt).total_seconds() / 60)
            duration = f"{mins//60}h {mins%60}m" if mins >= 60 else f"{mins}m"
        except Exception:
            pass
        trades.append({
            "symbol":      symbol,
            "signal":      signal,
            "entry_price": round(entry, 6),
            "exit_price":  round(exit_price, 6),
            "qty":         qty,
            "pnl":         round(pnl, 6),
            "pnl_pct":     round((pnl / (entry * qty)) * 100, 2) if entry * qty else 0,
            "reason":      reason,
            "entry_time":  entry_time,
            "exit_time":   now.strftime("%d/%m/%Y %H:%M"),
            "duration":    duration,
            "adx":         round(adx, 1) if adx else None,
        })
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
        logger.info(f"Trade salvato: {signal} {symbol} PnL={pnl:+.4f}")
    except Exception as e:
        logger.error(f"save_trade error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM — BOTTONI
# ═══════════════════════════════════════════════════════════════════════════════

async def send_signal(app, symbol, signal, price, sl, tp, adx):
    balance = get_balance()
    order_usdt = round(balance * RISK_PCT, 2)
    qty = round(order_usdt / price, 6)
    emoji = "🟢" if signal == "BUY" else "🔴"

    oid = f"{symbol}_{signal}_{int(time.time())}"
    pending[oid] = {
        "symbol": symbol, "signal": signal, "price": price,
        "sl": sl, "tp": tp, "adx": adx, "qty": qty,
        "entry_time": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "balance": balance,
    }

    text = (
        f"{emoji} *{signal} — {symbol}*\n\n"
        f"Prezzo:  `${price:,.4f}`\n"
        f"Stop Loss:  `${sl:,.4f}`\n"
        f"Take Profit: `${tp:,.4f}`\n"
        f"ADX: `{adx:.1f}` | TF: {INTERVAL}m\n"
        f"Saldo: `${balance:.2f}` → Ordine: `${order_usdt:.2f}` (2%)\n\n"
        f"Eseguire il trade?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Esegui", callback_data=f"exec_{oid}"),
        InlineKeyboardButton("❌ Salta",  callback_data=f"skip_{oid}"),
    ]])
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID, text=text,
        parse_mode="Markdown", reply_markup=kb
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, oid = query.data.split("_", 1)
    order = pending.pop(oid, None)

    if not order:
        await query.edit_message_text("⚠️ Segnale scaduto o già gestito.")
        return

    if action == "skip":
        await query.edit_message_text(f"⏭ Saltato: {order['signal']} {order['symbol']}")
        return

    # Esegui ordine
    resp = place_order(order["symbol"], order["signal"], order["qty"])
    errors = resp.get("error", [])
    if errors:
        await query.edit_message_text(f"❌ Errore Kraken:\n`{errors}`", parse_mode="Markdown")
        return

    # Registra posizione aperta
    positions[order["symbol"]] = {
        "signal":     order["signal"],
        "entry":      order["price"],
        "qty":        order["qty"],
        "sl":         order["sl"],
        "tp":         order["tp"],
        "adx":        order["adx"],
        "entry_time": order["entry_time"],
    }

    txid = resp.get("result", {}).get("txid", ["N/A"])[0]
    await query.edit_message_text(
        f"✅ *Ordine eseguito!*\n\n"
        f"{order['signal']} {order['symbol']} @ `${order['price']:,.4f}`\n"
        f"Qty: `{order['qty']}`\n"
        f"SL: `${order['sl']:,.4f}` | TP: `${order['tp']:,.4f}`\n"
        f"ID: `{txid}`",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR SL/TP
# ═══════════════════════════════════════════════════════════════════════════════

async def monitor_loop(app):
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        for symbol, pos in list(positions.items()):
            price = get_price(symbol)
            if price is None:
                continue
            hit_sl = (pos["signal"] == "BUY"  and price <= pos["sl"]) or \
                     (pos["signal"] == "SELL" and price >= pos["sl"])
            hit_tp = (pos["signal"] == "BUY"  and price >= pos["tp"]) or \
                     (pos["signal"] == "SELL" and price <= pos["tp"])
            if not (hit_sl or hit_tp):
                continue

            reason = "STOP LOSS" if hit_sl else "TAKE PROFIT"
            close_side = "sell" if pos["signal"] == "BUY" else "buy"
            resp = place_order(symbol, close_side, pos["qty"])
            errors = resp.get("error", [])
            if errors:
                logger.error(f"Chiusura {symbol} fallita: {errors}")
                continue

            del positions[symbol]
            save_trade(symbol, pos["signal"], pos["entry"], price,
                       pos["qty"], reason, pos["entry_time"], pos["adx"])

            pnl = (price - pos["entry"]) * pos["qty"]
            if pos["signal"] == "SELL":
                pnl = -pnl
            emoji = "🟢" if pnl >= 0 else "🔴"

            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"{'🛑' if hit_sl else '🎯'} *{reason}* — {symbol}\n\n"
                    f"Entry: `${pos['entry']:,.4f}` → Exit: `${price:,.4f}`\n"
                    f"{emoji} P&L: `${pnl:+.4f}`"
                ),
                parse_mode="Markdown"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

async def scan_loop(app):
    balance = get_balance()
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=(
            f"*Bot avviato*\n"
            f"Exchange: Kraken\n"
            f"Simboli: {', '.join(SYMBOLS)}\n"
            f"Strategia: EMA {EMA_FAST}/{EMA_SLOW} + Volume + ADX\n"
            f"Saldo: `${balance:.2f}` | Risk: 2%/trade\n"
            f"SL: ATR×{ATR_SL_MULT} | TP: ATR×{ATR_TP_MULT}"
        ),
        parse_mode="Markdown"
    )

    while True:
        logger.info("Scansione in corso...")
        for symbol in SYMBOLS:
            if symbol in positions:
                continue
            df = get_ohlc(symbol)
            if df is None or len(df) < 30:
                continue
            signal, price, sl, tp, adx = check_signal(df)
            if signal:
                logger.info(f"Segnale: {signal} {symbol} @ {price:.4f} ADX={adx:.1f}")
                await send_signal(app, symbol, signal, price, sl, tp, adx)
            else:
                if adx < ADX_THRESHOLD:
                    logger.info(f"ADX={adx:.1f} < {ADX_THRESHOLD} [{symbol}] — laterale")
            await asyncio.sleep(1)
        await asyncio.sleep(SCAN_INTERVAL)


# ═══════════════════════════════════════════════════════════════════════════════
# COMANDI TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════

def authorized(update: Update) -> bool:
    return str(update.effective_user.id) == str(TELEGRAM_CHAT_ID)

async def cmd_start(update: Update, context):
    if not authorized(update): return
    balance = get_balance()
    order_size = round(balance * RISK_PCT, 2)
    await update.message.reply_text(
        f"*Crypto Signal Bot*\n\n"
        f"Saldo: `${balance:.2f}`\n"
        f"Prossimo ordine: `${order_size:.2f}` (2%)\n"
        f"Posizioni aperte: `{len(positions)}`\n\n"
        f"/status — posizioni aperte\n"
        f"/trades — ultimi trade\n"
        f"/saldo — saldo aggiornato",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context):
    if not authorized(update): return
    if not positions:
        await update.message.reply_text("Nessuna posizione aperta.")
        return
    msg = "*Posizioni aperte:*\n\n"
    for sym, pos in positions.items():
        price = get_price(sym) or 0
        pnl = (price - pos["entry"]) * pos["qty"]
        if pos["signal"] == "SELL": pnl = -pnl
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg += (
            f"{emoji} *{pos['signal']} {sym}*\n"
            f"Entry: `${pos['entry']:,.4f}` → Now: `${price:,.4f}`\n"
            f"P&L: `${pnl:+.4f}` | SL: `${pos['sl']:,.4f}` | TP: `${pos['tp']:,.4f}`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_trades(update: Update, context):
    if not authorized(update): return
    trades = load_trades()
    if not trades:
        await update.message.reply_text("Nessun trade ancora nel diario.")
        return
    last5 = trades[-5:][::-1]
    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    msg = "*Ultimi trade:*\n\n"
    for t in last5:
        e = "🟢" if t["pnl"] >= 0 else "🔴"
        msg += f"{e} {t['signal']} {t['symbol']} `${t['pnl']:+.4f}` — {t['exit_time']}\n"
    msg += f"\nP&L totale: `${total_pnl:+.4f}` | Win rate: `{wins}/{len(trades)}`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_saldo(update: Update, context):
    if not authorized(update): return
    balance = get_balance()
    await update.message.reply_text(
        f"*Saldo Kraken*\n\n"
        f"USDC disponibile: `${balance:.2f}`\n"
        f"Prossimo ordine: `${round(balance * RISK_PCT, 2):.2f}` (2%)\n"
        f"Posizioni aperte: `{len(positions)}`",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    asyncio.create_task(scan_loop(app))
    asyncio.create_task(monitor_loop(app))

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("saldo",  cmd_saldo))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
