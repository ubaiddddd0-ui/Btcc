"""
BTC futures daily-candle bot.  PAPER mode by default.

Setup:   pip install ccxt
Run:     python btc_daily_bot.py          (once a day, just after the 00:00 UTC daily close)
Cron:    5 0 * * * /usr/bin/python3 /path/btc_daily_bot.py
Live:    set PAPER = False and export BOT_API_KEY / BOT_API_SECRET
         (API key: trading only, NO withdrawal permission)

Your rules, as enforced in code:
 1) Risk 1% of equity per trade
 2) Stop goes past the prior candle's high/low
 3) Never add to a losing trade (no new entry while a position is open)
 4) 3 losses in a row -> 3 days off
 5) Position notional never above the account (max 1x)
Plus: Bitcoin futures only, daily candle only, max 2 new trades per day.

Strategy (simple, edit to taste):
 LONG  when last closed candle closes above the prior 20-day high AND above SMA50
 SHORT when last closed candle closes below the prior 20-day low  AND below SMA50
 Entry  = market at run time (logged as last close in paper mode)
 Stop   = beyond the signal candle's low (long) / high (short)
 Target = 2R
"""
import json, os, time, datetime as dt
import ccxt

# ---------------- config ----------------
PAPER = True
SYMBOL = "BTC/USDT:USDT"          # Binance USDT-M perpetual
EXCHANGE = "binanceusdm"
PAPER_START_EQUITY = 10_000.0
RISK_PCT = 0.01
RR = 2.0
LOOKBACK = 20
SMA_LEN = 50
MAX_TRADES_PER_DAY = 2
LOSS_STREAK_LIMIT = 3
DAYS_OFF = 3
STOP_BUFFER = 0.0005              # 0.05% beyond the prior high/low
STATE_FILE = "bot_state.json"
LOG_FILE = "bot_log.txt"
# ----------------------------------------

def log(msg):
    line = f"{dt.datetime.utcnow():%Y-%m-%d %H:%M:%S} UTC | {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"equity": PAPER_START_EQUITY, "position": None, "loss_streak": 0,
            "pause_until": None, "trades_today": 0, "trades_day": None}

def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)

def make_exchange():
    cfg = {"enableRateLimit": True}
    if not PAPER:
        cfg["apiKey"] = os.environ["BOT_API_KEY"]
        cfg["secret"] = os.environ["BOT_API_SECRET"]
    return getattr(ccxt, EXCHANGE)(cfg)

def closed_candles(ex):
    c = ex.fetch_ohlcv(SYMBOL, "1d", limit=SMA_LEN + LOOKBACK + 5)
    return c[:-1]                  # drop today's unfinished candle; [ts,o,h,l,c,v]

def get_equity(ex, s):
    if PAPER:
        return s["equity"]
    bal = ex.fetch_balance()
    return float(bal["USDT"]["total"])

# ---------- manage an open position (check last closed candle) ----------
def manage_position(ex, s, candles):
    p = s["position"]
    if not p:
        return
    for ts, o, h, l, c, v in candles:
        if ts <= p["opened_ts"]:
            continue
        hit_stop = l <= p["stop"] if p["side"] == "long" else h >= p["stop"]
        hit_tp = h >= p["target"] if p["side"] == "long" else l <= p["target"]
        if hit_stop or hit_tp:             # if both in one candle, assume stop first
            exit_px = p["stop"] if hit_stop else p["target"]
            pnl = (exit_px - p["entry"]) * p["size"] * (1 if p["side"] == "long" else -1)
            s["equity"] = s["equity"] + pnl if PAPER else get_equity(ex, s)
            if pnl < 0:
                s["loss_streak"] += 1
            else:
                s["loss_streak"] = 0
            log(f"CLOSED {p['side']} @ {exit_px:.2f} | PnL {pnl:+.2f} | "
                f"equity {s['equity']:.2f} | loss streak {s['loss_streak']}")
            if s["loss_streak"] >= LOSS_STREAK_LIMIT:
                until = (dt.date.today() + dt.timedelta(days=DAYS_OFF)).isoformat()
                s["pause_until"] = until
                s["loss_streak"] = 0
                log(f"RULE 4: {LOSS_STREAK_LIMIT} losses in a row. No trading until {until}")
            s["position"] = None
            return

