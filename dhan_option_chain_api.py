"""
Dhan-based Option Chain & Index Quotes API
============================================
A small Flask service that pulls live NIFTY/BANKNIFTY/SENSEX/FINNIFTY data
from Dhan's free trading API and exposes it as JSON, meant to be consumed
by the WordPress plugin (niftytrader-widgets).

Why Dhan instead of Kite: Dhan's option-chain and quote APIs are free —
no separate market-data subscription like Kite's ₹500/month Connect plan.

SETUP
-----
1. Install dependencies (one time):
     pip install flask dhanhq flask-cors --break-system-packages

2. Get your Client ID + Access Token:
   Dhan app/web -> Profile -> DhanHQ Trading APIs -> generate access token.
   NOTE: Dhan access tokens expire daily — regenerate each trading day
   (or use the TOTP-based auto-refresh Dhan now supports, see their docs).

3. Fill in CLIENT_ID and ACCESS_TOKEN below.

4. Run it:
     python dhan_option_chain_api.py

   By default it runs on http://localhost:5000

5. Point the WordPress plugin's "API Base URL" setting to wherever this is
   hosted.

ENDPOINTS
---------
GET /api/option-chain?symbol=NIFTY&strikes=10
GET /api/index-quotes?symbols=NIFTY,SENSEX,BANKNIFTY,FINNIFTY

Response shapes are identical to the earlier Kite-based version, so the
WordPress plugin needs no changes.
"""

import datetime
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from dhanhq import dhanhq

# ---------------------------------------------------------------------------
# CONFIG — reads from environment variables (set these on your hosting
# platform's dashboard, e.g. Render's "Environment" tab) so you never have
# to put your real keys directly in the code.
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID", "YOUR_DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "YOUR_DAILY_ACCESS_TOKEN")

# Dhan security IDs for each underlying's index (used for LTP/OHLC + as the
# "underlying" for option chain calls). NSE index segment = "IDX_I".
UNDERLYING = {
    "NIFTY":     {"security_id": "13",    "segment": "IDX_I", "label": "NIFTY 50",   "step": 50},
    "BANKNIFTY": {"security_id": "25",    "segment": "IDX_I", "label": "BANK NIFTY", "step": 100},
    "FINNIFTY":  {"security_id": "27",    "segment": "IDX_I", "label": "FIN NIFTY",  "step": 50},
    "SENSEX":    {"security_id": "51",    "segment": "IDX_I", "label": "BSE SENSEX", "step": 100},
}
# NOTE: double-check these security IDs against Dhan's latest instrument
# master CSV (https://images.dhan.co/api-data/api-scrip-master.csv) —
# Dhan occasionally updates IDs, and SENSEX is on BSE (segment may differ).

app = Flask(__name__)
CORS(app)

dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)


def get_expiry_list(symbol):
    info = UNDERLYING[symbol]
    resp = dhan.expiry_list(
        under_security_id=int(info["security_id"]),
        under_exchange_segment=info["segment"],
    )
    return resp["data"]


def build_chain(symbol="NIFTY", num_strikes=10):
    info = UNDERLYING[symbol]
    expiries = get_expiry_list(symbol)
    if not expiries:
        return None
    nearest_expiry = expiries[0]

    chain_resp = dhan.option_chain(
        under_security_id=int(info["security_id"]),
        under_exchange_segment=info["segment"],
        expiry=nearest_expiry,
    )
    data = chain_resp.get("data", {})
    spot = data.get("last_price")
    oc = data.get("oc", {})  # dict keyed by strike price string

    step = info["step"]
    atm = round(spot / step) * step if spot else None
    strikes_wanted = (
        [atm + i * step for i in range(-num_strikes, num_strikes + 1)]
        if atm
        else []
    )

    chain = []
    for strike_str, row in sorted(oc.items(), key=lambda kv: float(kv[0])):
        strike = float(strike_str)
        if strikes_wanted and strike not in strikes_wanted:
            continue
        ce = row.get("ce", {})
        pe = row.get("pe", {})
        chain.append(
            {
                "strike": strike,
                "CE": {
                    "ltp": ce.get("last_price"),
                    "oi": ce.get("oi"),
                    "oi_change": ce.get("oi", 0) - ce.get("previous_oi", ce.get("oi", 0))
                    if ce
                    else None,
                    "volume": ce.get("volume"),
                }
                if ce
                else None,
                "PE": {
                    "ltp": pe.get("last_price"),
                    "oi": pe.get("oi"),
                    "oi_change": pe.get("oi", 0) - pe.get("previous_oi", pe.get("oi", 0))
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

    # Dhan's quote API wants security IDs grouped by exchange segment
    by_segment = {}
    for s in symbols:
        info = UNDERLYING[s]
        by_segment.setdefault(info["segment"], []).append(int(info["security_id"]))

    try:
        quote_resp = dhan.quote_data(securities=by_segment)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    quote_map = {}  # security_id (str) -> quote row
    for segment, rows in quote_resp.get("data", {}).items():
        for sec_id, row in rows.items():
            quote_map[sec_id] = row

    indices = []
    for s in symbols:
        info = UNDERLYING[s]
        row = quote_map.get(info["security_id"]) or quote_map.get(str(info["security_id"]))
        if not row:
            continue
        ltp = row.get("last_price")
        prev_close = row.get("ohlc", {}).get("close") or row.get("close_price")
        change = round(ltp - prev_close, 2) if (ltp is not None and prev_close) else None
        change_pct = round((change / prev_close) * 100, 2) if change is not None and prev_close else 0
        indices.append(
            {
                "symbol": s,
                "label": info["label"],
                "ltp": ltp,
                "change": change,
                "change_pct": change_pct,
                "high": row.get("ohlc", {}).get("high") or row.get("day_high"),
                "low": row.get("ohlc", {}).get("low") or row.get("day_low"),
            }
        )

    return jsonify(
        {
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "indices": indices,
        }
    )


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.datetime.now().isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
