
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

STATE_FILE = "state.json"
API = "https://api.kraken.com/0/public/OHLC"

RISK = 1.00
TARGET = 2.50
FEE = 0.0005
SLIPPAGE = 0.0002
EXPIRY = 6

def get_candles():
    params = urllib.parse.urlencode({
        "pair": "XBTUSD",
        "interval": 240
    })
    url = API + "?" + params

    req = urllib.request.Request(
        url, headers={"User-Agent": "BTC-Paper-Bot/1.0"}
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode())

    if data.get("error"):
        raise Exception(str(data["error"]))

    pair_key = [k for k in data["result"] if k != "last"][0]
    rows = data["result"][pair_key]

    # Kraken's final candle may still be open.
    now = datetime.now(timezone.utc).timestamp()
    rows = [r for r in rows if float(r[0]) + 4*3600 <= now]

    return [{
        "time": int(float(r[0])),
        "open": float(r[1]),
        "high": float(r[2]),
        "low": float(r[3]),
        "close": float(r[4])
    } for r in rows]

def add_indicators(candles):
    previous_close = None
    trs = []

    for i, c in enumerate(candles):
        if previous_close is None:
            tr = c["high"] - c["low"]
        else:
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - previous_close),
                abs(c["low"] - previous_close)
            )

        trs.append(tr)
        previous_close = c["close"]

        if i >= 13:
            c["atr"] = sum(trs[i-13:i+1]) / 14
        else:
            c["atr"] = None

        c["body"] = abs(c["close"] - c["open"])

def new_bot():
    return {
        "last_processed": None,
        "pending": None,
        "position": None,
        "trades": [],
        "signals": 0
    }

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)

    return {"bots": {"1.0": new_bot(), "0.9": new_bot()}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def finish_trade(bot, pos, exit_price, outcome, candle_time):
    qty = pos["qty"]

    gross = (
        (exit_price - pos["entry"]) * qty
        if pos["side"] == "BUY"
        else (pos["entry"] - exit_price) * qty
    )

    fees = (pos["entry"] + exit_price) * qty * FEE
    slip = (pos["entry"] + exit_price) * qty * SLIPPAGE

    bot["trades"].append({
        "side": pos["side"],
        "entry_time": pos["time"],
        "exit_time": candle_time,
        "entry": round(pos["entry"], 2),
        "exit": round(exit_price, 2),
        "quantity": round(qty, 8),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "slippage": round(slip, 4),
        "net": round(gross-fees-slip, 4),
        "result": outcome
    })

    bot["position"] = None

def process_candle(bot, c, p, index, threshold):

    # Manage open position first.
    pos = bot["position"]

    if pos:
        if pos["side"] == "BUY":
            hit_sl = c["low"] <= pos["sl"]
            hit_tp = c["high"] >= pos["tp"]
        else:
            hit_sl = c["high"] >= pos["sl"]
            hit_tp = c["low"] <= pos["tp"]

        if hit_sl:
            finish_trade(bot, pos, pos["sl"], "SL", c["time"])
        elif hit_tp:
            finish_trade(bot, pos, pos["tp"], "TP", c["time"])

    # Check pending entry.
    pending = bot["pending"]

    if bot["position"] is None and pending:
        if index > pending["index"] + EXPIRY:
            bot["pending"] = None

        elif index > pending["index"]:
            side = pending["side"]

            triggered = (
                c["high"] >= pending["entry"]
                if side == "BUY"
                else c["low"] <= pending["entry"]
            )

            if triggered:
                entry = pending["entry"]
                sl = pending["sl"]
                distance = abs(entry - sl)

                if distance > 0:
                    qty = RISK / distance
                    tp_distance = TARGET / qty

                    tp = (
                        entry + tp_distance
                        if side == "BUY"
                        else entry - tp_distance
                    )

                    bot["position"] = {
                        "side": side,
                        "entry": entry,
                        "sl": sl,
                        "tp": tp,
                        "qty": qty,
                        "time": c["time"]
                    }

                    bot["pending"] = None

                    # Conservative same-candle handling.
                    if side == "BUY":
                        hit_sl = c["low"] <= sl
                        hit_tp = c["high"] >= tp
                    else:
                        hit_sl = c["high"] >= sl
                        hit_tp = c["low"] <= tp

                    if hit_sl:
                        finish_trade(
                            bot, bot["position"], sl, "SL", c["time"]
                        )
                    elif hit_tp:
                        finish_trade(
                            bot, bot["position"], tp, "TP", c["time"]
                        )

    # Create a new signal only when free.
    if bot["position"] is None and bot["pending"] is None:

        if c["atr"] is None or c["atr"] <= 0:
            return

        if c["body"] < threshold * c["atr"]:
            return

        if (
            p["close"] < p["open"]
            and c["low"] < p["low"]
            and c["close"] > c["open"]
        ):
            bot["pending"] = {
                "side": "BUY",
                "entry": c["high"],
                "sl": c["low"],
                "index": index
            }
            bot["signals"] += 1

        elif (
            p["close"] > p["open"]
            and c["high"] > p["high"]
            and c["close"] < c["open"]
        ):
            bot["pending"] = {
                "side": "SELL",
                "entry": c["low"],
                "sl": c["high"],
                "index": index
            }
            bot["signals"] += 1

def main():
    candles = get_candles()

    if len(candles) < 20:
        raise Exception("Not enough 4H candles received")

    add_indicators(candles)
    state = load_state()

    print("BTC 4H PAPER BOT")
    print("Latest closed candle:",
          datetime.fromtimestamp(
              candles[-1]["time"], timezone.utc
          ))

    for label, threshold in [("1.0", 1.0), ("0.9", 0.9)]:
        bot = state["bots"][label]

        # First run starts from now; no historical trades.
        if bot["last_processed"] is None:
            bot["last_processed"] = candles[-1]["time"]
            print(f"Bot {label}: initialized; waiting for next candle.")
            continue

        start = next(
            (i for i, c in enumerate(candles)
             if c["time"] == bot["last_processed"]),
            None
        )

        if start is None:
            raise Exception(
                f"Saved candle not found for bot {label}; "
                "stopping to avoid skipping data."
            )

        for i in range(start + 1, len(candles)):
            process_candle(
                bot, candles[i], candles[i-1], i, threshold
            )
            bot["last_processed"] = candles[i]["time"]

        trades = bot["trades"]
        net = sum(t["net"] for t in trades)

        print("\nBOT", label)
        print("Signals:", bot["signals"])
        print("Completed trades:", len(trades))
        print("Net paper P/L: $", round(net, 2))
        print("Open position:", bot["position"])
        print("Pending entry:", bot["pending"])

        if trades:
            print("Last trade:", trades[-1])

    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print("\nState saved successfully.")

if __name__ == "__main__":
    main()
  