# ---------- signal ----------
def get_signal(candles):
    closes = [x[4] for x in candles]
    last = candles[-1]
    prior = candles[-1 - LOOKBACK:-1]
    hh = max(x[2] for x in prior)
    ll = min(x[3] for x in prior)
    sma = sum(closes[-SMA_LEN:]) / SMA_LEN
    close = last[4]
    if close > hh and close > sma:
        return "long", last
    if close < ll and close < sma:
        return "short", last
    return None, last

def build_trade(side, sig_candle, entry, equity):
    high, low = sig_candle[2], sig_candle[3]
    if side == "long":
        stop = low * (1 - STOP_BUFFER)          # rule 2: past the prior low
        risk = entry - stop
        target = entry + RR * risk
    else:
        stop = high * (1 + STOP_BUFFER)         # rule 2: past the prior high
        risk = stop - entry
        target = entry - RR * risk
    if risk <= 0:
        return None
    size_risk = (RISK_PCT * equity) / risk      # rule 1
    size_cap = equity / entry                   # rule 5: notional <= account
    size = min(size_risk, size_cap)
    return {"side": side, "entry": entry, "stop": stop, "target": target,
            "size": size, "risk_usd": risk * size, "capped": size < size_risk}

def place_live(ex, t):
    size = float(ex.amount_to_precision(SYMBOL, t["size"]))
    buy, sell = ("buy", "sell") if t["side"] == "long" else ("sell", "buy")
    ex.create_order(SYMBOL, "market", buy, size)
    ex.create_order(SYMBOL, "STOP_MARKET", sell, size, None,
                    {"stopPrice": ex.price_to_precision(SYMBOL, t["stop"]), "reduceOnly": True})
    ex.create_order(SYMBOL, "TAKE_PROFIT_MARKET", sell, size, None,
                    {"stopPrice": ex.price_to_precision(SYMBOL, t["target"]), "reduceOnly": True})
    return size

# ---------- main ----------
def main():
    s = load_state()
    ex = make_exchange()
    today = dt.date.today().isoformat()
    if s["trades_day"] != today:
        s["trades_day"], s["trades_today"] = today, 0

    candles = closed_candles(ex)
    manage_position(ex, s, candles)

    if s["pause_until"] and today < s["pause_until"]:
        log(f"Paused until {s['pause_until']} (rule 4). No trades.")
        return save_state(s)
    s["pause_until"] = None

    if s["position"]:                            # rule 3: never add
        log("Position open. Holding. No new trades.")
        return save_state(s)
    if s["trades_today"] >= MAX_TRADES_PER_DAY:
        log("Max trades for today reached.")
        return save_state(s)

    side, sig = get_signal(candles)
    if not side:
        log("No signal today.")
        return save_state(s)

    equity = get_equity(ex, s)
    entry = ex.fetch_ticker(SYMBOL)["last"]
    t = build_trade(side, sig, entry, equity)
    if not t:
        log("Invalid risk distance. Skipping.")
        return save_state(s)

    log(f"SIGNAL {side.upper()} | ENTRY {t['entry']:.2f} | STOP {t['stop']:.2f} | "
        f"TARGET {t['target']:.2f} | size {t['size']:.4f} BTC | risk ${t['risk_usd']:.2f}"
        f"{' | size capped at 1x' if t['capped'] else ''}")

    if not PAPER:
        t["size"] = place_live(ex, t)
        log("LIVE orders placed (entry + stop + target).")
    else:
        log("PAPER trade recorded.")

    t["opened_ts"] = int(time.time() * 1000)
    s["position"] = t
    s["trades_today"] += 1
    save_state(s)

if __name__ == "__main__":
    main()
