"""Single-file Paper Trading Replay Terminal.

Local: pip install Flask Flask-SQLAlchemy smartapi-python pyotp; python main.py
Render: set DATABASE_URL to Render PostgreSQL; install gunicorn and psycopg[binary];
        start with: gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT main:app
        Keep one web instance so its shared Angel One quote cache stays rate-limited.
Paper trading only. No broker orders are ever placed.
"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, time as clock_time, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import pyotp
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from SmartApi import SmartConnect
from werkzeug.security import check_password_hash, generate_password_hash

# Credentials requested for the Angel One SmartAPI connection.
API_KEY = "BAq0de8E"
CLIENT_ID = "S1263568"
PASSWORD = "7313"
TOTP_SECRET = "LY4QG5NPAOBDYIZXODIDWLQKN4"

INDICES = {
    "SENSEX": {"token": "99919000", "exchange": "BSE", "lot": 10},
    "NIFTY": {"token": "99926000", "exchange": "NSE", "lot": 50},
    "BANKNIFTY": {"token": "99926009", "exchange": "NSE", "lot": 15},
}
INTERVALS = {"ONE_MINUTE": "1m", "FIVE_MINUTE": "5m", "FIFTEEN_MINUTE": "15m"}
INTERVAL_SECONDS = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900}
IST = ZoneInfo("Asia/Kolkata")
SPEEDS = {1, 2, 5, 10, 25, 50}
TRIAL_SECONDS = 120

app = Flask(__name__)
render_runtime = bool(os.environ.get("RENDER"))
configured_database_url = os.environ.get("DATABASE_URL")
configured_secret_key = os.environ.get("SECRET_KEY")
if render_runtime and not configured_database_url:
    raise RuntimeError("Render deployment requires DATABASE_URL pointing to a persistent PostgreSQL database.")
if render_runtime and not configured_secret_key:
    raise RuntimeError("Render deployment requires a stable SECRET_KEY shared by every app instance.")
database_uri = configured_database_url or "sqlite:///final_app.db"
if database_uri.startswith("postgres://"):
    database_uri = "postgresql+psycopg://" + database_uri[len("postgres://"): ]
elif database_uri.startswith("postgresql://"):
    database_uri = "postgresql+psycopg://" + database_uri[len("postgresql://"): ]
engine_options = {}
if database_uri.startswith("sqlite:"):
    engine_options = {"connect_args": {"check_same_thread": False, "timeout": 30}}
app.config.update(
    SECRET_KEY=configured_secret_key or secrets.token_urlsafe(48),
    SQLALCHEMY_DATABASE_URI=database_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true" if render_runtime else "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
    MAX_CONTENT_LENGTH=64 * 1024,
)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
log = logging.getLogger("paper-replay")
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    valid_until = db.Column(db.Float, nullable=True)
    trial_started_at = db.Column(db.Float, nullable=True)
    auth_version = db.Column(db.Integer, nullable=False, default=0)


class TradingState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    symbol = db.Column(db.String(20), nullable=False, default="SENSEX")
    interval = db.Column(db.String(20), nullable=False, default="FIVE_MINUTE")
    selected_date = db.Column(db.String(10), nullable=False, default=lambda: datetime.now(IST).date().isoformat())
    real_data_json = db.Column(db.Text, nullable=False, default="[]")
    history_lookback_days = db.Column(db.Integer, nullable=False, default=1)
    candle_index = db.Column(db.Integer, nullable=False, default=0)
    candle_progress = db.Column(db.Float, nullable=False, default=0.0)
    last_tick_at = db.Column(db.Float, nullable=False, default=0.0)
    is_playing = db.Column(db.Boolean, nullable=False, default=True)
    playback_speed = db.Column(db.Integer, nullable=False, default=1)
    smooth_price = db.Column(db.Float, nullable=False, default=0.0)
    current_high = db.Column(db.Float, nullable=False, default=0.0)
    current_low = db.Column(db.Float, nullable=False, default=0.0)
    last_ltp = db.Column(db.Float, nullable=False, default=0.0)
    position = db.Column(db.String(8))
    position_label = db.Column(db.String(80), nullable=False, default="")
    entry_price = db.Column(db.Float, nullable=False, default=0.0)
    stop_loss = db.Column(db.Float, nullable=False, default=0.0)
    target_price = db.Column(db.Float, nullable=False, default=0.0)
    trailing_points = db.Column(db.Float, nullable=False, default=0.0)
    highest_price = db.Column(db.Float, nullable=False, default=0.0)
    lowest_price = db.Column(db.Float, nullable=False, default=0.0)
    lots = db.Column(db.Integer, nullable=False, default=1)
    unrealized_pnl = db.Column(db.Float, nullable=False, default=0.0)
    realized_pnl = db.Column(db.Float, nullable=False, default=0.0)
    trade_history_json = db.Column(db.Text, nullable=False, default="[]")
    needs_history_reload = db.Column(db.Boolean, nullable=False, default=True)
    secondary_symbol = db.Column(db.String(80), nullable=True)
    secondary_token = db.Column(db.String(32), nullable=True)
    secondary_exchange = db.Column(db.String(8), nullable=True)
    secondary_lot = db.Column(db.Integer, nullable=False, default=1)
    secondary_data_json = db.Column(db.Text, nullable=False, default="[]")
    secondary_needs_history_reload = db.Column(db.Boolean, nullable=False, default=True)
    secondary_last_index = db.Column(db.Integer, nullable=False, default=-1)
    secondary_active_time = db.Column(db.String(40), nullable=False, default="")
    secondary_smooth_price = db.Column(db.Float, nullable=False, default=0.0)
    secondary_high = db.Column(db.Float, nullable=False, default=0.0)
    secondary_low = db.Column(db.Float, nullable=False, default=0.0)


class RuntimeSetting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255), nullable=False)


class LiveCandle(db.Model):
    """Per-user candles captured from the live feed, available later in replay."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    underlying = db.Column(db.String(20), nullable=False)
    instrument_symbol = db.Column(db.String(80), nullable=False)
    exchange = db.Column(db.String(8), nullable=False)
    token = db.Column(db.String(32), nullable=False)
    interval = db.Column(db.String(20), nullable=False)
    trade_date = db.Column(db.String(10), nullable=False, index=True)
    candle_time = db.Column(db.Integer, nullable=False)
    open = db.Column(db.Float, nullable=False)
    high = db.Column(db.Float, nullable=False)
    low = db.Column(db.Float, nullable=False)
    close = db.Column(db.Float, nullable=False)


with app.app_context():
    db.create_all()
    # Lightweight additive migration for existing final_app.db deployments.
    user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
    state_columns = {column["name"] for column in inspect(db.engine).get_columns("trading_state")}
    with db.engine.begin() as connection:
        if "valid_until" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN valid_until DOUBLE PRECISION'))
        if "trial_started_at" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN trial_started_at DOUBLE PRECISION'))
        if "auth_version" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0'))
        state_migrations = {
            "history_lookback_days": "INTEGER NOT NULL DEFAULT 1",
            "secondary_symbol": "VARCHAR(80)", "secondary_token": "VARCHAR(32)",
            "secondary_exchange": "VARCHAR(8)", "secondary_lot": "INTEGER NOT NULL DEFAULT 1",
            "secondary_data_json": "TEXT NOT NULL DEFAULT '[]'",
            # TRUE is valid PostgreSQL Boolean syntax and SQLite also accepts it.
            "secondary_needs_history_reload": "BOOLEAN NOT NULL DEFAULT TRUE",
            "secondary_last_index": "INTEGER NOT NULL DEFAULT -1",
            "secondary_active_time": "VARCHAR(40) NOT NULL DEFAULT ''",
            "secondary_smooth_price": "FLOAT NOT NULL DEFAULT 0",
            "secondary_high": "FLOAT NOT NULL DEFAULT 0", "secondary_low": "FLOAT NOT NULL DEFAULT 0",
        }
        for column_name, sql_type in state_migrations.items():
            if column_name not in state_columns:
                connection.execute(text(f'ALTER TABLE "trading_state" ADD COLUMN "{column_name}" {sql_type}'))
    # Bootstrap the requested admin once. This also repairs an older final_app.db
    # whose admin credentials predate this build; later restarts preserve changes.
    if db.session.get(RuntimeSetting, "admin_bootstrap_v1") is None:
        admin_user = db.session.execute(db.select(User).filter_by(username="admin")).scalar_one_or_none()
        bootstrap_password = os.environ.get("ADMIN_PASSWORD", "admin123")
        if admin_user is None:
            admin_user = User(username="admin", password=generate_password_hash(bootstrap_password), is_admin=True, is_active=True)
            db.session.add(admin_user)
        else:
            admin_user.password = generate_password_hash(bootstrap_password)
            admin_user.is_admin = True
            admin_user.is_active = True
        db.session.add(RuntimeSetting(key="admin_bootstrap_v1", value="complete"))
        db.session.commit()

smart = SmartConnect(api_key=API_KEY)
smart_ready = False
smart_lock = threading.Lock()
state_lock = threading.RLock()
cache_lock = threading.RLock()
history_cache = {}
live_quote_cache = {}
live_quote_fetched_at = 0.0
live_quote_lock = threading.Lock()
live_candle_lock = threading.Lock()
live_candle_state = {}
live_history_cache = {}
option_master_cache = {"loaded_at": 0.0, "rows": []}
OPTION_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
OPTION_EXCHANGES = {"SENSEX": "BFO", "NIFTY": "NFO", "BANKNIFTY": "NFO"}
login_attempts = defaultdict(deque)
login_lock = threading.Lock()


def csrf_token():
    if not session.get("_csrf"):
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def get_state(user_id):
    state = db.session.execute(db.select(TradingState).filter_by(user_id=user_id)).scalar_one_or_none()
    if state is None:
        state = TradingState(user_id=user_id, selected_date=datetime.now(IST).date().isoformat())
        db.session.add(state)
        db.session.commit()
    return state


