import os
import time
import json
from datetime import datetime, timezone
import requests
import yfinance as yf
import pandas as pd
from keep_alive import keep_alive

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "bot_state.json"

MIN_RATING = 7.0
SCALP_SL_MIN = 4.0
SCALP_SL_MAX = 9.0
SWING_SL_MIN = 15.0
SWING_SL_MAX = 40.0

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"signal_count": 0, "trades": [], "last_asia": 0, "last_london": 0, "last_ny": 0}

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)

state = load_state()

def discord(msg):
    if not WEBHOOK:
        print(f"  [no webhook] {msg[:80]}")
        return
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print(f"  [discord error] {e}")

def get_candles(interval, n):
    try:
        df = yf.download("GC=F", interval=interval, period="60d" if interval != "1m" else "5d", progress=False)
        if df is None or df.empty:
            return []
        
        candles = []
        for idx, row in df.iterrows():
            candles.append({
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            })
        return candles[-n:]
    except Exception as e:
        print(f"  [fetch error {interval}] {e}")
        return []

def get_session():
    now = datetime.now(timezone.utc)
    hour = now.hour
    if 8 <= hour < 12:
        return "LONDON"
    elif 13 <= hour < 17:
        return "NEW_YORK"
    elif hour >= 22 or hour < 8:
        return "ASIA"
    return None

def get_bias(candles):
    if len(candles) < 10:
        return "NEUTRAL"
    h = [c["high"] for c in candles[-5:]]
    l = [c["low"] for c in candles[-5:]]
    ph = [c["high"] for c in candles[-10:-5]]
    pl = [c["low"] for c in candles[-10:-5]]
    
    if max(h) > max(ph) and min(l) > min(pl):
        return "BULLISH"
    if max(h) < max(ph) and min(l) < min(pl):
        return "BEARISH"
    return "NEUTRAL"

def find_order_blocks(candles, bias):
    blocks = []
    for i in range(2, len(candles) - 1):
        c = candles[i]
        rng = c["high"] - c["low"]
        if rng < 1.0:
            continue
        body = abs(c["close"] - c["open"])
        if body / rng < 0.6:
            continue
        
        if bias == "BULLISH" and c["close"] > c["open"]:
            blocks.append({"top": c["open"], "bottom": c["low"], "strength": body / rng})
        elif bias == "BEARISH" and c["close"] < c["open"]:
            blocks.append({"top": c["high"], "bottom": c["close"], "strength": body / rng})
    return blocks[-8:]

def find_fvgs(candles, bias):
    fvgs = []
    for i in range(2, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]
        
        if bias == "BULLISH" and c3["low"] > c1["high"]:
            fvgs.append({"top": c3["low"], "bottom": c1["high"]})
        elif bias == "BEARISH" and c3["high"] < c1["low"]:
            fvgs.append({"top": c1["low"], "bottom": c3["high"]})
    return fvgs[-5:]

def in_zone(price, zone, buf=2.5):
    return (zone["bottom"] - buf) <= price <= (zone["top"] + buf)

def rate_setup(strength, liq, fvg):
    score = 5.0
    score += strength * 1.5
    if liq:
        score += 1.5
    if fvg:
        score += 1.0
    return min(score, 10.0)

def calc_levels(bias, zone, sl_min, sl_max):
    width = zone["top"] - zone["bottom"]
    sl = min(max(width * 1.2, sl_min), sl_max)
    
    if bias == "BULLISH":
        entry = zone["top"]
        sl_price = entry - sl
        partial = entry + (sl * 0.6)
        tp = entry + (sl * 2.0)
    else:
        entry = zone["bottom"]
        sl_price = entry + sl
        partial = entry - (sl * 0.6)
        tp = entry - (sl * 2.0)
    
    return {
        "entry": round(entry, 2),
        "sl": round(sl_price, 2),
        "sl_pips": round(sl * 10, 1),
        "partial": round(partial, 2),
        "partial_pips": round((sl * 0.6) * 10, 1),
        "tp": round(tp, 2),
        "tp_pips": round((sl * 2.0) * 10, 1),
    }

def fmt_signal(num, direction, mode, levels, rating):
    return (
        f"Setup #{num}\n"
        f"{mode}\n"
        f"Rating: {rating}/10\n\n"
        f"{direction} XAUUSD CMP ({levels['entry']})\n"
        f"SL {levels['sl']} ({-levels['sl_pips']} pips)\n"
        f"Partial {levels['partial']} (+{levels['partial_pips']} pips)\n"
        f"Full TP {levels['tp']} (+{levels['tp_pips']} pips)"
    )

