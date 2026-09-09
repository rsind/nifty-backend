"""
Dhan-based Option Chain & Index Quotes API (raw REST version)
================================================================
A small Flask service that pulls live NIFTY/BANKNIFTY/SENSEX/FINNIFTY data
from Dhan's free trading API and exposes it as JSON, meant to be consumed
by the WordPress plugin (niftytrader-widgets).

WHY RAW REST INSTEAD OF THE dhanhq SDK
---------------------------------------
The dhanhq Python SDK (v2.2.0) has a known bug where quote_data() and
option_chain() fail with generic "status: failure" responses specifically
for INDEX instruments (IDX_I segment) — this is a documented issue in the
library, not an account/auth problem. Authentication itself works fine
(confirmed via get_fund_limits()). So this version calls Dhan's REST API
directly with `requests`, which sidesteps the SDK bug entirely.

SETUP
-----
1. Install dependencies (one time):
     pip install flask requests flask-cors gunicorn --break-system-packages

2. Get your Client ID + Access Token:
   Dhan app/web -> Profile -> DhanHQ Trading APIs -> generate access token.
   NOTE: Dhan access tokens expire daily — regenerate each trading day.

3. Set environment variables DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN
   (locally via your shell, or on Render's "Environment" tab).

4. Run it:
     python dhan_option_chain_api.py

ENDPOINTS
---------
GET /api/option-chain?symbol=NIFTY&strikes=10
GET /api/index-quotes?symbols=NIFTY,SENSEX,BANKNIFTY,FINNIFTY
GET /api/debug          -- sanity check that credentials work
GET /api/health
"""

import datetime
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "YOUR_DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "YOUR_DAILY_ACCESS_TOKEN")

BASE_URL = "https://api.dhan.co/v2"
HEADERS = {
    "access-token": ACCESS_TOKEN,
    "client-id": CLIENT_ID,
    "Content-Type": "application/json",
}

UNDERLYING = {
    "NIFTY":     {"security_id": 13, "segment": "IDX_I", "label": "NIFTY 50",   "step": 50},
    "BANKNIFTY": {"security_id": 25, "segment": "IDX_I", "label": "BANK NIFTY", "step": 100},
    "FINNIFTY":  {"security_id": 27, "segment": "IDX_I", "label": "FIN NIFTY",  "step": 50},
    "SENSEX":    {"security_id": 51, "segment": "IDX_I", "label": "BSE SENSEX", "step": 100},
}
# Double-check these security IDs against Dhan's instrument master CSV if
# something looks off: https://images.dhan.co/api-data/api-scrip-master.csv

app = Flask(__name__)
CORS(app)


def dhan_post(path, payload):
    resp = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=10)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw_text": resp.text}
    if resp.status_code != 200:
        raise RuntimeError(f"Dhan API {path} returned {resp.status_code}: {body}")
    return body


def get_expiry_list(symbol):
    info = UNDERLYING[symbol]
    body = dhan_post(
        "/optionchain/expirylist",
        {"UnderlyingScrip": info["security_id"], "UnderlyingSeg": info["segment"]},
    )
    return body.get("data", [])


def build_chain(symbol="NIFTY", num_strikes=10):
    info = UNDERLYING[symbol]
    expiries = get_expiry_list(symbol)
    if not expiries:
        return None
    nearest_expiry = expiries[0]

    body = dhan_post(
        "/optionchain",
        {
            "UnderlyingScrip": info["security_id"],
            "UnderlyingSeg": info["segment"],
            "Expiry": nearest_expiry,
        },
    )
    data = body.get("data", {})
    spot = data.get("last_price")
    oc = data.get("oc", {})

    step = info["step"]
    atm = round(spot / step) * step if spot else None
    strikes_wanted = (
        [atm + i * step for i in range(-num_strikes, num_strikes + 1)] if atm else []
    )

    chain = []
    for strike_str, row in sorted(oc.items(), key=lambda kv: float(kv[0])):
        strike = float(strike_str)
        if strikes_wanted and strike not in strikes_wanted:
            continue
        ce = row.get("ce") or {}
        pe = row.get("pe") or {}
        chain.append(
            {
                "strike": strike,
                "CE": {
                    "ltp": ce.get("last_price"),
                    "oi": ce.get("oi"),
                    "oi_change": (ce.get("oi", 0) - ce.get("previous_oi", ce.get("oi", 0)))
                    if ce
                    else None,
                    "volume": ce.get("volume"),
                }
                if ce
                else None,
                "PE": {
                    "ltp": pe.get("last_price"),
                    "oi": pe.get("oi"),
                    "oi_change": (pe.get("oi", 0) - pe.get("previous_oi", pe.get("oi", 0)))
                    if pe
                    else None,
                    "volume": pe.get("volume"),
                }
                if pe
                else None,
            }
        )

    return {
        "symbol": symbol,
        "spot": spot,
        "expiry": nearest_expiry,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "chain": chain,
    }


@app.route("/api/option-chain")
def option_chain():
    symbol = request.args.get("symbol", "NIFTY").upper()
    num_strikes = int(request.args.get("strikes", 10))
    if symbol not in UNDERLYING:
        return jsonify({"error": f"Unsupported symbol '{symbol}'"}), 400
    try:
        data = build_chain(symbol, num_strikes)
        if data is None:
            return jsonify({"error": "No expiry found"}), 500
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/index-quotes")
def index_quotes():
    symbols_param = request.args.get("symbols", "NIFTY,SENSEX,BANKNIFTY,FINNIFTY")
    symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]

    unknown = [s for s in symbols if s not in UNDERLYING]
    if unknown:
        return jsonify({"error": f"Unsupported symbol(s): {', '.join(unknown)}"}), 400

    by_segment = {}
    for s in symbols:
        info = UNDERLYING[s]
        by_segment.setdefault(info["segment"], []).append(info["security_id"])

    try:
        body = dhan_post("/marketfeed/quote", by_segment)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    data = body.get("data", {})
    if not isinstance(data, dict):
        return jsonify({"error": "Unexpected Dhan response", "details": body}), 502

    indices = []
    for s in symbols:
        info = UNDERLYING[s]
        segment_rows = data.get(info["segment"], {})
        row = segment_rows.get(str(info["security_id"])) or segment_rows.get(info["security_id"])
        if not row:
            continue
        ltp = row.get("last_price")
        ohlc = row.get("ohlc", {})
        prev_close = ohlc.get("close")
        change = round(ltp - prev_close, 2) if (ltp is not None and prev_close) else None
        change_pct = round((change / prev_close) * 100, 2) if change is not None and prev_close else 0
        indices.append(
            {
                "symbol": s,
                "label": info["label"],
                "ltp": ltp,
                "change": change,
                "change_pct": change_pct,
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
            }
        )

    return jsonify(
        {
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "indices": indices,
        }
    )


@app.route("/api/debug")
def debug():
    """Sanity check that credentials + base connectivity work."""
    try:
        resp = requests.get(f"{BASE_URL}/fundlimit", headers=HEADERS, timeout=10)
        return jsonify({"status_code": resp.status_code, "body": resp.json()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