def rows_of(state):
    try:
        value = json.loads(state.real_data_json or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def history_of(state):
    try:
        value = json.loads(state.trade_history_json or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def ensure_smart_login():
    global smart_ready
    if smart_ready:
        return
    with smart_lock:
        if smart_ready:
            return
        answer = smart.generateSession(CLIENT_ID, PASSWORD, pyotp.TOTP(TOTP_SECRET).now())
        if not answer or not answer.get("status"):
            raise RuntimeError("SmartAPI login failed: " + str((answer or {}).get("message", "unknown error")))
        smart_ready = True
        log.info("SmartAPI login successful")


def fetch_candles(symbol, interval, selected_date, lookback_days=1):
    instrument = INDICES[symbol]
    return fetch_instrument_candles(instrument["exchange"], instrument["token"], interval, selected_date, lookback_days)


def max_history_days(interval):
    return {"ONE_MINUTE": 30, "FIVE_MINUTE": 100, "FIFTEEN_MINUTE": 200}.get(interval, 30)


def fetch_instrument_candles(exchange, token, interval, selected_date, lookback_days=1):
    try:
        lookback_days = max(1, min(int(lookback_days), max_history_days(interval)))
    except (TypeError, ValueError):
        lookback_days = 1
    key = (exchange, str(token), interval, selected_date, lookback_days)
    with cache_lock:
        if key in history_cache:
            return history_cache[key]
    day = date.fromisoformat(selected_date)
    now = datetime.now(IST)
    start_day = day - timedelta(days=lookback_days - 1)
    start = datetime.combine(start_day, clock_time(9, 15), tzinfo=IST)
    close = datetime.combine(day, clock_time(15, 30), tzinfo=IST)
    day_start = datetime.combine(day, clock_time(9, 15), tzinfo=IST)
    if lookback_days == 1 and day == now.date() and now < day_start:
        raise ValueError("Market opens at 09:15 IST. Select a past trading date or try after market open.")
    end = min(now, close) if day == now.date() else close
    params = {"exchange": exchange, "symboltoken": str(token),
              "interval": interval, "fromdate": f"{start_day.isoformat()} 09:15", "todate": end.strftime("%Y-%m-%d %H:%M")}
    ensure_smart_login()
    response = smart.getCandleData(params)
    if not response or not response.get("status"):
        global smart_ready
        smart_ready = False
        raise RuntimeError("SmartAPI historical request failed: " + str((response or {}).get("message", "unknown error")))
    result = []
    for row in response.get("data") or []:
        try:
            if len(row) < 5:
                continue
            stamp = datetime.fromisoformat(str(row[0])).isoformat()
            o, h, low, c = (float(row[i]) for i in range(1, 5))
            if min(o, h, low, c) <= 0 or h < max(o, low, c) or low > min(o, h, c):
                continue
            result.append([stamp, o, h, low, c, float(row[5] or 0) if len(row) > 5 else 0.0])
        except (TypeError, ValueError, IndexError):
            continue
    # Lightweight Charts rejects duplicate or descending timestamps. Normalize
    # the broker payload once before it reaches either chart or replay state.
    result.sort(key=lambda row: (datetime.fromisoformat(row[0]).replace(tzinfo=IST)
                                 if datetime.fromisoformat(row[0]).tzinfo is None
                                 else datetime.fromisoformat(row[0])).timestamp())
    unique = {}
    for row in result:
        stamp = datetime.fromisoformat(row[0])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        unique[int(stamp.timestamp())] = row
    result = [unique[stamp] for stamp in sorted(unique)]
    with cache_lock:
        history_cache[key] = result
    return result


def live_exchange_tokens(state):
    """Build one bounded live quote batch containing every index and active option."""
    groups = {"NSE": [], "BSE": [], "NFO": [], "BFO": []}
    for item in INDICES.values():
        groups[item["exchange"]].append(str(item["token"]))
    seen = {key: set(value) for key, value in groups.items()}
    # Prioritize this user's selected option, then other active contracts.
    candidates = [(state.secondary_exchange, state.secondary_token)]
    for other in db.session.scalars(db.select(TradingState)).all():
        candidates.append((other.secondary_exchange, other.secondary_token))
    count = sum(len(v) for v in groups.values())
    for exchange, token in candidates:
        if exchange not in groups or not token or str(token) in seen[exchange]:
            continue
        if count >= 50:
            break
        groups[exchange].append(str(token))
        seen[exchange].add(str(token))
        count += 1
    return {exchange: tokens for exchange, tokens in groups.items() if tokens}


def refresh_live_quotes(state):
    """Use Angel One's batched live quote endpoint, throttled to its 1 req/sec limit."""
    global live_quote_cache, live_quote_fetched_at, smart_ready
    with live_quote_lock:
        if live_quote_cache and time.monotonic() - live_quote_fetched_at < 1.0:
            return live_quote_cache
        ensure_smart_login()
        response = smart.getMarketData("FULL", live_exchange_tokens(state))
        if not response or not response.get("status"):
            smart_ready = False
            raise RuntimeError("Angel One live quote request failed: " + str((response or {}).get("message", "unknown error")))
        quotes = {}
        for quote in (response.get("data") or {}).get("fetched") or []:
            try:
                key = (str(quote["exchange"]).upper(), str(quote["symbolToken"]))
                price = float(quote.get("ltp") or 0)
                if price > 0:
                    quotes[key] = quote
            except (KeyError, TypeError, ValueError):
                continue
        if quotes:
            live_quote_cache.update(quotes)
        live_quote_fetched_at = time.monotonic()
        return live_quote_cache


def make_live_candle(user_id, exchange, token, interval, price, seed=None):
    """Accumulate exchange LTP updates into a synchronized interval candle."""
    period = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900}.get(interval, 60)
    now_epoch = int(time.time())
    bucket = now_epoch - (now_epoch % period)
    key = (user_id, exchange, str(token), interval, datetime.now(IST).date().isoformat())
    with live_candle_lock:
        current = live_candle_state.get(key)
        if current is None or current["time"] != bucket:
            if current is not None:
                opening = float(current["close"])
                high = low = opening
            elif seed and seed.get("time") == bucket:
                opening = float(seed["open"])
                high = max(float(seed["high"]), price)
                low = min(float(seed["low"]), price)
            else:
                opening = high = low = price
            current = {"time": bucket, "open": opening, "high": high, "low": low, "close": price}
            live_candle_state[key] = current
        else:
            current["high"] = max(float(current["high"]), price)
            current["low"] = min(float(current["low"]), price)
            current["close"] = price
        return dict(current)


def save_live_candle(user_id, underlying, instrument_symbol, exchange, token, interval, trade_date, candle):
    """Upsert the current live candle so ending live mode does not lose it."""
    with live_candle_lock:
        query = db.select(LiveCandle).filter_by(
            user_id=user_id, exchange=exchange, token=str(token), interval=interval,
            trade_date=trade_date, candle_time=int(candle["time"])
        )
        saved = db.session.execute(query).scalar_one_or_none()
        if saved is None:
            saved = LiveCandle(user_id=user_id, underlying=underlying,
                               instrument_symbol=instrument_symbol, exchange=exchange,
                               token=str(token), interval=interval, trade_date=trade_date,
                               candle_time=int(candle["time"]), open=candle["open"],
                               high=candle["high"], low=candle["low"], close=candle["close"])
            db.session.add(saved)
        else:
            saved.open = candle["open"]
            saved.high = candle["high"]
            saved.low = candle["low"]
            saved.close = candle["close"]


def merge_recorded_live_candles(rows, user_id, underlying, exchange, token, interval, trade_date):
    """Overlay locally captured live OHLC on broker history for replay."""
    recorded = db.session.scalars(db.select(LiveCandle).filter_by(
        user_id=user_id, underlying=underlying, exchange=exchange, token=str(token),
        interval=interval, trade_date=trade_date
    ).order_by(LiveCandle.candle_time)).all()
    if not recorded:
        return rows
    by_time = {}
    for row in rows:
        try:
            by_time[chart_candle(row)["time"]] = row
        except (TypeError, ValueError, IndexError):
            continue
    for item in recorded:
        stamp = datetime.fromtimestamp(item.candle_time, IST).isoformat()
        by_time[item.candle_time] = [stamp, item.open, item.high, item.low, item.close, 0.0]
    return [by_time[stamp] for stamp in sorted(by_time)]


def get_option_master():
    """Load Angel One's daily instrument master, cached for one hour."""
    with cache_lock:
        if option_master_cache["rows"] and time.time() - option_master_cache["loaded_at"] < 3600:
            return option_master_cache["rows"]
    req = urllib.request.Request(OPTION_MASTER_URL, headers={"User-Agent": "PaperReplayTerminal/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("Angel One instrument master returned an unexpected format.")
    with cache_lock:
        option_master_cache["rows"] = payload
        option_master_cache["loaded_at"] = time.time()
    return payload


def option_expiries(symbol):
    exchange = OPTION_EXCHANGES[symbol]
    values = set()
    for row in get_option_master():
        if str(row.get("exch_seg", "")).upper() != exchange:
            continue
        if str(row.get("name", "")).upper() != symbol or not str(row.get("symbol", "")).upper().endswith(("CE", "PE")):
            continue
        expiry = str(row.get("expiry", "")).upper()
        if expiry:
            values.add(expiry)
    return sorted(values, key=lambda value: datetime.strptime(value.title(), "%d%b%Y") if len(value) == 9 else datetime.strptime(value.title(), "%d%b%y"))


def option_chain_rows(symbol, expiry):
    exchange = OPTION_EXCHANGES[symbol]
    result = {}
    for item in get_option_master():
        if str(item.get("exch_seg", "")).upper() != exchange or str(item.get("name", "")).upper() != symbol:
            continue
        if str(item.get("expiry", "")).upper() != expiry.upper():
            continue
        contract = str(item.get("symbol", ""))
        side = "CE" if contract.upper().endswith("CE") else "PE" if contract.upper().endswith("PE") else ""
        if not side:
            continue
        try:
            raw_strike = float(item.get("strike", 0))
            strike = raw_strike / 100 if raw_strike >= 100000 else raw_strike
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        key = f"{strike:g}"
        row = result.setdefault(key, {"strike": strike, "CE": None, "PE": None})
        try:
            lot = int(item.get("lotsize", 0))
        except (TypeError, ValueError):
            lot = 0
        row[side] = {"symbol": contract, "token": str(item.get("token", "")), "lot_size": lot}
    return sorted(result.values(), key=lambda row: row["strike"])


def reset_bounds(state, rows):
    if rows and 0 <= state.candle_index < len(rows):
        op = float(rows[state.candle_index][1])
        state.smooth_price = state.current_high = state.current_low = op


def chart_candle(row):
    dt = datetime.fromisoformat(row[0])
    # SmartAPI returns market timestamps without a timezone. Treat them as IST
    # explicitly so the chart is identical on Windows, local Linux, and Render.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return {"time": int(dt.timestamp()), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])}


def secondary_chart_tick(state, primary_row, progress, client_bootstrap=False, lookback_days=1):
    if not state.secondary_token or not state.secondary_symbol:
        return None
    try:
        try:
            rows = json.loads(state.secondary_data_json or "[]")
        except (TypeError, json.JSONDecodeError):
            rows = []
        if not rows:
            try:
                rows = fetch_instrument_candles(state.secondary_exchange, state.secondary_token, state.interval,
                                                state.selected_date, lookback_days)
            except Exception:
                rows = []
        merged_rows = merge_recorded_live_candles(
            rows, state.user_id, state.symbol, state.secondary_exchange,
            state.secondary_token, state.interval, state.selected_date
        )
        if merged_rows != rows:
            rows = merged_rows
            state.secondary_data_json = json.dumps(rows, separators=(",", ":"))
            state.secondary_needs_history_reload = True
        elif not state.secondary_data_json or state.secondary_data_json == "[]":
            state.secondary_data_json = json.dumps(rows, separators=(",", ":"))
            state.secondary_needs_history_reload = True
        if not rows:
            return {"symbol": state.secondary_symbol, "error": "No historical or recorded live candles for this option contract/date."}
        market_time = datetime.fromisoformat(primary_row[0])
        index = -1
        for position, row in enumerate(rows):
            if datetime.fromisoformat(row[0]) <= market_time:
                index = position
            else:
                break
        if index < 0:
            return {"symbol": state.secondary_symbol, "exchange": state.secondary_exchange,
                    "interval_label": INTERVALS.get(state.interval, "5m"), "current_candle": None,
                    "history": [], "waiting": True}
        reload_secondary = state.secondary_needs_history_reload or client_bootstrap
        history = [chart_candle(row) for row in rows[:index]] if reload_secondary else []
        finalized = [] if reload_secondary else [
            chart_candle(item) for item in rows[max(0, state.secondary_last_index):index]
        ] if index > state.secondary_last_index else []
        state.secondary_needs_history_reload = False
        state.secondary_last_index = index
        row = rows[index]
        op, hi, low, close = map(float, row[1:5])
        progress = min(max(float(progress), 0.0), 1.0)
        if progress < 0.35:
            target = low if close >= op else hi
            guide = op + (target - op) * (progress / 0.35)
        elif progress < 0.75:
            start = low if close >= op else hi
            target = hi if close >= op else low
            guide = start + (target - start) * ((progress - 0.35) / 0.40)
        else:
            start = hi if close >= op else low
            guide = start + (close - start) * min((progress - 0.75) / 0.25, 1.0)
        if state.secondary_active_time != row[0] or state.secondary_smooth_price == 0:
            state.secondary_active_time = row[0]
            state.secondary_smooth_price = op
            state.secondary_high = state.secondary_low = op
        state.secondary_smooth_price += (guide - state.secondary_smooth_price) * 0.15
        price = round(max(low, min(hi, state.secondary_smooth_price)), 2)
        if progress >= 0.999:
            price = close
            state.secondary_high, state.secondary_low = hi, low
        else:
            state.secondary_high = round(max(state.secondary_high, op, price), 2)
            state.secondary_low = round(min(state.secondary_low, op, price), 2)
        candle = chart_candle(row)
        candle.update(high=state.secondary_high, low=state.secondary_low, close=price)
        return {"symbol": state.secondary_symbol, "exchange": state.secondary_exchange,
                "interval_label": INTERVALS.get(state.interval, "5m"), "history": history,
                "finalized_candles": finalized,
                "current_candle": candle, "waiting": False}
    except Exception as exc:
        log.exception("Secondary option replay failed")
        return {"symbol": state.secondary_symbol, "error": str(exc)}


def closed_trades(state, reason="Manual square off"):
    if not state.position:
        return 0.0
    size = INDICES.get(state.symbol, INDICES["SENSEX"])["lot"] * state.lots
    pnl = (state.last_ltp - state.entry_price) * size
    if state.position == "SHORT":
        pnl = (state.entry_price - state.last_ltp) * size
    pnl = round(pnl, 2)
    state.realized_pnl = round(state.realized_pnl + pnl, 2)
    trades = history_of(state)
    trades.insert(0, {"symbol": state.position_label or state.symbol, "type": state.position, "qty": size,
                      "entry": round(state.entry_price, 2), "exit": round(state.last_ltp, 2), "pnl": pnl,
                      "reason": reason, "closed_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")})
    state.trade_history_json = json.dumps(trades[:200])
    state.position = None
    state.entry_price = state.stop_loss = state.target_price = state.trailing_points = 0.0
    state.highest_price = state.lowest_price = state.unrealized_pnl = 0.0
    state.position_label = ""
    state.lots = 1
    return pnl


def has_full_access(user):
    return bool(user and (user.is_admin or (user.valid_until is not None and user.valid_until > time.time())))


def trial_left(user):
    if has_full_access(user):
        return TRIAL_SECONDS
    started = user.trial_started_at
    if started is None:
        return TRIAL_SECONDS
    return max(0, TRIAL_SECONDS - int(time.time() - float(started)))


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        user = db.session.get(User, user_id) if user_id else None
        if user is None or not user.is_active or session.get("auth_version") != user.auth_version:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(status="error", message="Please sign in again."), 401
            return redirect(url_for("login"))
        if request.path.startswith("/api/") and not has_full_access(user) and trial_left(user) <= 0:
            return jsonify(status="error", message="Access expired. Contact the administrator to renew your validity."), 403
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def csrf_guard():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf", "")
        supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            return (jsonify(status="error", message="Refresh the page and retry."), 400) if request.path.startswith("/api/") else ("Refresh the page and retry.", 400)


@app.before_request
def limit_login_attempts():
    if request.endpoint == "login" and request.method == "POST":
        ip, now = request.remote_addr or "unknown", time.time()
        with login_lock:
            attempts = login_attempts[ip]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return render_template_string(LOGIN_HTML, error="Too many attempts. Wait one minute.", csrf=csrf_token(), owner=OWNER_INFO_HTML), 429
            attempts.append(now)


@app.after_request
def headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


OWNER_INFO_HTML = """
<section class="owner"><div class="owner-avatar">SA</div><b>Sunny Dev Anand</b><span>Software Engineer</span>
<a href="mailto:itsmywayokok@gmail.com">✉ &nbsp; itsmywayokok@gmail.com</a>
<a href="https://wa.me/917667655215" target="_blank" rel="noopener">◉ &nbsp; +91 7667655215 · WhatsApp</a>
<small class="owner-caption">Paper Trading Replay Terminal</small></section>
"""

LOGIN_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Replay Terminal</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#131722;color:#d1d4dc;font:15px Arial}.box{width:min(420px,92vw);padding:30px;background:#1e222d;border:1px solid #2a2e39;border-radius:12px}h1{font-size:23px;margin:0 0 8px;color:white}.muted{color:#9598a1;font-size:13px;margin-bottom:20px}label{display:block;margin:14px 0 6px;font-size:12px}input{width:100%;padding:11px;background:#131722;border:1px solid #363a45;border-radius:5px;color:white}button{width:100%;padding:11px;margin-top:17px;background:#2962ff;color:white;border:0;border-radius:5px;font-weight:bold}.error{padding:9px;background:#3b2027;color:#ff8289}.link{text-align:center;margin:16px}.link a,.owner a{color:#8aa9ff}.owner{display:flex;flex-direction:column;gap:5px;text-align:center;padding:13px;margin-top:18px;background:#131722;border:1px solid #2a2e39;border-radius:7px;font-size:12px}.owner span{color:#9598a1} .owner-avatar{width:40px;height:40px;margin:0 auto;display:grid;place-items:center;border-radius:50%;background:linear-gradient(135deg,#2962ff,#8d57d6);color:#fff;font-weight:800;box-shadow:0 4px 14px #2962ff55}

@keyframes page-enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}body{animation:page-enter .42s cubic-bezier(.2,.7,.2,1) both}.box{animation:page-enter .48s cubic-bezier(.2,.7,.2,1) both}button{transition:transform .16s ease,filter .16s ease,box-shadow .16s ease}button:hover{filter:brightness(1.12);transform:translateY(-1px);box-shadow:0 7px 18px #2962ff30}button:active{transform:translateY(0)}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}</style></head><body><main class="box"><h1>Paper Trading Replay</h1><div class="muted">Sign in to your isolated replay workspace</div>{% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label>Username</label><input name="username" autocomplete="username" required maxlength="80"><label>Password</label><input name="password" type="password" autocomplete="current-password" required><button>Sign in</button></form><div class="link"><a href="{{ url_for('register') }}">Create account</a></div>{{ owner|safe }}</main></body></html>
"""

REGISTER_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create account</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#131722;color:#d1d4dc;font:15px Arial}main{width:min(420px,92vw);padding:30px;background:#1e222d;border:1px solid #2a2e39;border-radius:12px}label{display:block;margin:14px 0 6px}input{width:100%;padding:11px;background:#131722;border:1px solid #363a45;border-radius:5px;color:white}button{width:100%;padding:11px;margin-top:18px;background:#2962ff;color:white;border:0;border-radius:5px}.owner{display:flex;flex-direction:column;gap:5px;text-align:center;padding:13px;margin-top:18px;background:#131722;border:1px solid #2a2e39;border-radius:7px;font-size:12px}.owner a{color:#8aa9ff}.owner span{color:#9598a1} .owner-avatar{width:40px;height:40px;margin:0 auto;display:grid;place-items:center;border-radius:50%;background:linear-gradient(135deg,#2962ff,#8d57d6);color:#fff;font-weight:800;box-shadow:0 4px 14px #2962ff55}

@keyframes page-enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}body{animation:page-enter .42s cubic-bezier(.2,.7,.2,1) both}main{animation:page-enter .48s cubic-bezier(.2,.7,.2,1) both}button{transition:transform .16s ease,filter .16s ease,box-shadow .16s ease}button:hover{filter:brightness(1.12);transform:translateY(-1px);box-shadow:0 7px 18px #2962ff30}button:active{transform:translateY(0)}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}</style></head><body><main><h1>Create account</h1>{% if error %}<p>{{ error }}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label>Username</label><input name="username" minlength="3" maxlength="80" required><label>Password (10 characters minimum)</label><input name="password" type="password" minlength="10" required><button>Create account</button></form><p><a href="{{ url_for('login') }}">Back to sign in</a></p>{{ owner|safe }}</main></body></html>
"""

ADMIN_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{{ csrf }}"><title>Admin · Replay Terminal</title><style>
*{box-sizing:border-box}body{margin:0;background:#10131c;color:#e5e7ef;font:14px Arial,sans-serif}header{padding:16px 24px;background:#1e222d;border-bottom:1px solid #303644;display:flex;justify-content:space-between}header a{color:#a9baff;text-decoration:none}main{max-width:1200px;margin:28px auto;padding:0 18px}h1{margin-bottom:5px}.sub{color:#969baa;margin:0 0 20px}.table-wrap{overflow:auto;border:1px solid #303644;border-radius:12px;background:#1a1e28}table{width:100%;min-width:940px;border-collapse:collapse}th,td{text-align:left;padding:14px;border-bottom:1px solid #2b303b;vertical-align:middle}th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#9298a7}tr:last-child td{border-bottom:0}.username{font-size:15px;font-weight:700;color:#fff}.validity{margin-top:5px;color:#9ca3b2;font-size:12px}.grant{display:flex;gap:7px;align-items:center}.grant input{width:82px;padding:9px;background:#11151e;border:1px solid #394150;border-radius:6px;color:#fff}.btn{padding:9px 12px;background:#2962ff;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer;white-space:nowrap}.btn.secondary{background:#343a47}.status{display:inline-block;padding:5px 8px;border-radius:20px;background:#173b33;color:#55d5ae;font-size:11px}.status.off{background:#40242b;color:#ff8890}.password-result{margin-top:10px;min-width:235px;padding:9px;border:1px solid #42506a;background:#111724;border-radius:7px}.password-result small{display:block;color:#f3c66b;margin-bottom:6px}.password-result input{width:100%;padding:8px;background:#080b11;color:#b9d2ff;border:1px solid #30394a;border-radius:5px;font-family:monospace}.note{color:#9ca3b2;font-size:12px;margin:15px 0}.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);padding:11px 16px;background:#252b37;border:1px solid #485164;border-radius:7px;display:none} @media(max-width:700px){header{padding:13px}main{margin-top:18px}}

@keyframes page-enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}body{animation:page-enter .42s cubic-bezier(.2,.7,.2,1) both}tbody tr{transition:background .16s ease,transform .16s ease}tbody tr:hover{background:#232a37}button,.btn{transition:transform .16s ease,filter .16s ease,box-shadow .16s ease}button:hover,.btn:hover{filter:brightness(1.12);transform:translateY(-1px);box-shadow:0 6px 18px #2962ff26}button:active,.btn:active{transform:translateY(0)}.password-result{animation:page-enter .24s ease-out both}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}</style></head><body><header><b>Replay Terminal <span style="color:#839fff">· Admin Console</span></b><nav><a href="{{ url_for('home') }}">Terminal</a> &nbsp;·&nbsp; <a href="{{ url_for('logout') }}">Logout</a></nav></header><main><h1>User access & validity</h1><p class="sub">Issue or extend access for each registered username. The username never changes.</p><div class="table-wrap"><table><thead><tr><th>User</th><th>Access</th><th>Validity</th><th>Grant / extend</th><th>Account</th></tr></thead><tbody>{% for u in users %}<tr><td><span class="username">{{ u.username }}</span><div class="validity">ID #{{ u.id }}</div></td><td>{% if u.is_admin %}<span class="status">Administrator</span>{% elif u.valid_until and u.valid_until > now %}<span class="status">Full terminal</span>{% else %}<span class="status off">{{ 'Expired' if u.valid_until else 'Trial only' }}</span>{% endif %}<div id="secret-{{ u.id }}"></div></td><td>{% if u.valid_until %}<span id="validity-{{ u.id }}">{{ validity_labels[u.id] }}</span>{% else %}<span id="validity-{{ u.id }}">No paid access</span>{% endif %}</td><td>{% if not u.is_admin %}<form class="grant" data-user="{{ u.id }}" action="{{ url_for('grant_access',user_id=u.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label><input name="days" type="number" min="1" max="3650" value="30" aria-label="Validity days"></label><button class="btn">Generate pass + grant</button></form>{% else %}—{% endif %}</td><td>{% if not u.is_admin %}<form method="post" action="{{ url_for('toggle_user',user_id=u.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf }}"><button class="btn secondary">{{ 'Block' if u.is_active else 'Activate' }}</button></form>{% else %}<span class="status">Protected</span>{% endif %}</td></tr>{% endfor %}</tbody></table></div><p class="note">Generated passwords are shown once. Copy and share them securely. Reissuing a pass changes the user's password. Validity extends from the later of now or their current expiry.</p></main><div id="toast" class="toast"></div><script>const csrf=document.querySelector('meta[name="csrf-token"]').content;function notify(s){const t=document.getElementById('toast');t.textContent=s;t.style.display='block';setTimeout(()=>t.style.display='none',4000)}document.querySelectorAll('.grant').forEach(form=>form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;try{const days=Number(form.querySelector('[name="days"]').value);const r=await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify({days})});const d=await r.json();if(!r.ok||d.status!=='success')throw Error(d.message||'Could not issue access');const box=document.getElementById('secret-'+form.dataset.user);box.innerHTML='<div class="password-result"><small>Copy this new password now</small><input readonly value="'+d.password+'"><button type="button" class="btn secondary copy-pass" style="margin-top:6px;width:100%">Copy password</button></div>';box.querySelector('.copy-pass').addEventListener('click',()=>navigator.clipboard.writeText(box.querySelector('input').value).then(()=>notify('Copied')).catch(()=>notify('Copy the password manually')));document.getElementById('validity-'+form.dataset.user).textContent=d.valid_until;notify('Access granted; username unchanged.')}catch(err){notify(err.message)}finally{button.disabled=false}}));
</script></body></html>
"""

HTML_PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{{ csrf }}"><title>Paper Trading Replay Terminal</title><script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script><style>
:root{--bg:#131722;--panel:#1e222d;--border:#2a2e39;--text:#d1d4dc;--muted:#787b86}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Arial;height:100vh;overflow:hidden}.top{height:64px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;padding:0 10px;white-space:nowrap;overflow:auto}.brand{font-weight:bold;color:white}.top select,.top input,.btn{height:31px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:0 8px}.btn{cursor:pointer}.gap{flex:1}.layout{height:calc(100vh - 64px);display:grid;grid-template-columns:44px minmax(0,1fr) 290px;transition:grid-template-columns .24s ease}.layout.panel-collapsed{grid-template-columns:44px minmax(0,1fr)}.layout.panel-collapsed .right{display:none}.left{background:var(--panel);border-right:1px solid var(--border);display:flex;align-items:center;flex-direction:column;gap:8px;padding:10px 0}.icon{height:32px;width:32px;background:transparent;color:var(--text);border:1px solid transparent;cursor:pointer}.icon.active,.icon:hover{background:#2a2e39;border-color:#414755}.center{display:flex;flex-direction:column;min-width:0}.quote{height:58px;display:flex;align-items:center;gap:15px;padding:0 12px;border-bottom:1px solid var(--border)}.index-strip{height:29px;flex:0 0 29px;display:flex;align-items:center;gap:18px;padding:0 12px;background:#171b25;border-bottom:1px solid var(--border);overflow:auto;white-space:nowrap;font-size:11px;color:var(--muted)}.index-chip b{color:var(--text);margin-left:5px}.live-control{height:54px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;flex:0 0 auto}.live-clock{font-size:9px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;line-height:11px}.feed-status{margin-left:auto;padding:5px 8px;border:1px solid #454b58;border-radius:12px;color:#b0b7c3;font-size:10px;font-weight:bold}.feed-status.live{color:#45c7a5;border-color:#167b67;background:#0c2824}.feed-status.closed{color:#e2b464;border-color:#735a28;background:#292212}.sym,.price{font-weight:bold;color:white}.price{font-size:18px}.chart{flex:1;min-height:0}.bottom{height:28px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 10px;color:var(--muted);font-size:11px}.right{background:var(--panel);border-left:1px solid var(--border);min-height:0;display:flex;flex-direction:column}.title{padding:15px;border-bottom:1px solid var(--border);font-weight:bold;color:white}.body{padding:12px;overflow:auto;display:flex;flex-direction:column;gap:12px}.card{padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px}.inputs{display:grid;grid-template-columns:1fr 1fr;gap:8px}.inputs label{font-size:11px;color:var(--muted)}.inputs input{width:100%;margin-top:4px;padding:8px;background:var(--panel);border:1px solid var(--border);color:white;border-radius:4px}.actions{display:flex;gap:8px;margin-top:10px}.buy,.sell,.square{flex:1;padding:9px;border:0;border-radius:4px;color:white;font-weight:bold;cursor:pointer}.buy{background:#089981}.sell{background:#f23645}.square{background:#dd9226}.row{display:flex;justify-content:space-between;margin:8px 0}.row span{color:var(--muted)}.history{display:flex;flex-direction:column;gap:6px;margin-top:8px}.item{display:flex;justify-content:space-between;padding:8px;background:var(--panel);border-radius:4px;font-size:11px}.green{color:#089981}.red{color:#f23645}.toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%);display:none;background:#252a36;border:1px solid #454b58;border-radius:6px;padding:10px;z-index:10}.overlay{position:fixed;inset:0;background:#000c;display:none;align-items:center;justify-content:center;z-index:20}.overlaybox{max-width:430px;width:92vw;padding:25px;background:var(--panel);border:1px solid var(--border);border-radius:12px;text-align:center}.owner{display:flex;flex-direction:column;align-items:center;gap:7px;margin-top:15px;padding:15px;border:1px solid var(--border);border-radius:9px;text-align:center}.owner a{color:#9db5ff;text-decoration:none}.owner a:hover{text-decoration:underline}.owner span,.owner-caption{color:var(--muted)}.owner-avatar{width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,#2962ff,#8d57d6);color:#fff;font-weight:800;font-size:15px;box-shadow:0 4px 16px #2962ff55}.owner-panel{background:linear-gradient(145deg,#1b2940,#11151e);border-color:#34517d}.owner-title{font-weight:700;color:#f2c66d;margin-bottom:10px}.owner-panel .owner{margin-top:0;background:#0e1420aa;border-color:#354765}.owner-panel .owner b{color:#fff}body.light{--bg:#fff;--panel:#f4f5f8;--border:#e0e3eb;--text:#131722;--muted:#667085}body.light .brand,body.light .sym,body.light .title{color:#131722}body.light .inputs input,body.light .item{background:#fff;color:#131722}@media(max-width:900px){.layout{grid-template-columns:40px minmax(0,1fr)}.right{display:none}}
.charts{flex:1;min-height:0;min-width:0;width:100%;display:grid;grid-template-rows:minmax(0,1fr)}.chart-pane{display:none;min-width:0;min-height:0;overflow:hidden;flex-direction:column;position:relative}.chart-pane.primary{display:flex}.charts.split{grid-template-columns:minmax(0,calc(var(--primary-share,50%) - 3px)) 6px minmax(0,1fr);grid-template-rows:minmax(0,1fr)}.charts.split .chart-pane.secondary{display:flex}.chart-divider{display:none;touch-action:none;user-select:none;background:#2a2e39;cursor:col-resize;position:relative;z-index:3}.charts.split .chart-divider{display:block}.chart-divider:hover,.chart-divider.dragging{background:#2962ff}.chart-divider:after{content:"";position:absolute;inset:0 -4px;cursor:col-resize}.chart-pane-title{height:27px;flex:0 0 27px;padding:6px 10px;background:#181c26;border-top:1px solid #2a2e39;color:#aeb4c0;font-size:11px;display:flex;align-items:center;justify-content:space-between}.chart-pane-title button{border:0;background:transparent;color:#9ca3b2;cursor:pointer}.chart{flex:1;min-height:0;width:100%}.chart-placeholder{position:absolute;inset:27px 0 0;display:flex;align-items:center;justify-content:center;color:#7b8290;background:#131722;pointer-events:none;text-align:center;padding:24px}.oc-modal{position:fixed;inset:0;background:#05070bcc;display:none;align-items:center;justify-content:center;padding:18px;z-index:30}.oc-box{width:min(1100px,96vw);height:min(82vh,850px);display:flex;flex-direction:column;background:#1e222d;border:1px solid #343a49;border-radius:12px;box-shadow:0 24px 80px #0009}.oc-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid #2a2e39}.oc-head strong{font-size:16px;color:#fff}.oc-head select{background:#131722;color:#d1d4dc;border:1px solid #363a45;border-radius:5px;padding:8px}.oc-spacer{flex:1}.oc-close{background:#303644;color:#d1d4dc;border:0;border-radius:5px;padding:8px 11px;cursor:pointer}.oc-note{padding:9px 16px;color:#9298a7;font-size:11px;border-bottom:1px solid #2a2e39}.oc-table-wrap{overflow:auto;flex:1}.oc-table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}.oc-table th{position:sticky;top:0;background:#191d27;color:#9ca3b2;font-size:11px;letter-spacing:.07em;text-transform:uppercase;z-index:1}.oc-table td,.oc-table th{text-align:center;padding:8px 10px;border-bottom:1px solid #292e39}.oc-table td:nth-child(2){font-weight:700;color:#fff;background:#202532}.oc-ce{color:#45c7a5;text-align:right!important}.oc-pe{color:#ff7882;text-align:left!important}.oc-contract{font-family:monospace;font-size:11px}.oc-select{padding:0;background:transparent;border:0;color:inherit;cursor:pointer;text-decoration:underline;text-decoration-color:#ffffff33;text-underline-offset:3px}.oc-select:hover{color:#fff;text-decoration-color:currentColor}.oc-select{padding:0;background:transparent;border:0;color:inherit;cursor:pointer;text-decoration:underline;text-decoration-color:#ffffff33;text-underline-offset:3px}.oc-select:hover{color:#fff;text-decoration-color:currentColor}.oc-empty{padding:30px;text-align:center;color:#a0a6b3}.oc-btn{border-color:#40506e!important;color:#bacbff!important;font-weight:700}body.light .oc-box{background:#f4f5f8;color:#131722}body.light .oc-head strong,body.light .oc-table td:nth-child(2){color:#131722}body.light .oc-table th{background:#e9ebf0;color:#485064}body.light .oc-table td:nth-child(2){background:#eaedf3}body.light .oc-head select{background:#fff;color:#131722}body.light .chart-pane-title{background:#eef0f4;color:#485064}body.light .chart-placeholder{background:#fff;color:#667085}@media(max-width:900px){.layout{grid-template-columns:40px minmax(0,1fr)}.right{display:none}.oc-box{height:90vh}.oc-head{flex-wrap:wrap}}@media(max-width:680px){.charts.split{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,calc(var(--primary-share,50%) - 3px)) 6px minmax(0,1fr)}.charts.split .chart-divider{cursor:row-resize}.chart-divider:after{inset:-4px 0;cursor:row-resize}.chart-pane.secondary{border-left:0;border-top:1px solid #2a2e39}}
@keyframes page-enter{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}body{animation:page-enter .38s cubic-bezier(.2,.7,.2,1) both}.top,.left,.right,.quote,.bottom{transition:background-color .18s ease,border-color .18s ease}.btn,.icon,.buy,.sell,.square{transition:transform .16s ease,filter .16s ease,box-shadow .16s ease}.btn:hover,.icon:hover,.buy:hover,.sell:hover,.square:hover{filter:brightness(1.12);transform:translateY(-1px);box-shadow:0 6px 18px #2962ff26}.btn:active,.icon:active,.buy:active,.sell:active,.square:active{transform:translateY(0)}.card{transition:border-color .18s ease,box-shadow .18s ease,transform .18s ease}.card:hover{border-color:#3b4d6b;box-shadow:0 7px 22px #0002}.oc-box{animation:page-enter .2s ease-out both}.charts.split .chart-pane.secondary{animation:page-enter .24s ease-out both}.toast{animation:page-enter .18s ease-out both}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}body[data-theme="modern"]{--bg:#131722;--panel:#1e222d;--border:#2a2e39;--text:#d1d4dc;--muted:#787b86;--grid:#252a36;--up:#089981;--down:#f23645}
body[data-theme="acoustic"]{--bg:#211d19;--panel:#2d261f;--border:#514336;--text:#ebdfcc;--muted:#b09b7d;--grid:#3d3329;--up:#83b47c;--down:#d9786d}
body[data-theme="ocean"]{--bg:#101b27;--panel:#172838;--border:#29465d;--text:#dceaf5;--muted:#8aa8bd;--grid:#20384b;--up:#23c7a5;--down:#ff7185}
body[data-theme="forest"]{--bg:#121b16;--panel:#1b2920;--border:#344b3b;--text:#e1eee4;--muted:#96b09c;--grid:#293b30;--up:#70c98d;--down:#ed777b}
body[data-theme="violet"]{--bg:#171321;--panel:#231c31;--border:#403254;--text:#eee5f7;--muted:#ad9dbc;--grid:#30263e;--up:#53d6b5;--down:#f276a0}
body[data-theme="sunset"]{--bg:#201714;--panel:#2d201b;--border:#53372d;--text:#f3e3d7;--muted:#bc9b86;--grid:#422c24;--up:#66c69f;--down:#ff806d}
body[data-theme] .brand,body[data-theme] .sym,body[data-theme] .price,body[data-theme] .title{color:var(--text)}
body[data-theme] .top select,body[data-theme] .top input,body[data-theme] .inputs input,body[data-theme] .item{color:var(--text);background:var(--bg);border-color:var(--border)}
body[data-theme] .index-strip,body[data-theme] .chart-pane-title{background:var(--panel);border-color:var(--border);color:var(--muted)}
body[data-theme] .chart-placeholder{background:var(--bg);color:var(--muted)}
body[data-theme] .oc-box{background:var(--panel);border-color:var(--border);color:var(--text)}
body[data-theme] .oc-head{border-color:var(--border)}body[data-theme] .oc-head strong,body[data-theme] .oc-table td:nth-child(2){color:var(--text)}
body[data-theme] .oc-table th{background:var(--panel);color:var(--muted)}body[data-theme] .oc-table td{border-color:var(--border)}body[data-theme] .oc-table td:nth-child(2){background:var(--bg)}body[data-theme] .oc-head select{background:var(--bg);color:var(--text);border-color:var(--border)}
body[data-theme] .owner-panel{background:linear-gradient(145deg,var(--panel),var(--bg));border-color:var(--border)}body[data-theme] .owner-panel .owner{background:var(--bg);border-color:var(--border)}body[data-theme] .owner-panel .owner b{color:var(--text)}
body[data-theme] .green,body[data-theme] .oc-ce{color:var(--up)}body[data-theme] .red,body[data-theme] .oc-pe{color:var(--down)}
body[data-theme] .feed-status.live{color:var(--up)}body[data-theme] .buy{background:var(--up)}body[data-theme] .sell{background:var(--down)}</style></head><body data-username="{{ username }}"><header class="top"><span class="brand">REPLAY TERMINAL</span><select id="symbol"><option value="SENSEX" {% if selected_symbol == 'SENSEX' %}selected{% endif %}>SENSEX</option><option value="NIFTY" {% if selected_symbol == 'NIFTY' %}selected{% endif %}>NIFTY</option><option value="BANKNIFTY" {% if selected_symbol == 'BANKNIFTY' %}selected{% endif %}>BANKNIFTY</option></select><select id="interval"><option value="ONE_MINUTE" {% if selected_interval == 'ONE_MINUTE' %}selected{% endif %}>1m</option><option value="FIVE_MINUTE" {% if selected_interval == 'FIVE_MINUTE' %}selected{% endif %}>5m</option><option value="FIFTEEN_MINUTE" {% if selected_interval == 'FIFTEEN_MINUTE' %}selected{% endif %}>15m</option></select><input id="date" type="date" value="{{ selected_date }}" max="{{ max_date }}"><button class="btn" id="play">⏸ Pause</button><button class="btn" id="step">Step ▸</button><button class="btn" id="reset">↺ Reset</button><select id="speed"><option value="1" {% if selected_speed == 1 %}selected{% endif %}>1x</option><option value="2" {% if selected_speed == 2 %}selected{% endif %}>2x</option><option value="5" {% if selected_speed == 5 %}selected{% endif %}>5x</option><option value="10" {% if selected_speed == 10 %}selected{% endif %}>10x</option><option value="25" {% if selected_speed == 25 %}selected{% endif %}>25x</option><option value="50" {% if selected_speed == 50 %}selected{% endif %}>50x</option></select><span class="live-control"><button class="btn" id="liveModeBtn" title="Switch between live market and historical replay">LIVE</button><small id="liveClock" class="live-clock" style="display:none"></small></span><select class="btn theme-select" id="theme" aria-label="Theme"><option value="modern">Modern</option><option value="acoustic">Acoustic</option><option value="ocean">Ocean</option><option value="forest">Forest</option><option value="violet">Violet</option><option value="sunset">Sunset</option></select><button class="btn oc-btn" id="optionChainBtn">Option Chain</button><button class="btn" id="chartModeBtn">2 Charts</button><button class="btn" id="toggleTradingPanel" aria-expanded="true">Hide panel ▸</button><span class="gap"></span><span id="trial">{{ access_label }}</span><span>{{ username }}</span>{% if is_admin %}<a class="btn" href="{{ url_for('admin_panel') }}">Admin</a>{% endif %}<a class="btn" href="{{ url_for('logout') }}">Logout</a></header><div class="layout" id="layout"><aside class="left"><button class="icon active" title="Crosshair">⌖</button><button class="icon" id="line" title="Horizontal line">―</button><button class="icon" id="fit" title="Fit chart">⤢</button></aside><main class="center"><section class="quote"><span class="sym" id="name">Loading…</span><span class="price" id="ltp">—</span><span id="change">—</span><span id="ohlc">O — H — L — C —</span><span id="feedStatus" class="feed-status">REPLAY</span></section><div id="indexStrip" class="index-strip"><span class="index-chip">SENSEX <b>—</b></span><span class="index-chip">NIFTY <b>—</b></span><span class="index-chip">BANKNIFTY <b>—</b></span></div><div id="charts" class="charts{% if has_secondary %} split{% endif %}"><section class="chart-pane primary"><div class="chart-pane-title"><span>Main index · synchronized replay</span></div><div id="chart" class="chart"></div></section><div id="chartDivider" class="chart-divider" role="separator" tabindex="0" aria-valuemin="25" aria-valuemax="75" aria-valuenow="50" aria-orientation="vertical" aria-label="Resize chart panels"></div><section class="chart-pane secondary"><div class="chart-pane-title"><span id="secondaryTitle">{{ secondary_symbol or 'Choose a CE/PE contract from Option Chain' }}</span><button id="clearSecondary" title="Remove second chart">Remove ×</button></div><div id="chartSecondary" class="chart"></div><div id="secondaryPlaceholder" class="chart-placeholder" {% if has_secondary %}style="display:none"{% endif %}>Select a CE/PE contract in Option Chain to add its synchronized replay chart.</div></section></div><footer class="bottom"><span id="clock">Historical replay</span><span>Original broker OHLC · Paper trading only</span><span>Realized PnL <b id="realized">₹0.00</b></span></footer></main><aside class="right"><div class="title">Paper Trading</div><div class="body"><section class="card"><div class="inputs"><label>Lots<input id="lots" type="number" min="1" max="500" value="1"></label><label>SL points<input id="sl" type="number" min="0" value="20"></label><label>Target points<input id="target" type="number" min="0" value="40"></label><label>Trailing points<input id="trail" type="number" min="0" value="0"></label></div><div class="actions"><button class="sell" id="short">SELL</button><button class="buy" id="long">BUY</button></div></section><section class="card"><b>Open position</b><div id="none" style="color:var(--muted);margin-top:10px">No open position.</div><div id="pos" style="display:none"><div class="row"><span>Instrument</span><b id="psym">—</b></div><div class="row"><span>Side / Qty</span><b id="side">—</b></div><div class="row"><span>Entry</span><b id="entry">—</b></div><div class="row"><span>LTP</span><b id="pltp">—</b></div><div class="row"><span>SL</span><b id="psl">—</b></div><div class="row"><span>Target</span><b id="ptarget">—</b></div><div class="row"><span>Live PnL</span><b id="pnl">—</b></div><button class="square" id="square">Square off</button></div></section><section><b>Closed trades</b><div class="history" id="history">No closed trades.</div></section><section class="card owner-panel"><div class="owner-title">Built by</div>
{{ owner|safe }}
</section></div></aside></div><div id="optionChainModal" class="oc-modal"><section class="oc-box"><header class="oc-head"><strong id="ocTitle">Option Chain</strong><select id="ocExpiry" aria-label="Expiry"></select><span class="oc-spacer"></span><button id="ocRefresh" class="btn">Refresh</button><button id="ocClose" class="oc-close">Close</button></header><div id="ocNote" class="oc-note">Loading contracts…</div><div class="oc-table-wrap"><table class="oc-table"><thead><tr><th>Call · CE</th><th>Strike</th><th>Put · PE</th></tr></thead><tbody id="ocRows"><tr><td class="oc-empty" colspan="3">Loading contracts…</td></tr></tbody></table></div></section></div><div id="toast" class="toast"></div><div id="overlay" class="overlay"><div class="overlaybox"><h2>Free trial ended</h2><p>Your two-minute trial has ended. Contact the owner for access.</p>
{{ owner|safe }}
<a class="btn" href="/logout">Sign out</a></div></div>
<script>
(function(){
document.body.classList.remove('light');
const csrf=document.querySelector('meta[name="csrf-token"]').content,E=id=>document.getElementById(id),feedModeKey='replay-feed-mode:'+document.body.dataset.username,themeKey='replay-theme:'+document.body.dataset.username;let chart,series,chart2,series2,bars=[],bars2=[],busy=false,trial={{ trial }},fullAccess={{ 'true' if full_access else 'false' }},secondaryEnabled={{ 'true' if has_secondary else 'false' }},splitMode={{ 'true' if has_secondary else 'false' }},draw=false,lines=[],syncingRange=false,bootstrapPending=true,secondaryRangeSynced=false,liveMode=(()=>{try{return localStorage.getItem(feedModeKey)==='live'}catch(_){return false}})(),liveBootstrapPending=liveMode,replayLookbackOverride={{ 'null' if selected_date == max_date else '1' }};
if(liveMode){E('liveModeBtn').textContent='Replay';E('liveModeBtn').classList.add('live-active')}
async function api(url,opts){opts=opts||{};opts.headers=Object.assign({},opts.headers||{}, {'X-CSRFToken':csrf});if(opts.body&&typeof opts.body==='object'){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.body)}const r=await fetch(url,opts);let d={};try{d=await r.json()}catch(_){}if(!r.ok||d.status==='error')throw Error(d.message||('Request failed ('+r.status+')'));return d}
function toast(s){const t=E('toast');t.textContent=s;t.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.style.display='none',3500)}function f(n){return Number(n||0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}function historyKey(kind,symbol,dateValue){return 'replay-visible-history:'+encodeURIComponent(document.body.dataset.username||'')+':'+kind+':'+symbol+':'+E('interval').value+':'+dateValue}function persistVisibleHistory(dateValue){try{if(bars.length)localStorage.setItem(historyKey('primary',E('symbol').value,dateValue),JSON.stringify(bars));const secondary=E('secondaryTitle').textContent;if(bars2.length&&secondary&&secondary.indexOf('Choose a CE/PE')!==0)localStorage.setItem(historyKey('secondary',secondary,dateValue),JSON.stringify(bars2))}catch(_){}}function restoreVisibleHistory(){const dateValue=liveMode?E('date').max:E('date').value;try{const saved=JSON.parse(localStorage.getItem(historyKey('primary',E('symbol').value,dateValue))||'[]');if(Array.isArray(saved)&&saved.length){bars=saved;series.setData(bars)}if(secondaryEnabled){const name=E('secondaryTitle').textContent,optionBars=JSON.parse(localStorage.getItem(historyKey('secondary',name,dateValue))||'[]');if(Array.isArray(optionBars)&&optionBars.length){bars2=optionBars;series2.setData(bars2)}}}catch(_){}}function updateSeries(seriesRef,barList,candle){seriesRef.update(candle);const i=barList.findIndex(x=>x.time===candle.time);if(i>=0)barList[i]=candle;else barList.push(candle)}function applyFinalized(seriesRef,barList,candles){for(const candle of candles||[])updateSeries(seriesRef,barList,candle)}function safe(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function chartOptions(box){const p=PALETTES[document.body.dataset.theme]||PALETTES.modern;return {width:Math.max(200,box.clientWidth),height:Math.max(120,box.clientHeight),layout:{background:{color:p.bg},textColor:p.text,fontSize:12},grid:{vertLines:{color:p.grid},horzLines:{color:p.grid}},timeScale:{timeVisible:true,secondsVisible:false,borderColor:p.border,rightOffset:6},rightPriceScale:{borderColor:p.border},crosshair:{mode:LightweightCharts.CrosshairMode.Normal}}}
let paneShare=0.5;try{const saved=Number(localStorage.getItem('replay-pane-share'));if(saved>=0.25&&saved<=0.75)paneShare=saved}catch(_){}function syncSecondaryRange(){if(!chart||!chart2||!splitMode)return;const range=chart.timeScale().getVisibleRange();if(!range)return;syncingRange=true;try{chart2.timeScale().setVisibleRange(range)}catch(_){}syncingRange=false}function setupDivider(){const divider=E('chartDivider'),root=E('charts');divider.addEventListener('pointerdown',e=>{if(!splitMode)return;e.preventDefault();divider.classList.add('dragging');try{divider.setPointerCapture(e.pointerId)}catch(_){}const move=ev=>{const r=root.getBoundingClientRect(),stacked=window.matchMedia('(max-width:680px)').matches,ratio=stacked?(ev.clientY-r.top)/r.height:(ev.clientX-r.left)/r.width;paneShare=Math.min(.75,Math.max(.25,ratio));root.style.setProperty('--primary-share',(paneShare*100)+'%');resizeCharts();syncSecondaryRange()};const end=()=>{divider.classList.remove('dragging');try{localStorage.setItem('replay-pane-share',String(paneShare))}catch(_){}divider.removeEventListener('pointermove',move);divider.removeEventListener('pointerup',end);divider.removeEventListener('pointercancel',end)};divider.addEventListener('pointermove',move);divider.addEventListener('pointerup',end);divider.addEventListener('pointercancel',end)})}
function resizeCharts(){[[chart,E('chart')],[chart2,E('chartSecondary')]].forEach(([c,box])=>{if(!c||!box)return;const r=box.getBoundingClientRect();if(r.width>0&&r.height>0)c.applyOptions({width:r.width,height:r.height})})}
function initChart(){const box=E('chart'),box2=E('chartSecondary');chart=LightweightCharts.createChart(box,chartOptions(box));chart2=LightweightCharts.createChart(box2,chartOptions(box2));const p=PALETTES[document.body.dataset.theme]||PALETTES.modern;const opts={upColor:p.up,downColor:p.down,borderVisible:false,wickUpColor:p.up,wickDownColor:p.down};series=chart.addCandlestickSeries(opts);series2=chart2.addCandlestickSeries(opts);window.addEventListener('resize',resizeCharts);if(window.ResizeObserver){const ro=new ResizeObserver(resizeCharts);ro.observe(E('charts'));ro.observe(E('chart'));ro.observe(E('chartSecondary'))}setupDivider();chart.subscribeClick(p=>{if(draw&&p.point){let price=series.coordinateToPrice(p.point.y);if(price)series.createPriceLine({price,color:'#2962ff',lineStyle:LightweightCharts.LineStyle.Dashed,title:'S/R',axisLabelVisible:true});draw=false;E('line').classList.remove('active')}});requestAnimationFrame(resizeCharts)}
function drawPosition(t){lines.forEach(x=>series.removePriceLine(x));lines=[];if(!t||!t.position)return;lines.push(series.createPriceLine({price:t.entry_price,color:'#2962ff',lineWidth:2,title:'ENTRY'}));if(t.sl_price>0)lines.push(series.createPriceLine({price:t.sl_price,color:'#f23645',lineStyle:2,title:'SL'}));if(t.target_price>0)lines.push(series.createPriceLine({price:t.target_price,color:'#089981',lineStyle:2,title:'TARGET'}))}
function renderSecondary(sc){if(!sc){E('secondaryTitle').textContent='Choose a CE/PE contract from Option Chain';E('secondaryPlaceholder').textContent='Select a CE/PE contract in Option Chain to add its synchronized replay chart.';E('secondaryPlaceholder').style.display=splitMode?'flex':'none';return}E('secondaryTitle').textContent=sc.symbol+(sc.interval_label?' · '+sc.interval_label:'');if(sc.error){E('secondaryPlaceholder').textContent=sc.error;E('secondaryPlaceholder').style.display='flex';return}if(sc.waiting||!sc.current_candle){E('secondaryPlaceholder').textContent='Waiting for this option’s first candle at the replay time…';E('secondaryPlaceholder').style.display='flex';return}E('secondaryPlaceholder').style.display='none';if(sc.history&&sc.history.length){bars2=sc.history;series2.setData(bars2)}applyFinalized(series2,bars2,sc.finalized_candles);const c=sc.current_candle;updateSeries(series2,bars2,c);if(sc.history&&sc.history.length&&!secondaryRangeSynced){syncSecondaryRange();secondaryRangeSynced=true}}
function updateFeedDisplay(d){const status=E("feedStatus");if(status){const live=d.mode==="live";status.textContent=live?(d.market_open?"● LIVE":"MARKET CLOSED · LAST QUOTE"):"HISTORICAL REPLAY";status.className="feed-status"+(live?(d.market_open?" live":" closed"):"")}const liveClock=E("liveClock");if(liveClock){liveClock.textContent=d.mode==="live"?(d.synced_time_str||"Waiting for Angel One exchange time"):"";liveClock.style.display=d.mode==="live"?"block":"none"}const quotes=d.index_quotes||{},names=["SENSEX","NIFTY","BANKNIFTY"];E("indexStrip").innerHTML=names.map(n=>{const q=quotes[n];return "<span class=\"index-chip\">"+n+" <b>"+(q?"₹"+f(q.ltp)+" ("+(q.change_pct>=0?"+":"")+f(q.change_pct)+"%)":"—")+"</b></span>"}).join("")}function render(d){updateFeedDisplay(d);if(d.selected_date)E("date").value=d.selected_date;const c=d.current_candle;if(d.history&&d.history.length){bars=d.history;series.setData(bars)}applyFinalized(series,bars,d.finalized_candles);if(c){updateSeries(series,bars,c);E('name').textContent=d.symbol+' · '+d.interval_label+' · '+d.exchange;E('ltp').textContent=f(c.close);E('change').textContent=d.change_str;E('ohlc').textContent='O '+f(c.open)+' H '+f(c.high)+' L '+f(c.low)+' C '+f(c.close);E('clock').textContent=d.synced_time_str}renderSecondary(d.secondary_chart);if(d.selected_date)persistVisibleHistory(d.selected_date);E('realized').textContent=(d.realized_pnl>=0?'+₹':'−₹')+f(Math.abs(d.realized_pnl));E('play').textContent=d.is_playing?'⏸ Pause':'▶ Play';const t=d.trade_info,open=!!(t&&t.position);E('none').style.display=open?'none':'block';E('pos').style.display=open?'block':'none';if(open&&c){E('psym').textContent=t.label;E('side').textContent=t.position+' / '+t.qty;E('entry').textContent=f(t.entry_price);E('pltp').textContent=f(c.close);E('psl').textContent=t.sl_price?f(t.sl_price):'—';E('ptarget').textContent=t.target_price?f(t.target_price):'—';E('pnl').textContent=(t.pnl>=0?'+₹':'−₹')+f(Math.abs(t.pnl));drawPosition(t)}else drawPosition(null);if(!d.trade_history||!d.trade_history.length)E('history').textContent='No closed trades.';else E('history').innerHTML=d.trade_history.slice(0,30).map(x=>'<div class="item"><span><b>'+safe(x.symbol)+'</b> '+safe(x.type)+'<br>'+safe(x.reason)+' · '+f(x.entry)+' → '+f(x.exit)+'</span><b class="'+(x.pnl>=0?'green':'red')+'">'+(x.pnl>=0?'+':'−')+'₹'+f(Math.abs(x.pnl))+'</b></div>').join('');if(t&&t.auto_closed)toast('Closed: '+t.reason+' · PnL ₹'+f(t.pnl))}
async function tick(){if(busy||trial<=0)return;busy=true;try{const lookback=replayLookbackOverride||({ONE_MINUTE:30,FIVE_MINUTE:100,FIFTEEN_MINUTE:200}[E('interval').value]||30);const endpoint=liveMode?'/api/live_tick'+(liveBootstrapPending?'?bootstrap=1':''):'/api/get_tick?lookback='+lookback+(bootstrapPending?'&bootstrap=1':'');const data=await api(endpoint);bootstrapPending=false;liveBootstrapPending=false;render(data)}catch(e){E('name').textContent='Data unavailable';if(liveMode)E('feedStatus').textContent='LIVE FEED UNAVAILABLE';if(e.message.indexOf('Access expired')===0){trial=0;E('overlay').style.display='flex'}toast(e.message)}finally{busy=false}}
async function doRun(fn){try{await fn();await tick()}catch(e){toast(e.message)}}async function change(url,data){bootstrapPending=true;secondaryRangeSynced=false;bars=[];bars2=[];series.setData([]);series2.setData([]);await api(url,{method:'POST',body:data||{}});await tick()}
E('symbol').addEventListener('change',()=>doRun(()=>change('/api/change_symbol',{symbol:E('symbol').value})));E('interval').addEventListener('change',()=>doRun(()=>change('/api/set_interval',{interval:E('interval').value})));E('date').addEventListener('change',()=>{replayLookbackOverride=1;doRun(()=>change('/api/set_date',{date:E('date').value}))});E('speed').addEventListener('change',()=>doRun(()=>api('/api/set_speed',{method:'POST',body:{speed:Number(E('speed').value)}})));E('play').addEventListener('click',()=>doRun(()=>api('/api/toggle_play',{method:'POST',body:{}})));E('step').addEventListener('click',()=>doRun(()=>api('/api/step_candle',{method:'POST',body:{}})));E('reset').addEventListener('click',()=>doRun(()=>change('/api/reset_sim',{})));E('fit').addEventListener('click',()=>{chart.timeScale().fitContent();requestAnimationFrame(syncSecondaryRange)});E('line').addEventListener('click',()=>{draw=!draw;E('line').classList.toggle('active',draw)});
let ocExpiryReady=false;async function loadOptionChain(expiry){const modal=E('optionChainModal');if(modal.style.display!=='flex')return;E('ocRows').innerHTML='<tr><td class="oc-empty" colspan="3">Loading contracts…</td></tr>';E('ocNote').textContent='Fetching Angel One contracts…';try{const q=new URLSearchParams({symbol:E('symbol').value});if(expiry)q.set('expiry',expiry);const d=await api('/api/option_chain?'+q.toString());E('ocTitle').textContent=d.symbol+' Option Chain · '+d.exchange;const sel=E('ocExpiry');if(!ocExpiryReady||sel.dataset.symbol!==d.symbol){sel.innerHTML=d.expiries.map(x=>'<option value="'+safe(x)+'">'+safe(x)+'</option>').join('');sel.dataset.symbol=d.symbol;ocExpiryReady=true}if(d.expiry)sel.value=d.expiry;E('ocNote').textContent=d.rows.length+' strikes · Click any CE/PE contract to add synchronized replay in chart 2. '+d.note;E('ocRows').innerHTML=d.rows.map(r=>'<tr><td class="oc-ce">'+(r.CE?'<button class="oc-select oc-contract" data-underlying="'+safe(d.symbol)+'" data-symbol="'+safe(r.CE.symbol)+'" data-token="'+safe(r.CE.token)+'" data-lot="'+safe(r.CE.lot_size)+'">'+safe(r.CE.symbol)+'</button><br><small>Lot '+safe(r.CE.lot_size)+'</small>':'—')+'</td><td>'+Number(r.strike).toLocaleString('en-IN',{maximumFractionDigits:4})+'</td><td class="oc-pe">'+(r.PE?'<button class="oc-select oc-contract" data-underlying="'+safe(d.symbol)+'" data-symbol="'+safe(r.PE.symbol)+'" data-token="'+safe(r.PE.token)+'" data-lot="'+safe(r.PE.lot_size)+'">'+safe(r.PE.symbol)+'</button><br><small>Lot '+safe(r.PE.lot_size)+'</small>':'—')+'</td></tr>').join('')}catch(e){E('ocRows').innerHTML='<tr><td class="oc-empty" colspan="3">'+safe(e.message)+'</td></tr>';E('ocNote').textContent='Option chain unavailable'}}E('optionChainBtn').addEventListener('click',()=>{E('optionChainModal').style.display='flex';loadOptionChain(E('ocExpiry').value)});E('ocClose').addEventListener('click',()=>E('optionChainModal').style.display='none');E('optionChainModal').addEventListener('click',e=>{if(e.target===E('optionChainModal'))E('optionChainModal').style.display='none'});E('ocRefresh').addEventListener('click',()=>loadOptionChain(E('ocExpiry').value));E('ocExpiry').addEventListener('change',()=>loadOptionChain(E('ocExpiry').value));document.addEventListener('keydown',e=>{if(e.key==='Escape')E('optionChainModal').style.display='none'});E('symbol').addEventListener('change',()=>{if(E('optionChainModal').style.display==='flex')loadOptionChain('')});const PALETTES={modern:{bg:'#131722',panel:'#1e222d',border:'#2a2e39',text:'#d1d4dc',muted:'#787b86',grid:'#252a36',up:'#089981',down:'#f23645'},acoustic:{bg:'#211d19',panel:'#2d261f',border:'#514336',text:'#ebdfcc',muted:'#b09b7d',grid:'#3d3329',up:'#83b47c',down:'#d9786d'},ocean:{bg:'#101b27',panel:'#172838',border:'#29465d',text:'#dceaf5',muted:'#8aa8bd',grid:'#20384b',up:'#23c7a5',down:'#ff7185'},forest:{bg:'#121b16',panel:'#1b2920',border:'#344b3b',text:'#e1eee4',muted:'#96b09c',grid:'#293b30',up:'#70c98d',down:'#ed777b'},violet:{bg:'#171321',panel:'#231c31',border:'#403254',text:'#eee5f7',muted:'#ad9dbc',grid:'#30263e',up:'#53d6b5',down:'#f276a0'},sunset:{bg:'#201714',panel:'#2d201b',border:'#53372d',text:'#f3e3d7',muted:'#bc9b86',grid:'#422c24',up:'#66c69f',down:'#ff806d'}};function applyPalette(name,save){if(!PALETTES[name])name='modern';const p=PALETTES[name];document.body.dataset.theme=name;E('theme').value=name;if(save)try{localStorage.setItem(themeKey,name)}catch(_){}if(chart&&chart2){const options={layout:{background:{color:p.bg},textColor:p.text},grid:{vertLines:{color:p.grid},horzLines:{color:p.grid}},timeScale:{borderColor:p.border},rightPriceScale:{borderColor:p.border}};chart.applyOptions(options);chart2.applyOptions(options);const colors={upColor:p.up,downColor:p.down,borderVisible:false,wickUpColor:p.up,wickDownColor:p.down};series.applyOptions(colors);series2.applyOptions(colors)}}let savedTheme='modern';try{savedTheme=localStorage.getItem(themeKey)||'modern'}catch(_){}applyPalette(savedTheme,false);E('theme').addEventListener('change',()=>applyPalette(E('theme').value,true));
E('chartModeBtn').textContent=splitMode?'Single Chart':'2 Charts';function applyChartMode(){const root=E('charts');root.style.setProperty('--primary-share',(paneShare*100)+'%');root.classList.toggle('split',splitMode);E('chartModeBtn').textContent=splitMode?'Single Chart':'2 Charts';E('secondaryPlaceholder').style.display=splitMode&&!secondaryEnabled?'flex':'none';requestAnimationFrame(()=>{resizeCharts();syncSecondaryRange()})}E('chartModeBtn').addEventListener('click',()=>{splitMode=!splitMode;applyChartMode()});E('liveModeBtn').addEventListener('click',async()=>{liveMode=!liveMode;try{localStorage.setItem(feedModeKey,liveMode?'live':'replay')}catch(_){}replayLookbackOverride=null;liveBootstrapPending=liveMode;bootstrapPending=!liveMode;bars=[];bars2=[];series.setData([]);series2.setData([]);E('liveModeBtn').textContent=liveMode?'↩ Replay':'● Live';E('liveModeBtn').classList.toggle('live-active',liveMode);try{await tick()}catch(e){toast(e.message)}});let tradingPanelVisible=true;E('toggleTradingPanel').addEventListener('click',()=>{tradingPanelVisible=!tradingPanelVisible;E('layout').classList.toggle('panel-collapsed',!tradingPanelVisible);E('toggleTradingPanel').textContent=tradingPanelVisible?'Hide panel ▸':'Show panel ◂';E('toggleTradingPanel').setAttribute('aria-expanded',String(tradingPanelVisible));requestAnimationFrame(()=>{resizeCharts();if(tradingPanelVisible)syncSecondaryRange()})});E('clearSecondary').addEventListener('click',()=>doRun(async()=>{await api('/api/clear_option',{method:'POST',body:{}});secondaryEnabled=false;splitMode=false;bars2=[];series2.setData([]);applyChartMode()}));E('ocRows').addEventListener('click',e=>{const b=e.target.closest('.oc-select');if(!b)return;doRun(async()=>{await api('/api/select_option',{method:'POST',body:{underlying:b.dataset.underlying,symbol:b.dataset.symbol,token:b.dataset.token}});secondaryEnabled=true;splitMode=true;secondaryRangeSynced=false;bars2=[];series2.setData([]);E('secondaryTitle').textContent=b.dataset.symbol;E('secondaryPlaceholder').style.display='none';E('optionChainModal').style.display='none';applyChartMode()})});if(secondaryEnabled){splitMode=true;applyChartMode()}async function order(side){await doRun(()=>api('/api/trade',{method:'POST',body:{action:side,lots:Number(E('lots').value),sl_points:Number(E('sl').value),target_points:Number(E('target').value),tsl_points:Number(E('trail').value)}}))}E('long').addEventListener('click',()=>order('LONG'));E('short').addEventListener('click',()=>order('SHORT'));E('square').addEventListener('click',()=>doRun(()=>api('/api/close_trade',{method:'POST',body:{}})));
if(!fullAccess)setInterval(()=>{if(trial>0){trial--;E('trial').textContent='Trial '+Math.floor(trial/60)+':'+String(trial%60).padStart(2,'0')}if(trial<=0){E('overlay').style.display='flex';E('trial').textContent='Trial ended'}},1000);initChart();restoreVisibleHistory();tick();setInterval(()=>{if(!liveMode)tick()},100);setInterval(()=>{if(liveMode)tick()},1000)
})();
</script></body></html>"""

@app.route("/")
def index():
    return redirect(url_for("home" if session.get("user_id") else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name, password = (request.form.get("username") or "").strip(), request.form.get("password") or ""
        user = db.session.execute(db.select(User).filter_by(username=name)).scalar_one_or_none()
        if user and user.is_active and check_password_hash(user.password, password):
            session.clear(); session.permanent = True
            if not has_full_access(user) and user.trial_started_at is None:
                user.trial_started_at = time.time()
                db.session.commit()
            session.update(user_id=user.id, username=user.username, is_admin=bool(user.is_admin), auth_version=user.auth_version)
            csrf_token()
            return redirect(url_for("admin_panel" if user.is_admin else "home"))
        msg = "Invalid username or password." if user is None or user.is_active else "This account is blocked."
        return render_template_string(LOGIN_HTML, error=msg, csrf=csrf_token(), owner=OWNER_INFO_HTML), 401
    return render_template_string(LOGIN_HTML, error=None, csrf=csrf_token(), owner=OWNER_INFO_HTML)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, password = (request.form.get("username") or "").strip(), request.form.get("password") or ""
        if not (3 <= len(name) <= 80) or not name.replace("_", "").replace("-", "").isalnum():
            return render_template_string(REGISTER_HTML, error="Username must be 3–80 letters, numbers, _ or -.", csrf=csrf_token(), owner=OWNER_INFO_HTML), 400
        if len(password) < 10:
            return render_template_string(REGISTER_HTML, error="Password must be at least 10 characters.", csrf=csrf_token(), owner=OWNER_INFO_HTML), 400
        if db.session.execute(db.select(User).filter_by(username=name)).scalar_one_or_none():
            return render_template_string(REGISTER_HTML, error="That username is already taken.", csrf=csrf_token(), owner=OWNER_INFO_HTML), 409
        new_user = User(username=name, password=generate_password_hash(password), is_admin=False, is_active=True)
        db.session.add(new_user); db.session.commit(); get_state(new_user.id)
        return redirect(url_for("login"))
    return render_template_string(REGISTER_HTML, error=None, csrf=csrf_token(), owner=OWNER_INFO_HTML)


@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin_panel():
    users = db.session.execute(db.select(User).order_by(User.id)).scalars().all()
    labels = {
        user.id: datetime.fromtimestamp(user.valid_until, IST).strftime("%d %b %Y, %I:%M %p IST")
        for user in users if user.valid_until is not None
    }
    return render_template_string(ADMIN_HTML, users=users, csrf=csrf_token(), validity_labels=labels, now=time.time())


@app.route("/admin/grant/<int:user_id>", methods=["POST"])
@admin_required
def grant_access(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.is_admin:
        return jsonify(status="error", message="This account cannot receive a standard user pass."), 404
    try:
        days = int((request.get_json(silent=True) or {}).get("days", 0))
    except (TypeError, ValueError):
        return jsonify(status="error", message="Enter a valid number of days."), 400
    if not 1 <= days <= 3650:
        return jsonify(status="error", message="Validity must be between 1 and 3650 days."), 400

    # Issue a strong one-time password; only its hash is stored in the database.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    generated_password = "".join(secrets.choice(alphabet) for _ in range(20))
    now = time.time()
    start = max(now, user.valid_until or 0.0)
    user.password = generate_password_hash(generated_password)
    user.valid_until = start + days * 86400
    user.auth_version += 1
    if user.trial_started_at is None:
        user.trial_started_at = now - TRIAL_SECONDS
    db.session.commit()
    return jsonify(
        status="success",
        password=generated_password,
        valid_until=datetime.fromtimestamp(user.valid_until, IST).strftime("%d %b %Y, %I:%M %p IST"),
    )


@app.route("/admin/toggle/<int:user_id>", methods=["POST"])
@admin_required
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if user and not user.is_admin:
        user.is_active = not user.is_active; db.session.commit()
    return redirect(url_for("admin_panel"))


@app.route("/terminal")
@login_required
def home():
    user = db.session.get(User, session["user_id"]); state = get_state(user.id)
    full_access = has_full_access(user)
    if user.is_admin:
        access_label = "Administrator · unlimited access"
    elif full_access:
        access_label = "Full access until " + datetime.fromtimestamp(user.valid_until, IST).strftime("%d %b %Y")
    else:
        left = trial_left(user)
        access_label = "Trial " + str(left // 60) + ":" + str(left % 60).zfill(2)
    return render_template_string(HTML_PAGE, username=user.username, is_admin=user.is_admin,
        full_access=full_access, trial=999999 if full_access else trial_left(user), access_label=access_label,
        selected_date=state.selected_date, selected_symbol=state.symbol, selected_interval=state.interval, selected_speed=state.playback_speed,
        secondary_symbol=state.secondary_symbol or "", has_secondary=bool(state.secondary_symbol),
        max_date=datetime.now(IST).date().isoformat(), csrf=csrf_token(), owner=OWNER_INFO_HTML)


@app.route("/api/option_chain")
@login_required
def api_option_chain():
    symbol = (request.args.get("symbol") or "SENSEX").upper()
    if symbol not in INDICES:
        return jsonify(status="error", message="Unsupported index."), 400
    try:
        expiries = option_expiries(symbol)
        if not expiries:
            return jsonify(status="error", message=f"No option contracts are currently listed for {symbol}."), 404
        expiry = (request.args.get("expiry") or expiries[0]).upper()
        if expiry not in expiries:
            return jsonify(status="error", message="Choose an expiry returned by the instrument master."), 400
        rows = option_chain_rows(symbol, expiry)
        if not rows:
            return jsonify(status="error", message="No CE/PE strikes found for this expiry."), 404
        return jsonify(status="success", symbol=symbol, exchange=OPTION_EXCHANGES[symbol], expiry=expiry,
                       expiries=expiries, rows=rows, source="Angel One instrument master",
                       note="Contract/strike list is refreshed from the daily instrument master; it does not contain live option premiums.")
    except Exception as exc:
        log.exception("Option-chain fetch failed for %s", symbol)
        return jsonify(status="error", message="Could not load Angel One option contracts: " + str(exc)), 502


@app.route("/api/live_tick")
@login_required
def api_live_tick():
    user, state = current_user_state()
    try:
        quotes = refresh_live_quotes(state)
    except Exception as exc:
        log.exception("Live quote update failed")
        return jsonify(status="error", message=str(exc)), 503

    indices = {}
    for name, instrument in INDICES.items():
        quote = quotes.get((instrument["exchange"], str(instrument["token"])))
        if quote:
            indices[name] = {"ltp": float(quote.get("ltp") or 0),
                             "change": float(quote.get("netChange") or 0),
                             "change_pct": float(quote.get("percentChange") or 0)}
    instrument = INDICES[state.symbol]
    primary_quote = quotes.get((instrument["exchange"], str(instrument["token"])))
    if not primary_quote:
        return jsonify(status="error", message="Angel One has not returned a live quote for " + state.symbol), 503
    try:
        price = float(primary_quote.get("ltp") or 0)
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(status="error", message="Live index price is unavailable."), 503

    now_ist = datetime.now(IST)
    today = now_ist.date().isoformat()
    market_open = now_ist.weekday() < 5 and clock_time(9, 15) <= now_ist.time().replace(tzinfo=None) <= clock_time(15, 30)
    cached_primary_rows = rows_of(state) if state.selected_date == today else []
    # Live always anchors replay to today's market session. Clear an old cached
    # replay day once when switching over, so Replay can reload today's data.
    if state.selected_date != today or rows_of(state):
        with state_lock:
            state.selected_date = today
            state.real_data_json = "[]"
            state.candle_index = 0
            state.candle_progress = 0
            state.is_playing = True
            state.needs_history_reload = True
            state.last_tick_at = time.time()
            state.smooth_price = state.current_high = state.current_low = 0
            state.secondary_data_json = "[]"
            state.secondary_needs_history_reload = True
            state.secondary_last_index = -1
            state.secondary_active_time = ""
            state.secondary_smooth_price = state.secondary_high = state.secondary_low = 0
    bootstrap = request.args.get("bootstrap") == "1"
    if bootstrap:
        state.is_playing = True
        state.last_tick_at = time.time()
    primary_history = []
    if bootstrap:
        try:
            rows = cached_primary_rows or rows_of(state)
            if not rows:
                rows = fetch_candles(state.symbol, state.interval, today)
            primary_history = [chart_candle(row) for row in rows]
        except Exception:
            log.info("Live chart history is not ready; starting from the next live tick.")
    seed = primary_history[-1] if primary_history else None
    candle = make_live_candle(user.id, instrument["exchange"], instrument["token"], state.interval, price, seed)
    save_live_candle(user.id, state.symbol, state.symbol, instrument["exchange"], instrument["token"],
                     state.interval, today, candle)

    secondary = None
    if state.secondary_symbol and state.secondary_token and state.secondary_exchange:
        secondary_quote = quotes.get((state.secondary_exchange.upper(), str(state.secondary_token)))
        if secondary_quote and float(secondary_quote.get("ltp") or 0) > 0:
            option_price = float(secondary_quote["ltp"])
            option_history = []
            if bootstrap:
                try:
                    rows = fetch_instrument_candles(state.secondary_exchange, state.secondary_token, state.interval, today)
                    option_history = [chart_candle(row) for row in rows]
                except Exception:
                    log.info("Live option chart history is not ready for %s.", state.secondary_symbol)
            option_seed = option_history[-1] if option_history else None
            option_candle = make_live_candle(user.id, state.secondary_exchange.upper(), state.secondary_token,
                                             state.interval, option_price, option_seed)
            save_live_candle(user.id, state.symbol, state.secondary_symbol, state.secondary_exchange.upper(),
                             state.secondary_token, state.interval, today, option_candle)
            secondary = {"symbol": state.secondary_symbol, "interval_label": INTERVALS.get(state.interval, "1m"),
                         "history": option_history if bootstrap else [], "current_candle": option_candle}
        else:
            secondary = {"symbol": state.secondary_symbol, "error": "Waiting for Angel One live quote for this option."}

    state.last_ltp = price
    trade_info = None
    if state.position:
        qty = INDICES[state.symbol]["lot"] * state.lots
        pnl = (price - state.entry_price) * qty if state.position == "LONG" else (state.entry_price - price) * qty
        state.unrealized_pnl = round(pnl, 2)
        trade_info = {"position": state.position, "label": state.position_label, "qty": qty,
                      "entry_price": state.entry_price, "sl_price": state.stop_loss,
                      "target_price": state.target_price, "pnl": state.unrealized_pnl}
    change = float(primary_quote.get("netChange") or 0)
    change_pct = float(primary_quote.get("percentChange") or 0)
    exchange_time = ""
    for raw_time in (primary_quote.get("exchFeedTime"), primary_quote.get("exchTradeTime")):
        candidate_time = str(raw_time or "").strip()
        if candidate_time and "1970" not in candidate_time:
            exchange_time = candidate_time
            break
    result = {"status": "success", "mode": "live", "market_open": market_open, "is_playing": False,
              "current_candle": candle, "history": primary_history if bootstrap else [],
              "secondary_chart": secondary, "index_quotes": indices,
              "symbol": state.symbol, "exchange": instrument["exchange"],
              "interval_label": INTERVALS.get(state.interval, "1m"),
              "change_value": change, "change_str": f"{'+' if change >= 0 else ''}{change:.2f} ({change_pct:.2f}%)",
              "selected_date": today, "synced_time_str": exchange_time or datetime.now(IST).strftime("%d %b %Y  %H:%M:%S IST"),
              "trade_info": trade_info, "realized_pnl": state.realized_pnl,
              "trade_history": history_of(state)}
    db.session.commit()
    return jsonify(result)


@app.route("/api/select_option", methods=["POST"])
@login_required
def select_option():
    data = request.get_json(silent=True) or {}
    underlying = str(data.get("underlying", "")).upper()
    token = str(data.get("token", ""))
    contract = str(data.get("symbol", ""))
    if underlying not in INDICES or not token or not contract:
        return jsonify(status="error", message="Choose a valid CE/PE contract from the option chain."), 400
    exchange = OPTION_EXCHANGES[underlying]
    match = next((row for row in get_option_master()
                  if str(row.get("token", "")) == token
                  and str(row.get("symbol", "")) == contract
                  and str(row.get("name", "")).upper() == underlying
                  and str(row.get("exch_seg", "")).upper() == exchange
                  and contract.upper().endswith(("CE", "PE"))), None)
    if match is None:
        return jsonify(status="error", message="That contract is no longer in the current instrument master."), 400
    try:
        lot = max(1, int(match.get("lotsize", 1)))
    except (TypeError, ValueError):
        lot = 1
    user, state = current_user_state()
    state.secondary_symbol = contract
    state.secondary_token = token
    state.secondary_exchange = exchange
    state.secondary_lot = lot
    state.secondary_data_json = "[]"
    state.secondary_needs_history_reload = True
    state.secondary_last_index = -1
    state.secondary_active_time = ""
    state.secondary_smooth_price = state.secondary_high = state.secondary_low = 0
    state.last_tick_at = time.time()
    db.session.commit()
    return jsonify(status="success", symbol=contract, exchange=exchange, message="Option replay linked to the main chart clock.")


@app.route("/api/clear_option", methods=["POST"])
@login_required
def clear_option():
    user, state = current_user_state()
    state.secondary_symbol = state.secondary_token = state.secondary_exchange = None
    state.secondary_data_json = "[]"
    state.secondary_needs_history_reload = True
    state.secondary_last_index = -1
    state.secondary_active_time = ""
    state.secondary_smooth_price = state.secondary_high = state.secondary_low = 0
    db.session.commit()
    return jsonify(status="success")


def current_user_state():
    user = db.session.get(User, session["user_id"])
    query = db.select(TradingState).filter_by(user_id=user.id).with_for_update()
    state = db.session.execute(query).scalar_one_or_none()
    if state is None:
        state = get_state(user.id)
    return user, state


@app.route("/api/change_symbol", methods=["POST"])
@login_required
def change_symbol():
    symbol = (request.get_json(silent=True) or {}).get("symbol")
    if symbol not in INDICES: return jsonify(status="error", message="Unsupported index."), 400
    user, s = current_user_state()
    with state_lock:
        rows = rows_of(s)
        if s.position and s.last_ltp > 0: closed_trades(s, "Symbol switch")
        s.symbol=symbol; s.real_data_json="[]"; s.candle_index=0; s.candle_progress=0; s.last_ltp=0
        s.is_playing=True; s.needs_history_reload=True; s.smooth_price=s.current_high=s.current_low=0
        s.secondary_needs_history_reload=True; s.secondary_active_time=""
        s.secondary_last_index=-1
        s.secondary_smooth_price=s.secondary_high=s.secondary_low=0
        s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success")


@app.route("/api/set_interval", methods=["POST"])
@login_required
def set_interval():
    interval=(request.get_json(silent=True) or {}).get("interval")
    if interval not in INTERVALS: return jsonify(status="error", message="Unsupported timeframe."), 400
    user,s=current_user_state(); s.interval=interval; s.real_data_json="[]"; s.candle_index=0; s.candle_progress=0; s.last_ltp=0
    s.secondary_data_json="[]"; s.secondary_needs_history_reload=True; s.secondary_active_time=""
    s.secondary_last_index=-1
    s.secondary_smooth_price=s.secondary_high=s.secondary_low=0
    s.is_playing=True; s.needs_history_reload=True; s.smooth_price=s.current_high=s.current_low=0; s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success")


@app.route("/api/set_date", methods=["POST"])
@login_required
def set_date():
    try:
        day=date.fromisoformat(str((request.get_json(silent=True) or {}).get("date", "")))
        if day>datetime.now(IST).date(): raise ValueError
    except ValueError: return jsonify(status="error", message="Choose a valid date up to today."),400
    user,s=current_user_state()
    if s.position and s.last_ltp>0: closed_trades(s, "Date change")
    s.selected_date=day.isoformat(); s.real_data_json="[]"; s.candle_index=0; s.candle_progress=0
    s.secondary_data_json="[]"; s.secondary_needs_history_reload=True; s.secondary_active_time=""
    s.secondary_last_index=-1
    s.secondary_smooth_price=s.secondary_high=s.secondary_low=0
    s.is_playing=True; s.smooth_price=s.current_high=s.current_low=0; s.needs_history_reload=True; s.last_tick_at=time.time(); s.last_ltp=0
    db.session.commit(); return jsonify(status="success",selected_date=s.selected_date,is_playing=True)


@app.route("/api/set_speed", methods=["POST"])
@login_required
def set_speed():
    try: speed=int((request.get_json(silent=True) or {}).get("speed",1))
    except (TypeError,ValueError): return jsonify(status="error",message="Invalid speed."),400
    if speed not in SPEEDS: return jsonify(status="error",message="Unsupported replay speed."),400
    user,s=current_user_state(); s.playback_speed=speed; s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success",speed=speed)


@app.route("/api/toggle_play", methods=["POST"])
@login_required
def toggle_play():
    user,s=current_user_state(); s.is_playing=not s.is_playing; s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success",is_playing=s.is_playing)


@app.route("/api/step_candle", methods=["POST"])
@login_required
def step_candle():
    user,s=current_user_state(); rows=rows_of(s)
    if rows and s.candle_index<len(rows)-1:
        s.candle_index+=1; s.candle_progress=0; s.needs_history_reload=True; reset_bounds(s,rows); s.last_tick_at=time.time()
    db.session.commit(); return jsonify(status="success")


@app.route("/api/reset_sim", methods=["POST"])
@login_required
def reset_sim():
    user,s=current_user_state()
    if s.position and s.last_ltp>0: closed_trades(s, "Simulation reset")
    s.candle_index=0; s.candle_progress=0; s.is_playing=True; s.needs_history_reload=True
    s.secondary_data_json="[]"; s.secondary_needs_history_reload=True; s.secondary_active_time=""
    s.secondary_last_index=-1
    s.secondary_smooth_price=s.secondary_high=s.secondary_low=0
    s.smooth_price=s.current_high=s.current_low=0; s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success")


@app.route("/api/trade", methods=["POST"])
@login_required
def trade():
    d=request.get_json(silent=True) or {}; action=d.get("action")
    try: lots=int(d.get("lots",1)); sl=float(d.get("sl_points",20)); target=float(d.get("target_points",40)); trail=float(d.get("tsl_points",0))
    except (TypeError,ValueError): return jsonify(status="error",message="Trade inputs must be numbers."),400
    if action not in {"LONG","SHORT"} or not 1<=lots<=500 or any(not 0<=x<=100000 for x in (sl,target,trail)):
        return jsonify(status="error",message="Invalid trade input."),400
    user,s=current_user_state()
    if s.last_ltp<=0: return jsonify(status="error",message="Waiting for a valid market or replay price."),409
    if s.position: return jsonify(status="error",message="One paper position is already open."),409
    s.position=action;s.position_label=s.symbol;s.lots=lots;s.entry_price=round(s.last_ltp,2);s.highest_price=s.lowest_price=s.entry_price;s.trailing_points=trail
    s.stop_loss=round(s.entry_price-sl,2) if action=="LONG" and sl else round(s.entry_price+sl,2) if sl else 0
    s.target_price=round(s.entry_price+target,2) if action=="LONG" and target else round(s.entry_price-target,2) if target else 0
    db.session.commit();return jsonify(status="success")


@app.route("/api/close_trade", methods=["POST"])
@login_required
def close_trade():
    user,s=current_user_state()
    if not s.position:return jsonify(status="error",message="No open position."),409
    pnl=closed_trades(s);db.session.commit();return jsonify(status="success",final_pnl=pnl)


@app.route("/api/get_tick")
@login_required
def get_tick():
    user,s=current_user_state()
    with state_lock:
        try:
            lookback_days = max(1, min(int(request.args.get("lookback", "1")), max_history_days(s.interval)))
        except (TypeError, ValueError):
            lookback_days = 1
        if s.history_lookback_days != lookback_days:
            s.history_lookback_days = lookback_days
            s.real_data_json = "[]"
            s.candle_index = 0
            s.candle_progress = 0
            s.needs_history_reload = True
            s.last_tick_at = time.time()
            s.smooth_price = s.current_high = s.current_low = 0
            s.secondary_data_json = "[]"
            s.secondary_needs_history_reload = True
            s.secondary_last_index = -1
            s.secondary_active_time = ""
            s.secondary_smooth_price = s.secondary_high = s.secondary_low = 0
        rows=rows_of(s)
        if not rows:
            try: rows=fetch_candles(s.symbol,s.interval,s.selected_date,lookback_days)
            except ValueError as e:
                rows=[]
                history_error=str(e)
            except Exception as e:
                log.exception("Historical candle load failed")
                rows=[]
                history_error=str(e)
            rows = merge_recorded_live_candles(
                rows, s.user_id, s.symbol, INDICES[s.symbol]["exchange"],
                INDICES[s.symbol]["token"], s.interval, s.selected_date
            )
            if not rows:
                return jsonify(status="error",message=locals().get("history_error", "No candles for this date (holiday, weekend, or broker data limit).")),404
            s.real_data_json=json.dumps(rows,separators=(",",":"));s.candle_index=0;s.candle_progress=0;s.needs_history_reload=True;s.last_tick_at=time.time();reset_bounds(s,rows)
        s.candle_index=min(max(s.candle_index,0),len(rows)-1);previous_index=s.candle_index;now=time.time()
        if s.is_playing:
            candle_seconds = INTERVAL_SECONDS.get(s.interval, 300)
            s.candle_progress+=max(0,now-(s.last_tick_at or now))/candle_seconds*s.playback_speed
            while s.candle_progress>=1 and s.candle_index<len(rows)-1:
                s.candle_progress-=1;s.candle_index+=1;reset_bounds(s,rows)
            if s.candle_index==len(rows)-1 and s.candle_progress>=1:s.candle_progress=1;s.is_playing=False
        s.last_tick_at=now
        history=[];reload_history=s.needs_history_reload or request.args.get("bootstrap") == "1"
        if reload_history:history=[chart_candle(r) for r in rows[:s.candle_index]];s.needs_history_reload=False
        finalized=[] if reload_history else [chart_candle(r) for r in rows[previous_index:s.candle_index]] if s.candle_index>previous_index else []
        c=rows[s.candle_index];op,hi,lo,cl=map(float,c[1:5]);p=min(max(s.candle_progress,0),1)
        if p<.35:
            target=lo if cl>=op else hi;guide=op+(target-op)*(p/.35)
        elif p<.75:
            start=lo if cl>=op else hi;target=hi if cl>=op else lo;guide=start+(target-start)*((p-.35)/.40)
        else:
            start=hi if cl>=op else lo;guide=start+(cl-start)*min((p-.75)/.25,1)
        if s.smooth_price==0:s.smooth_price=op
        s.smooth_price+=(guide-s.smooth_price)*.15
        price=round(max(lo,min(hi,s.smooth_price)),2)
        if p>=.999:price=cl;s.current_high=hi;s.current_low=lo
        else:s.current_high=round(max(s.current_high,op,price),2);s.current_low=round(min(s.current_low,op,price),2)
        s.last_ltp=price
        if s.position=="LONG" and price>s.highest_price:
            s.highest_price=price
            if s.trailing_points>0:s.stop_loss=max(s.stop_loss,round(s.highest_price-s.trailing_points,2))
        elif s.position=="SHORT" and (price<s.lowest_price or s.lowest_price==0):
            s.lowest_price=price
            if s.trailing_points>0:
                candidate=round(s.lowest_price+s.trailing_points,2);s.stop_loss=min(s.stop_loss,candidate) if s.stop_loss else candidate
        ti=None
        if s.position=="LONG":
            if s.stop_loss>0 and price<=s.stop_loss: result=closed_trades(s);ti={"auto_closed":True,"reason":"Stop loss / trailing stop","pnl":result}
            elif s.target_price>0 and price>=s.target_price: result=closed_trades(s);ti={"auto_closed":True,"reason":"Target","pnl":result}
        elif s.position=="SHORT":
            if s.stop_loss>0 and price>=s.stop_loss: result=closed_trades(s);ti={"auto_closed":True,"reason":"Stop loss / trailing stop","pnl":result}
            elif s.target_price>0 and price<=s.target_price: result=closed_trades(s);ti={"auto_closed":True,"reason":"Target","pnl":result}
        if s.position and ti is None:
            qty=INDICES.get(s.symbol,INDICES["SENSEX"])["lot"]*s.lots;pnl=(price-s.entry_price)*qty if s.position=="LONG" else (s.entry_price-price)*qty;s.unrealized_pnl=round(pnl,2)
            ti={"position":s.position,"label":s.position_label,"qty":qty,"entry_price":s.entry_price,"sl_price":s.stop_loss,"target_price":s.target_price,"pnl":s.unrealized_pnl}
        dt=datetime.fromisoformat(c[0]);day_open=float(rows[0][1]);change=price-day_open;pct=change/day_open*100 if day_open else 0
        chart_dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt
        result={"status":"success","is_playing":bool(s.is_playing),"current_candle":{"time":int(chart_dt.timestamp()),"open":op,"high":s.current_high,"low":s.current_low,"close":price},"history":history,"finalized_candles":finalized,"trade_info":ti,"realized_pnl":s.realized_pnl,"trade_history":history_of(s),"symbol":s.symbol,"exchange":INDICES[s.symbol]["exchange"],"interval_label":INTERVALS.get(s.interval, "5m"),"change_value":round(change,2),"change_str":f"{'+' if change>=0 else ''}{change:.2f} ({pct:.2f}%)","selected_date":s.selected_date,"synced_time_str":dt.strftime("%d %b %Y  %H:%M:%S IST")}
        result["secondary_chart"] = secondary_chart_tick(
            s, c, p, request.args.get("bootstrap") == "1", lookback_days
        )
        db.session.commit();return jsonify(result)


@app.errorhandler(404)
def not_found(_):
    return (jsonify(status="error",message="Endpoint or data not found."),404) if request.path.startswith("/api/") else ("Page not found.",404)


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback();log.exception("Unhandled server error",exc_info=error)
    return (jsonify(status="error",message="Unexpected server error; check server log."),500) if request.path.startswith("/api/") else ("Unexpected server error; check server log.",500)


if __name__ == "__main__":
    print("Paper Trading Replay Terminal: http://127.0.0.1:5000")
    print("Default admin login: admin / admin123")
    app.run(host=os.environ.get("HOST","127.0.0.1"),port=int(os.environ.get("PORT","5000")),debug=False,use_reloader=False,threaded=True)
