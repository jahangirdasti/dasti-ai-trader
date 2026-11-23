
import os
import time
import json
import hmac
import hashlib
import logging
import urllib.request
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

# ==========================================
# DASTI AI TRADER - BINANCE SPOT
# ==========================================

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
LIVE_TRADING = os.getenv("LIVE_TRADING", "NO").upper() == "YES"

SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"

TRADE_FRACTION = Decimal("0.20")  # 20% of free USDT
STOP_LOSS = Decimal("0.02")       # 2%
TAKE_PROFIT = Decimal("0.03")     # 3%
MAX_DAILY_LOSS = Decimal("0.03")  # 3%
CHECK_INTERVAL = 60

BASE_URL = "https://api.binance.com"
STATE_FILE = "dasti_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

def D(value):
    return Decimal(str(value))

def request_api(method, path, params=None, signed=False):
    params = params or {}
    headers = {"X-MBX-APIKEY": API_KEY} if API_KEY else {}

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            API_SECRET.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        query += "&signature=" + signature
    else:
        query = urllib.parse.urlencode(params)

    url = BASE_URL + path
    data = None

    if method == "GET" and query:
        url += "?" + query
    elif method == "POST":
        data = query.encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())

def get_market_data():
    return request_api(
        "GET",
        "/api/v3/klines",
        {"symbol": SYMBOL, "interval": "15m", "limit": 100}
    )

def ema(values, period):
    result = D(values[0])
    multiplier = D(2) / D(period + 1)
    for value in values[1:]:
        result = D(value) * multiplier + result * (1 - multiplier)
    return result

def rsi(values, period=14):
    changes = [D(values[i]) - D(values[i-1])
               for i in range(1, len(values))]
    gains = [max(x, D(0)) for x in changes[-period:]]
    losses = [max(-x, D(0)) for x in changes[-period:]]

    avg_gain = sum(gains) / D(period)
    avg_loss = sum(losses) / D(period)

    if avg_loss == 0:
        return D(100)

    rs = avg_gain / avg_loss
    return D(100) - (D(100) / (1 + rs))

def get_indicators():
    candles = get_market_data()
    closes = [D(c[4]) for c in candles[:-1]]

    if len(closes) < 30:
        raise RuntimeError("Not enough market data")

    fast = ema(closes[-50:], 9)
    slow = ema(closes[-50:], 21)
    strength = rsi(closes)

    return closes[-1], fast, slow, strength

def get_balances():
    account = request_api("GET", "/api/v3/account", signed=True)
    balances = {
        item["asset"]: D(item["free"])
        for item in account["balances"]
    }
    return balances

def get_equity(balances, price):
    return balances.get(QUOTE_ASSET, D(0)) + (
        balances.get(BASE_ASSET, D(0)) * price
    )

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {
        "position": None,
        "daily_date": "",
        "daily_start_equity": "0",
        "daily_realized_pnl": "0",
        "trades": []
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_symbol_rules():
    info = request_api(
        "GET",
        "/api/v3/exchangeInfo",
        {"symbol": SYMBOL}
    )

    filters = info["symbols"][0]["filters"]
    step = D("0.000001")
    minimum = D("5")

    for item in filters:
        if item["filterType"] == "LOT_SIZE":
            step = D(item["stepSize"])
        if item["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            minimum = D(item.get("minNotional", "5"))

    return step, minimum

def round_quantity(quantity, step):
    return (quantity / step).to_integral_value(
        rounding=ROUND_DOWN
    ) * step

def place_buy(usdt_amount):
    return request_api(
        "POST",
        "/api/v3/order",
        {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(usdt_amount)
        },
        signed=True
    )

def place_sell(quantity, step):
    quantity = round_quantity(quantity, step)

    if quantity <= 0:
        raise RuntimeError("Sell quantity is zero")

    return request_api(
        "POST",
        "/api/v3/order",
        {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": format(quantity, "f")
        },
        signed=True
    )

def order_price(order):
    qty = D(order.get("executedQty", "0"))
    quote = D(order.get("cummulativeQuoteQty", "0"))

    if qty <= 0:
        raise RuntimeError("Order has no executed quantity")

    return quote / qty, qty, quote

def main():
    if not API_KEY or not API_SECRET:
        raise RuntimeError(
            "Set BINANCE_API_KEY and BINANCE_API_SECRET first."
        )

    step, minimum = get_symbol_rules()
    state = load_state()

    logging.info("DASTI AI TRADER started.")
    logging.info("Symbol: %s | Live trading: %s", SYMBOL, LIVE_TRADING)

    while True:
        try:
            price, fast, slow, strength = get_indicators()
            balances = get_balances()
            equity = get_equity(balances, price)

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if state["daily_date"] != today:
                state["daily_date"] = today
                state["daily_start_equity"] = str(equity)
                state["daily_realized_pnl"] = "0"

            daily_start = D(state["daily_start_equity"])
            daily_pnl = D(state["daily_realized_pnl"])

            logging.info(
                "BTC=%s | EMA9=%s | EMA21=%s | RSI=%s | Equity=%s USDT",
                price, fast, slow, strength, equity
            )

            if daily_start > 0 and daily_pnl <= -daily_start * MAX_DAILY_LOSS:
                logging.warning("Daily loss limit reached. No new trades today.")
                save_state(state)
                time.sleep(CHECK_INTERVAL)
                continue

            position = state["position"]

            # MANAGE OPEN POSITION
            if position:
                entry = D(position["entry"])
                quantity = D(position["quantity"])
                stop = entry * (1 - STOP_LOSS)
                target = entry * (1 + TAKE_PROFIT)

                exit_signal = (
                    price <= stop
                    or price >= target
                    or fast < slow
                )

                if exit_signal:
                    if not LIVE_TRADING:
                        logging.warning("SELL signal detected. Live trading is OFF.")
                    else:
                        order = place_sell(quantity, step)
                        sell_price, sold_qty, received = order_price(order)

                        pnl = received - (sold_qty * entry)
                        state["daily_realized_pnl"] = str(
                            D(state["daily_realized_pnl"]) + pnl
                        )

                        state["trades"].append({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "side": "SELL",
                            "price": str(sell_price),
                            "quantity": str(sold_qty),
                            "estimated_pnl": str(pnl)
                        })

                        state["position"] = None
                        logging.info("SELL completed. Estimated P/L: %s USDT", pnl)

            # LOOK FOR BUY SIGNAL
            else:
                buy_signal = fast > slow and D(50) <= strength <= D(68)
                free_usdt = balances.get(QUOTE_ASSET, D(0))
                amount = free_usdt * TRADE_FRACTION

                if buy_signal and amount >= minimum:
                    if LIVE_TRADING:
                        order = place_buy(amount)
                        entry, quantity, spent = order_price(order)

                        state["position"] = {
                            "entry": str(entry),
                            "quantity": str(quantity),
                            "spent": str(spent)
                        }

                        state["trades"].append({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "side": "BUY",
                            "price": str(entry),
                            "quantity": str(quantity)
                        })

                        logging.info(
                            "BUY completed. BTC=%s at %s USDT",
                            quantity, entry
                        )
                    else:
                        logging.info(
                            "BUY signal detected. Live trading is OFF."
                        )

            save_state(state)

        except Exception as error:
            logging.exception("Trading cycle error: %s", error)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
