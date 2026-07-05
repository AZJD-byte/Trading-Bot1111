import os
import time
import json
from datetime import datetime, timezone
import requests
from twelvedata import TDClient
from keep_alive import keep_alive

client = TDClient(apikey=os.environ.get("TWELVE_DATA_API_KEY", ""))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "bot_state.json"

MIN_RATING = 7.0
MIN_RR = 2.0
PARTIAL_AT_R = 0.6

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
        print(f"  [no webhook] {msg[:100]}")
        return
    try:
        requests.post(WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        print(f"  [discord error] {e}")

def get_candles(interval, n):
    try:
        ts = client.time_series(
            symbol="XAU/USD",
            interval=interval,
            outputsize=n
        )
        
        # TwelveData returns a TimeSeries object
        # Convert it properly
        candles = []
        
        try:
            # Method 1: Try as_json()
            json_data = ts.as_json()
            if json_data and "values" in json_data:
                return json_data["values"]
        except:
            pass
        
        try:
            # Method 2: Try iterating directly
            for bar in ts:
                candles.append(bar)
            if candles:
                return candles
        except:
            pass
        
        return []
        
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
    recent_h = max(float(c["high"]) for c in candles[-5:])
    recent_l = min(float(c["low"]) for c in candles[-5:])
    prev_h = max(float(c["high"]) for c in candles[-10:-5])
    prev_l = min(float(c["low"]) for c in candles[-10:-5])
    
    if recent_h > prev_h and recent_l > prev_l:
        return "BULLISH"
    if recent_h < prev_h and recent_l < prev_l:
        return "BEARISH"
    return "NEUTRAL"

def find_order_blocks(candles, bias):
    blocks = []
    for i in range(2, len(candles) - 1):
        c = candles[i]
        rng = float(c["high"]) - float(c["low"])
        if rng < 1.0:
            continue
        body = abs(float(c["close"]) - float(c["open"]))
        if body / rng < 0.6:
            continue
        
        if bias == "BULLISH" and float(c["close"]) > float(c["open"]):
            blocks.append({
                "type": "DEMAND",
                "top": float(c["open"]),
                "bottom": float(c["low"]),
                "strength": body / rng,
            })
        elif bias == "BEARISH" and float(c["close"]) < float(c["open"]):
            blocks.append({
                "type": "SUPPLY",
                "top": float(c["high"]),
                "bottom": float(c["close"]),
                "strength": body / rng,
            })
    return blocks[-8:]

def find_fvgs(candles, bias):
    fvgs = []
    for i in range(2, len(candles)):
        c1 = candles[i - 2]
        c3 = candles[i]
        gap = float(c3["low"]) - float(c1["high"]) if bias == "BULLISH" else float(c1["low"]) - float(c3["high"])
        
        if bias == "BULLISH" and gap > 0:
            fvgs.append({"type": "BULLISH_FVG", "top": float(c3["low"]), "bottom": float(c1["high"])})
        elif bias == "BEARISH" and gap > 0:
            fvgs.append({"type": "BEARISH_FVG", "top": float(c1["low"]), "bottom": float(c3["high"])})
    return fvgs[-5:]

def check_liquidity_sweep(candles, bias):
    if len(candles) < 25:
        return False
    look = candles[-25:-5]
    recent = candles[-5:]
    
    lh = max(float(c["high"]) for c in look)
    ll = min(float(c["low"]) for c in look)
    rh = max(float(c["high"]) for c in recent)
    rl = min(float(c["low"]) for c in recent)
    price = float(recent[-1]["close"])
    
    if bias == "BULLISH" and rl < ll and price > ll:
        return True
    if bias == "BEARISH" and rh > lh and price < lh:
        return True
    return False

def in_zone(price, zone, buffer=2.5):
    return (zone["bottom"] - buffer) <= price <= (zone["top"] + buffer)

def rate_setup(obs_strength, liq_ok, fvg_present, mtf_aligned):
    score = 5.0
    score += obs_strength * 1.5
    if liq_ok:
        score += 1.5
    if fvg_present:
        score += 1.0
    if mtf_aligned:
        score += 1.5
    return min(score, 10.0)

def calc_levels(bias, zone, sl_min, sl_max):
    width = zone["top"] - zone["bottom"]
    sl = min(max(width * 1.2 + 0.5, sl_min), sl_max)
    
    if bias == "BULLISH":
        entry = zone["top"]
        sl_price = entry - sl
        partial = entry + (sl * PARTIAL_AT_R)
        tp = entry + (sl * MIN_RR)
    else:
        entry = zone["bottom"]
        sl_price = entry + sl
        partial = entry - (sl * PARTIAL_AT_R)
        tp = entry - (sl * MIN_RR)
    
    return {
        "entry": round(entry, 2),
        "sl": round(sl_price, 2),
        "sl_pips": round(sl * 10, 1),
        "partial": round(partial, 2),
        "partial_pips": round((sl * PARTIAL_AT_R) * 10, 1),
        "tp": round(tp, 2),
        "tp_pips": round((sl * MIN_RR) * 10, 1),
        "rr": round((sl * MIN_RR) / sl, 2),
    }

def fmt_signal(num, direction, mode, levels, rating, session):
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
    cw = get_candles("1W", 20)
    cd = get_candles("1D", 30)
    c4h = get_candles("4h", 80)
    c1h = get_candles("1h", 100)
    c15m = get_candles("15m", 60)
    c5m = get_candles("5m", 60)
    c1m = get_candles("1m", 30)
    
    if not all([cw, cd, c4h, c1h, c15m, c5m, c1m]):
        print(f"  [error] Missing data")
        return state
    
    price = float(c1m[-1]["close"])
    print(f"  Price: {price:.2f} | Session: {session}")
    
    bw = get_bias(cw)
    bd = get_bias(cd)
    b4h = get_bias(c4h)
    b1h = get_bias(c1h)
    print(f"  W1:{bw} D1:{bd} 4H:{b4h} 1H:{b1h}")
    
    aligned = [bw, bd, b4h, b1h].count("BULLISH") >= 3 or [bw, bd, b4h, b1h].count("BEARISH") >= 3
    if not aligned:
        print(f"  [skip] MTF not aligned")
        return state
    
    bias = "BULLISH" if [bw, bd, b4h, b1h].count("BULLISH") >= 3 else "BEARISH"
    direction = "BUY" if bias == "BULLISH" else "SELL"
    
    obs_4h = find_order_blocks(c4h, bias)
    obs_1h = find_order_blocks(c1h, bias)
    obs_15m = find_order_blocks(c15m, bias)
    all_obs = obs_4h + obs_1h + obs_15m
    
    if not all_obs:
        print(f"  [skip] No order blocks")
        return state
    
    active_ob = None
    for ob in reversed(all_obs):
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
    
    liq = check_liquidity_sweep(c4h, bias) or check_liquidity_sweep(c1h, bias)
    fvgs = find_fvgs(c4h, bias) + find_fvgs(c1h, bias)
    fvg_ok = any(in_zone(price, f, 3) for f in fvgs)
    
    rating = rate_setup(active_ob.get("strength", 0.7), liq, fvg_ok, True)
    print(f"  Rating: {rating}/10")
    
    if rating < MIN_RATING:
        print(f"  [skip] Rating below {MIN_RATING}")
        return state
    
    is_scalp = active_ob in (obs_15m + obs_5m) if obs_15m else False
    mode = "SCALP" if is_scalp else "SWING"
    sl_min = SCALP_SL_MIN if is_scalp else SWING_SL_MIN
    sl_max = SCALP_SL_MAX if is_scalp else SWING_SL_MAX
    
    levels = calc_levels(bias, active_ob, sl_min, sl_max)
    
    state["signal_count"] += 1
    msg = fmt_signal(state["signal_count"], direction, mode, levels, rating, session)
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

def monitor():
    global state
    c1m = get_candles("1m", 5)
    if not c1m:
        return state
    
    price = float(c1m[-1]["close"])
    
    still_pending = []
    for trade in state["trades"]:
        if trade["status"] != "PENDING":
            still_pending.append(trade)
            continue
        
        if in_zone(price, {"top": trade["entry"], "bottom": trade["entry"] - 5}, 2):
            discord(f"Setup #{trade['num']} Trade executed")
            trade["status"] = "ACTIVE"
        
        still_pending.append(trade)
    
    state["trades"] = still_pending
    
    still_active = []
    for trade in state["trades"]:
        if trade["status"] != "ACTIVE":
            still_active.append(trade)
            continue
        
        is_long = trade["direction"] == "BUY"
        
        if (is_long and price <= trade["sl"]) or (not is_long and price >= trade["sl"]):
            discord(f"Setup #{trade['num']} SL hit ({-trade['sl_pips']:.0f} pips)")
            continue
        
        if (is_long and price >= trade["partial"]) or (not is_long and price <= trade["partial"]):
            if not trade.get("partial_hit"):
                discord(f"Setup #{trade['num']} Partial hit (+{trade['partial_pips']:.0f} pips)")
                trade["partial_hit"] = True
        
        if (is_long and price >= trade["tp"]) or (not is_long and price <= trade["tp"]):
            discord(f"Setup #{trade['num']} Full TP hit (+{trade['tp_pips']:.0f} pips)")
            continue
        
        still_active.append(trade)
    
    state["trades"] = still_active
    return state

def startup():
    discord(
        "XAUUSD SMC Bot Online\n\n"
        "Timeframes: W1 D1 4H 1H 15M 5M 1M\n"
        "Analysis: Order Blocks, Liquidity Sweeps, FVG, Structure\n"
        "Sessions: ASIA, LONDON, NEW_YORK (24/7)\n"
        "Scalp: 40-90 pips SL | Swing: 150-400 pips SL\n"
        "Min Rating: 7.0/10"
    )

def main():
    keep_alive()
    startup()
    global state
    state = load_state()
    
    while True:
        try:
            state = analyze()
            state = monitor()
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
