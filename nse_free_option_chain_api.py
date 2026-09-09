"""
NSE Public API — Free Option Chain & Index Quotes
====================================================
A Flask service that pulls live NIFTY / BANK NIFTY / FIN NIFTY data from
NSE India's public website JSON endpoints — completely free, no broker
subscription needed.

IMPORTANT CAVEATS (read before relying on this in production)
---------------------------------------------------------------
- This is UNOFFICIAL. NSE does not publish this as a supported public API;
  it's the same JSON their own website's charts use. They can change the
  response shape or block frequent automated requests at any time.
- NSE requires a valid browser-like session (cookies) before the JSON
  endpoints will respond — hitting them cold returns 401/403. This file
  handles that by visiting the homepage first and reusing the session.
- Keep request frequency modest (a few requests per minute, not per
  second) to avoid getting temporarily blocked.
- SENSEX is a BSE index, not NSE, so it is NOT available through this
  free route. Only NIFTY, BANKNIFTY, FINNIFTY are supported here.
- For a public-facing production site, Dhan (₹499/mo) or Kite (₹500/mo)
  Data API plans are the reliable, ToS-compliant option — see the other
  backend files if you switch later.

SETUP
-----
1. Install dependencies:
     pip install flask requests flask-cors gunicorn --break-system-packages

2. Run it:
     python nse_free_option_chain_api.py

ENDPOINTS
---------
GET /api/option-chain?symbol=NIFTY&strikes=10
GET /api/index-quotes?symbols=NIFTY,BANKNIFTY,FINNIFTY
GET /api/health
"""

import datetime
import os
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

# NSE index-page symbol names vary from the API "symbol" query param —
# these map our friendly names to what each endpoint expects.
OPTION_CHAIN_SYMBOL = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
}
INDEX_DISPLAY_NAME = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
}
STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}

_session_cache = {"session": None, "created_at": 0}
SESSION_TTL_SECONDS = 240  # refresh cookies every ~4 minutes


def get_session():
    now = time.time()
    if _session_cache["session"] and (now - _session_cache["created_at"] < SESSION_TTL_SECONDS):
        return _session_cache["session"]

    s = requests.Session()
    s.headers.update(HEADERS)
    # Visiting the homepage first sets the cookies the API endpoints require.
    s.get(BASE, timeout=10)
    s.get(f"{BASE}/option-chain", timeout=10)
    _session_cache["session"] = s
    _session_cache["created_at"] = now
    return s


def nse_get(path, params=None):
    s = get_session()
    resp = s.get(f"{BASE}{path}", params=params, timeout=10)
    if resp.status_code != 200:
        # Session likely stale/blocked — force a fresh one and retry once.
        _session_cache["session"] = None
        s = get_session()
        resp = s.get(f"{BASE}{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def build_chain(symbol="NIFTY", num_strikes=10):
    nse_symbol = OPTION_CHAIN_SYMBOL[symbol]
    data = nse_get("/api/option-chain-indices", params={"symbol": nse_symbol})

    records = data.get("records", {})
    spot = records.get("underlyingValue")
    expiry_dates = records.get("expiryDates", [])
    nearest_expiry = expiry_dates[0] if expiry_dates else None
    rows = records.get("data", [])

    step = STEP.get(symbol, 50)
    atm = round(spot / step) * step if spot else None
    strikes_wanted = (
        set(atm + i * step for i in range(-num_strikes, num_strikes + 1)) if atm else None
    )

    chain_by_strike = {}
    for row in rows:
        if nearest_expiry and row.get("expiryDate") != nearest_expiry:
            continue
        strike = row.get("strikePrice")
        if strikes_wanted is not None and strike not in strikes_wanted:
            continue
        entry = chain_by_strike.setdefault(strike, {"CE": None, "PE": None})
        ce = row.get("CE")
        pe = row.get("PE")
        if ce:
            entry["CE"] = {
                "ltp": ce.get("lastPrice"),
                "oi": ce.get("openInterest"),
                "oi_change": ce.get("changeinOpenInterest"),
                "volume": ce.get("totalTradedVolume"),
            }
        if pe:
            entry["PE"] = {
                "ltp": pe.get("lastPrice"),
                "oi": pe.get("openInterest"),
                "oi_change": pe.get("changeinOpenInterest"),
                "volume": pe.get("totalTradedVolume"),
            }

    chain = [
        {"strike": strike, "CE": v["CE"], "PE": v["PE"]}
        for strike, v in sorted(chain_by_strike.items())
    ]

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
    if symbol not in OPTION_CHAIN_SYMBOL:
        return jsonify({"error": f"Unsupported symbol '{symbol}' (free NSE route supports NIFTY, BANKNIFTY, FINNIFTY only)"}), 400
    try:
        data = build_chain(symbol, num_strikes)
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/index-quotes")
def index_quotes():
    symbols_param = request.args.get("symbols", "NIFTY,BANKNIFTY,FINNIFTY")
    symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]

    unsupported = [s for s in symbols if s not in INDEX_DISPLAY_NAME]
    if unsupported:
        return jsonify(
            {"error": f"Unsupported symbol(s) in free NSE route: {', '.join(unsupported)} (SENSEX needs a different source)"}
        ), 400

    try:
        data = nse_get("/api/allIndices")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

    by_name = {row["index"]: row for row in data.get("data", [])}

    indices = []
    for s in symbols:
        nse_name = INDEX_DISPLAY_NAME[s]
        row = by_name.get(nse_name)
        if not row:
            continue
        indices.append(
            {
                "symbol": s,
                "label": nse_name,
                "ltp": row.get("last"),
                "change": row.get("variation"),
                "change_pct": row.get("percentChange"),
                "high": row.get("dayHigh"),
                "low": row.get("dayLow"),
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