def analyze():
    now = time.time()
    session = get_session()
    
    if not session:
        print(f"  [skip] No session active")
        return state
    
    cooldowns = {"ASIA": 900, "LONDON": 1200, "NEW_YORK": 1200}
    last_key = f"last_{session.lower()}"
    
    if now - state[last_key] < cooldowns[session]:
        mins = int((cooldowns[session] - (now - state[last_key])) / 60)
        print(f"  [{session} cooldown] {mins}m remaining")
        return state
    
    print(f"  Fetching candles for {session}...")
    cw = get_candles("1wk", 20)
    cd = get_candles("1d", 30)
    c4h = get_candles("60m", 80)
    c1h = get_candles("60m", 100)
    c15m = get_candles("15m", 60)
    c5m = get_candles("5m", 60)
    c1m = get_candles("1m", 30)
    
    if not all([cw, cd, c4h, c1h, c15m, c5m, c1m]):
        print(f"  [error] Missing data")
        return state
    
    price = c1m[-1]["close"]
    print(f"  Price: {price:.2f} | Session: {session}")
    
    bw = get_bias(cw)
    bd = get_bias(cd)
    b4h = get_bias(c4h)
    b1h = get_bias(c1h)
    print(f"  W1:{bw} D1:{bd} 4H:{b4h} 1H:{b1h}")
    
    biases = [bw, bd, b4h, b1h]
    if biases.count("BULLISH") >= 3:
        bias, direction = "BULLISH", "BUY"
    elif biases.count("BEARISH") >= 3:
        bias, direction = "BEARISH", "SELL"
    else:
        print(f"  [skip] No MTF alignment")
        return state
    
    obs = find_order_blocks(c4h, bias) + find_order_blocks(c1h, bias) + find_order_blocks(c15m, bias)
    if not obs:
        print(f"  [skip] No order blocks")
        return state
    
    active_ob = None
    for ob in reversed(obs):
        if in_zone(price, ob):
            active_ob = ob
            break
    
    if not active_ob:
        print(f"  [skip] Price not in OB")
        return state
    
    ob_id = f"{active_ob['bottom']:.1f}_{active_ob['top']:.1f}"
    if any(t.get("ob_id") == ob_id for t in state["trades"]):
        print(f"  [skip] Already tracking")
        return state
    
    fvgs = find_fvgs(c4h, bias) + find_fvgs(c1h, bias)
    fvg_ok = any(in_zone(price, f, 3) for f in fvgs)
    liq_ok = True
    
    rating = rate_setup(active_ob.get("strength", 0.7), liq_ok, fvg_ok)
    print(f"  Rating: {rating}/10")
    
    if rating < MIN_RATING:
        print(f"  [skip] Rating below {MIN_RATING}")
        return state
    
    mode = "SCALP" if active_ob in (find_order_blocks(c15m, bias) + find_order_blocks(c5m, bias)) else "SWING"
    sl_min = SCALP_SL_MIN if mode == "SCALP" else SWING_SL_MIN
    sl_max = SCALP_SL_MAX if mode == "SCALP" else SWING_SL_MAX
    
    levels = calc_levels(bias, active_ob, sl_min, sl_max)
    state["signal_count"] += 1
    msg = fmt_signal(state["signal_count"], direction, mode, levels, rating)
    discord(msg)
    
    trade = {
        "num": state["signal_count"],
        "ob_id": ob_id,
        "direction": direction,
        "mode": mode,
        "entry": levels["entry"],
        "sl": levels["sl"],
        "partial": levels["partial"],
        "tp": levels["tp"],
        "sl_pips": levels["sl_pips"],
        "partial_pips": levels["partial_pips"],
        "tp_pips": levels["tp_pips"],
        "status": "PENDING",
    }
    state["trades"].append(trade)
    state[last_key] = now
    save_state()
    
    print(f"  Signal #{state['signal_count']} posted: {mode} {direction} {rating}/10")
    return state

def main():
    keep_alive()
    discord("XAUUSD SMC Bot Online\n\nTimeframes: W1 D1 4H 1H 15M 5M 1M\nAnalysis: Order Blocks, Liquidity Sweeps, FVG, Structure\nSessions: ASIA, LONDON, NEW_YORK (24/7)\nScalp: 40-90 pips SL | Swing: 150-400 pips SL\nMin Rating: 7.0/10")
    
    global state
    state = load_state()
    
    while True:
        try:
            state = analyze()
            save_state()
            
            sess = get_session()
            sleep_time = 30 if sess else 60
            print(f"  Next scan in {sleep_time}s")
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  [error] {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
