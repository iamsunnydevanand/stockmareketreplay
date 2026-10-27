"""Single-file Paper Trading Replay Terminal.

Local: pip install Flask Flask-SQLAlchemy smartapi-python pyotp; python main.py
Render: set DATABASE_URL to Render PostgreSQL; install gunicorn and psycopg[binary];
        start with: gunicorn -w 2 --threads 4 -b 0.0.0.0:$PORT main:app
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
from collections import defaultdict, deque
from datetime import date, datetime, time as clock_time
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


class RuntimeSetting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255), nullable=False)


with app.app_context():
    db.create_all()
    # Lightweight additive migration for existing final_app.db deployments.
    user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
    with db.engine.begin() as connection:
        if "valid_until" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN valid_until DOUBLE PRECISION'))
        if "trial_started_at" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN trial_started_at DOUBLE PRECISION'))
        if "auth_version" not in user_columns:
            connection.execute(text('ALTER TABLE "user" ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0'))
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


def fetch_candles(symbol, interval, selected_date):
    key = (symbol, interval, selected_date)
    with cache_lock:
        if key in history_cache:
            return history_cache[key]
    day = date.fromisoformat(selected_date)
    now = datetime.now(IST)
    start = datetime.combine(day, clock_time(9, 15), tzinfo=IST)
    close = datetime.combine(day, clock_time(15, 30), tzinfo=IST)
    if day == now.date() and now < start:
        raise ValueError("Market opens at 09:15 IST. Select a past trading date or try after market open.")
    end = min(now, close) if day == now.date() else close
    params = {"exchange": INDICES[symbol]["exchange"], "symboltoken": INDICES[symbol]["token"],
              "interval": interval, "fromdate": f"{day.isoformat()} 09:15", "todate": end.strftime("%Y-%m-%d %H:%M")}
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
    with cache_lock:
        history_cache[key] = result
    return result


def reset_bounds(state, rows):
    if rows and 0 <= state.candle_index < len(rows):
        op = float(rows[state.candle_index][1])
        state.smooth_price = state.current_high = state.current_low = op


def chart_candle(row):
    dt = datetime.fromisoformat(row[0])
    return {"time": int(dt.timestamp()) + 19800, "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])}


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
</style></head><body><main class="box"><h1>Paper Trading Replay</h1><div class="muted">Sign in to your isolated replay workspace</div>{% if error %}<p class="error">{{ error }}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label>Username</label><input name="username" autocomplete="username" required maxlength="80"><label>Password</label><input name="password" type="password" autocomplete="current-password" required><button>Sign in</button></form><div class="link"><a href="{{ url_for('register') }}">Create account</a></div>{{ owner|safe }}</main></body></html>
"""

REGISTER_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create account</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#131722;color:#d1d4dc;font:15px Arial}main{width:min(420px,92vw);padding:30px;background:#1e222d;border:1px solid #2a2e39;border-radius:12px}label{display:block;margin:14px 0 6px}input{width:100%;padding:11px;background:#131722;border:1px solid #363a45;border-radius:5px;color:white}button{width:100%;padding:11px;margin-top:18px;background:#2962ff;color:white;border:0;border-radius:5px}.owner{display:flex;flex-direction:column;gap:5px;text-align:center;padding:13px;margin-top:18px;background:#131722;border:1px solid #2a2e39;border-radius:7px;font-size:12px}.owner a{color:#8aa9ff}.owner span{color:#9598a1} .owner-avatar{width:40px;height:40px;margin:0 auto;display:grid;place-items:center;border-radius:50%;background:linear-gradient(135deg,#2962ff,#8d57d6);color:#fff;font-weight:800;box-shadow:0 4px 14px #2962ff55}
</style></head><body><main><h1>Create account</h1>{% if error %}<p>{{ error }}</p>{% endif %}<form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label>Username</label><input name="username" minlength="3" maxlength="80" required><label>Password (10 characters minimum)</label><input name="password" type="password" minlength="10" required><button>Create account</button></form><p><a href="{{ url_for('login') }}">Back to sign in</a></p>{{ owner|safe }}</main></body></html>
"""

ADMIN_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{{ csrf }}"><title>Admin · Replay Terminal</title><style>
*{box-sizing:border-box}body{margin:0;background:#10131c;color:#e5e7ef;font:14px Arial,sans-serif}header{padding:16px 24px;background:#1e222d;border-bottom:1px solid #303644;display:flex;justify-content:space-between}header a{color:#a9baff;text-decoration:none}main{max-width:1200px;margin:28px auto;padding:0 18px}h1{margin-bottom:5px}.sub{color:#969baa;margin:0 0 20px}.table-wrap{overflow:auto;border:1px solid #303644;border-radius:12px;background:#1a1e28}table{width:100%;min-width:940px;border-collapse:collapse}th,td{text-align:left;padding:14px;border-bottom:1px solid #2b303b;vertical-align:middle}th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#9298a7}tr:last-child td{border-bottom:0}.username{font-size:15px;font-weight:700;color:#fff}.validity{margin-top:5px;color:#9ca3b2;font-size:12px}.grant{display:flex;gap:7px;align-items:center}.grant input{width:82px;padding:9px;background:#11151e;border:1px solid #394150;border-radius:6px;color:#fff}.btn{padding:9px 12px;background:#2962ff;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer;white-space:nowrap}.btn.secondary{background:#343a47}.status{display:inline-block;padding:5px 8px;border-radius:20px;background:#173b33;color:#55d5ae;font-size:11px}.status.off{background:#40242b;color:#ff8890}.password-result{margin-top:10px;min-width:235px;padding:9px;border:1px solid #42506a;background:#111724;border-radius:7px}.password-result small{display:block;color:#f3c66b;margin-bottom:6px}.password-result input{width:100%;padding:8px;background:#080b11;color:#b9d2ff;border:1px solid #30394a;border-radius:5px;font-family:monospace}.note{color:#9ca3b2;font-size:12px;margin:15px 0}.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);padding:11px 16px;background:#252b37;border:1px solid #485164;border-radius:7px;display:none} @media(max-width:700px){header{padding:13px}main{margin-top:18px}}
</style></head><body><header><b>Replay Terminal <span style="color:#839fff">· Admin Console</span></b><nav><a href="{{ url_for('home') }}">Terminal</a> &nbsp;·&nbsp; <a href="{{ url_for('logout') }}">Logout</a></nav></header><main><h1>User access & validity</h1><p class="sub">Issue or extend access for each registered username. The username never changes.</p><div class="table-wrap"><table><thead><tr><th>User</th><th>Access</th><th>Validity</th><th>Grant / extend</th><th>Account</th></tr></thead><tbody>{% for u in users %}<tr><td><span class="username">{{ u.username }}</span><div class="validity">ID #{{ u.id }}</div></td><td>{% if u.is_admin %}<span class="status">Administrator</span>{% elif u.valid_until and u.valid_until > now %}<span class="status">Full terminal</span>{% else %}<span class="status off">{{ 'Expired' if u.valid_until else 'Trial only' }}</span>{% endif %}<div id="secret-{{ u.id }}"></div></td><td>{% if u.valid_until %}<span id="validity-{{ u.id }}">{{ validity_labels[u.id] }}</span>{% else %}<span id="validity-{{ u.id }}">No paid access</span>{% endif %}</td><td>{% if not u.is_admin %}<form class="grant" data-user="{{ u.id }}" action="{{ url_for('grant_access',user_id=u.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label><input name="days" type="number" min="1" max="3650" value="30" aria-label="Validity days"></label><button class="btn">Generate pass + grant</button></form>{% else %}—{% endif %}</td><td>{% if not u.is_admin %}<form method="post" action="{{ url_for('toggle_user',user_id=u.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf }}"><button class="btn secondary">{{ 'Block' if u.is_active else 'Activate' }}</button></form>{% else %}<span class="status">Protected</span>{% endif %}</td></tr>{% endfor %}</tbody></table></div><p class="note">Generated passwords are shown once. Copy and share them securely. Reissuing a pass changes the user's password. Validity extends from the later of now or their current expiry.</p></main><div id="toast" class="toast"></div><script>
const csrf=document.querySelector('meta[name="csrf-token"]').content;function notify(s){const t=document.getElementById('toast');t.textContent=s;t.style.display='block';setTimeout(()=>t.style.display='none',4000)}document.querySelectorAll('.grant').forEach(form=>form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;try{const days=Number(form.querySelector('[name="days"]').value);const r=await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify({days})});const d=await r.json();if(!r.ok||d.status!=='success')throw Error(d.message||'Could not issue access');const box=document.getElementById('secret-'+form.dataset.user);box.innerHTML='<div class="password-result"><small>Copy this new password now</small><input readonly value="'+d.password+'"><button type="button" class="btn secondary copy-pass" style="margin-top:6px;width:100%">Copy password</button></div>';box.querySelector('.copy-pass').addEventListener('click',()=>navigator.clipboard.writeText(box.querySelector('input').value).then(()=>notify('Copied')).catch(()=>notify('Copy the password manually')));document.getElementById('validity-'+form.dataset.user).textContent=d.valid_until;notify('Access granted; username unchanged.')}catch(err){notify(err.message)}finally{button.disabled=false}}));
</script></body></html>
"""

HTML_PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf-token" content="{{ csrf }}"><title>Paper Trading Replay Terminal</title><script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script><style>
:root{--bg:#131722;--panel:#1e222d;--border:#2a2e39;--text:#d1d4dc;--muted:#787b86}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px Arial;height:100vh;overflow:hidden}.top{height:54px;background:var(--panel);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;padding:0 10px;white-space:nowrap;overflow:auto}.brand{font-weight:bold;color:white}.top select,.top input,.btn{height:31px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:0 8px}.btn{cursor:pointer}.gap{flex:1}.layout{height:calc(100vh - 54px);display:grid;grid-template-columns:44px minmax(0,1fr) 290px}.left{background:var(--panel);border-right:1px solid var(--border);display:flex;align-items:center;flex-direction:column;gap:8px;padding:10px 0}.icon{height:32px;width:32px;background:transparent;color:var(--text);border:1px solid transparent;cursor:pointer}.icon.active,.icon:hover{background:#2a2e39;border-color:#414755}.center{display:flex;flex-direction:column;min-width:0}.quote{height:58px;display:flex;align-items:center;gap:15px;padding:0 12px;border-bottom:1px solid var(--border)}.sym,.price{font-weight:bold;color:white}.price{font-size:18px}.chart{flex:1;min-height:0}.bottom{height:28px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 10px;color:var(--muted);font-size:11px}.right{background:var(--panel);border-left:1px solid var(--border);min-height:0;display:flex;flex-direction:column}.title{padding:15px;border-bottom:1px solid var(--border);font-weight:bold;color:white}.body{padding:12px;overflow:auto;display:flex;flex-direction:column;gap:12px}.card{padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px}.inputs{display:grid;grid-template-columns:1fr 1fr;gap:8px}.inputs label{font-size:11px;color:var(--muted)}.inputs input{width:100%;margin-top:4px;padding:8px;background:var(--panel);border:1px solid var(--border);color:white;border-radius:4px}.actions{display:flex;gap:8px;margin-top:10px}.buy,.sell,.square{flex:1;padding:9px;border:0;border-radius:4px;color:white;font-weight:bold;cursor:pointer}.buy{background:#089981}.sell{background:#f23645}.square{background:#dd9226}.row{display:flex;justify-content:space-between;margin:8px 0}.row span{color:var(--muted)}.history{display:flex;flex-direction:column;gap:6px;margin-top:8px}.item{display:flex;justify-content:space-between;padding:8px;background:var(--panel);border-radius:4px;font-size:11px}.green{color:#089981}.red{color:#f23645}.toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%);display:none;background:#252a36;border:1px solid #454b58;border-radius:6px;padding:10px;z-index:10}.overlay{position:fixed;inset:0;background:#000c;display:none;align-items:center;justify-content:center;z-index:20}.overlaybox{max-width:430px;width:92vw;padding:25px;background:var(--panel);border:1px solid var(--border);border-radius:12px;text-align:center}.owner{display:flex;flex-direction:column;align-items:center;gap:7px;margin-top:15px;padding:15px;border:1px solid var(--border);border-radius:9px;text-align:center}.owner a{color:#9db5ff;text-decoration:none}.owner a:hover{text-decoration:underline}.owner span,.owner-caption{color:var(--muted)}.owner-avatar{width:42px;height:42px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,#2962ff,#8d57d6);color:#fff;font-weight:800;font-size:15px;box-shadow:0 4px 16px #2962ff55}.owner-panel{background:linear-gradient(145deg,#1b2940,#11151e);border-color:#34517d}.owner-title{font-weight:700;color:#f2c66d;margin-bottom:10px}.owner-panel .owner{margin-top:0;background:#0e1420aa;border-color:#354765}.owner-panel .owner b{color:#fff}body.light{--bg:#fff;--panel:#f4f5f8;--border:#e0e3eb;--text:#131722;--muted:#667085}body.light .brand,body.light .sym,body.light .title{color:#131722}body.light .inputs input,body.light .item{background:#fff;color:#131722}@media(max-width:900px){.layout{grid-template-columns:40px minmax(0,1fr)}.right{display:none}}
</style></head><body><header class="top"><span class="brand">REPLAY TERMINAL</span><select id="symbol"><option value="SENSEX" {% if selected_symbol == 'SENSEX' %}selected{% endif %}>SENSEX</option><option value="NIFTY" {% if selected_symbol == 'NIFTY' %}selected{% endif %}>NIFTY</option><option value="BANKNIFTY" {% if selected_symbol == 'BANKNIFTY' %}selected{% endif %}>BANKNIFTY</option></select><select id="interval"><option value="ONE_MINUTE" {% if selected_interval == 'ONE_MINUTE' %}selected{% endif %}>1m</option><option value="FIVE_MINUTE" {% if selected_interval == 'FIVE_MINUTE' %}selected{% endif %}>5m</option><option value="FIFTEEN_MINUTE" {% if selected_interval == 'FIFTEEN_MINUTE' %}selected{% endif %}>15m</option></select><input id="date" type="date" value="{{ selected_date }}" max="{{ max_date }}"><button class="btn" id="play">⏸ Pause</button><button class="btn" id="step">Step ▸</button><button class="btn" id="reset">↺ Reset</button><select id="speed"><option value="1">1x</option><option value="2">2x</option><option value="5">5x</option><option value="10">10x</option><option value="25">25x</option><option value="50">50x</option></select><button class="btn" id="theme">Day/Night</button><span class="gap"></span><span id="trial">{{ access_label }}</span><span>{{ username }}</span>{% if is_admin %}<a class="btn" href="{{ url_for('admin_panel') }}">Admin</a>{% endif %}<a class="btn" href="{{ url_for('logout') }}">Logout</a></header><div class="layout"><aside class="left"><button class="icon active" title="Crosshair">⌖</button><button class="icon" id="line" title="Horizontal line">―</button><button class="icon" id="fit" title="Fit chart">⤢</button></aside><main class="center"><section class="quote"><span class="sym" id="name">Loading…</span><span class="price" id="ltp">—</span><span id="change">—</span><span id="ohlc">O — H — L — C —</span></section><div id="chart" class="chart"></div><footer class="bottom"><span id="clock">Historical replay</span><span>Original broker OHLC · Paper trading only</span><span>Realized PnL <b id="realized">₹0.00</b></span></footer></main><aside class="right"><div class="title">Paper Trading</div><div class="body"><section class="card"><div class="inputs"><label>Lots<input id="lots" type="number" min="1" max="500" value="1"></label><label>SL points<input id="sl" type="number" min="0" value="20"></label><label>Target points<input id="target" type="number" min="0" value="40"></label><label>Trailing points<input id="trail" type="number" min="0" value="0"></label></div><div class="actions"><button class="sell" id="short">SELL</button><button class="buy" id="long">BUY</button></div></section><section class="card"><b>Open position</b><div id="none" style="color:var(--muted);margin-top:10px">No open position.</div><div id="pos" style="display:none"><div class="row"><span>Instrument</span><b id="psym">—</b></div><div class="row"><span>Side / Qty</span><b id="side">—</b></div><div class="row"><span>Entry</span><b id="entry">—</b></div><div class="row"><span>LTP</span><b id="pltp">—</b></div><div class="row"><span>SL</span><b id="psl">—</b></div><div class="row"><span>Target</span><b id="ptarget">—</b></div><div class="row"><span>Live PnL</span><b id="pnl">—</b></div><button class="square" id="square">Square off</button></div></section><section><b>Closed trades</b><div class="history" id="history">No closed trades.</div></section><section class="card owner-panel"><div class="owner-title">Built by</div>{{ owner|safe }}</section></div></aside></div><div id="toast" class="toast"></div><div id="overlay" class="overlay"><div class="overlaybox"><h2>Free trial ended</h2><p>Your two-minute trial has ended. Contact the owner for access.</p>{{ owner|safe }}<a class="btn" href="{{ url_for('logout') }}">Sign out</a></div></div>
<script>
(function(){
const csrf=document.querySelector('meta[name="csrf-token"]').content,E=id=>document.getElementById(id);let chart,series,bars=[],busy=false,trial={{ trial }},fullAccess={{ 'true' if full_access else 'false' }},draw=false,lines=[];
async function api(url,opts){opts=opts||{};opts.headers=Object.assign({},opts.headers||{}, {'X-CSRFToken':csrf});if(opts.body&&typeof opts.body==='object'){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.body)}const r=await fetch(url,opts);let d={};try{d=await r.json()}catch(_){}if(!r.ok||d.status==='error')throw Error(d.message||('Request failed ('+r.status+')'));return d}
function toast(s){const t=E('toast');t.textContent=s;t.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.style.display='none',3500)}function f(n){return Number(n||0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}function safe(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function initChart(){const box=E('chart');chart=LightweightCharts.createChart(box,{width:box.clientWidth,height:box.clientHeight,layout:{background:{color:'#131722'},textColor:'#d1d4dc',fontSize:12},grid:{vertLines:{color:'#252a36'},horzLines:{color:'#252a36'}},timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#2a2e39',rightOffset:6},rightPriceScale:{borderColor:'#2a2e39'},crosshair:{mode:LightweightCharts.CrosshairMode.Normal}});series=chart.addCandlestickSeries({upColor:'#089981',downColor:'#f23645',borderVisible:false,wickUpColor:'#089981',wickDownColor:'#f23645'});window.addEventListener('resize',()=>chart.applyOptions({width:box.clientWidth,height:box.clientHeight}));chart.subscribeClick(p=>{if(draw&&p.point){let price=series.coordinateToPrice(p.point.y);if(price)series.createPriceLine({price,color:'#2962ff',lineStyle:LightweightCharts.LineStyle.Dashed,title:'S/R',axisLabelVisible:true});draw=false;E('line').classList.remove('active')}})}
function drawPosition(t){lines.forEach(x=>series.removePriceLine(x));lines=[];if(!t||!t.position)return;lines.push(series.createPriceLine({price:t.entry_price,color:'#2962ff',lineWidth:2,title:'ENTRY'}));if(t.sl_price>0)lines.push(series.createPriceLine({price:t.sl_price,color:'#f23645',lineStyle:2,title:'SL'}));if(t.target_price>0)lines.push(series.createPriceLine({price:t.target_price,color:'#089981',lineStyle:2,title:'TARGET'}))}
function render(d){const c=d.current_candle;if(d.history&&d.history.length){bars=d.history;series.setData(bars)}if(c){series.update(c);if(bars.length&&bars[bars.length-1].time===c.time)bars[bars.length-1]=c;else bars.push(c);E('name').textContent=d.symbol+' · '+d.interval_label+' · '+d.exchange;E('ltp').textContent=f(c.close);E('change').textContent=d.change_str;E('ohlc').textContent='O '+f(c.open)+' H '+f(c.high)+' L '+f(c.low)+' C '+f(c.close);E('clock').textContent=d.synced_time_str}
E('realized').textContent=(d.realized_pnl>=0?'+₹':'−₹')+f(Math.abs(d.realized_pnl));E('play').textContent=d.is_playing?'⏸ Pause':'▶ Play';const t=d.trade_info,open=!!(t&&t.position);E('none').style.display=open?'none':'block';E('pos').style.display=open?'block':'none';if(open&&c){E('psym').textContent=t.label;E('side').textContent=t.position+' / '+t.qty;E('entry').textContent=f(t.entry_price);E('pltp').textContent=f(c.close);E('psl').textContent=t.sl_price?f(t.sl_price):'—';E('ptarget').textContent=t.target_price?f(t.target_price):'—';E('pnl').textContent=(t.pnl>=0?'+₹':'−₹')+f(Math.abs(t.pnl));drawPosition(t)}else drawPosition(null);if(!d.trade_history||!d.trade_history.length)E('history').textContent='No closed trades.';else E('history').innerHTML=d.trade_history.slice(0,30).map(x=>'<div class="item"><span><b>'+safe(x.symbol)+'</b> '+safe(x.type)+'<br>'+safe(x.reason)+' · '+f(x.entry)+' → '+f(x.exit)+'</span><b class="'+(x.pnl>=0?'green':'red')+'">'+(x.pnl>=0?'+':'−')+'₹'+f(Math.abs(x.pnl))+'</b></div>').join('');if(t&&t.auto_closed)toast('Closed: '+t.reason+' · PnL ₹'+f(t.pnl))}
async function tick(){if(busy||trial<=0)return;busy=true;try{render(await api('/api/get_tick'))}catch(e){E('name').textContent='Data unavailable';if(e.message.indexOf('Access expired')===0){trial=0;E('overlay').style.display='flex'}toast(e.message)}finally{busy=false}}
async function doRun(fn){try{await fn();await tick()}catch(e){toast(e.message)}}async function change(url,data){bars=[];series.setData([]);await api(url,{method:'POST',body:data||{}});await tick()}
E('symbol').addEventListener('change',()=>doRun(()=>change('/api/change_symbol',{symbol:E('symbol').value})));E('interval').addEventListener('change',()=>doRun(()=>change('/api/set_interval',{interval:E('interval').value})));E('date').addEventListener('change',()=>doRun(()=>change('/api/set_date',{date:E('date').value})));E('speed').addEventListener('change',()=>doRun(()=>api('/api/set_speed',{method:'POST',body:{speed:Number(E('speed').value)}})));E('play').addEventListener('click',()=>doRun(()=>api('/api/toggle_play',{method:'POST',body:{}})));E('step').addEventListener('click',()=>doRun(()=>api('/api/step_candle',{method:'POST',body:{}})));E('reset').addEventListener('click',()=>doRun(()=>change('/api/reset_sim',{})));E('fit').addEventListener('click',()=>chart.timeScale().fitContent());E('line').addEventListener('click',()=>{draw=!draw;E('line').classList.toggle('active',draw)});
E('theme').addEventListener('click',()=>{document.body.classList.toggle('light');const l=document.body.classList.contains('light');chart.applyOptions({layout:{background:{color:l?'#fff':'#131722'},textColor:l?'#131722':'#d1d4dc'}})});
async function order(side){await doRun(()=>api('/api/trade',{method:'POST',body:{action:side,lots:Number(E('lots').value),sl_points:Number(E('sl').value),target_points:Number(E('target').value),tsl_points:Number(E('trail').value)}}))}E('long').addEventListener('click',()=>order('LONG'));E('short').addEventListener('click',()=>order('SHORT'));E('square').addEventListener('click',()=>doRun(()=>api('/api/close_trade',{method:'POST',body:{}})));
if(!fullAccess)setInterval(()=>{if(trial>0){trial--;E('trial').textContent='Trial '+Math.floor(trial/60)+':'+String(trial%60).padStart(2,'0')}if(trial<=0){E('overlay').style.display='flex';E('trial').textContent='Trial ended'}},1000);initChart();tick();setInterval(tick,100)
})();
</script></body></html>
"""


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
        selected_date=state.selected_date, selected_symbol=state.symbol, selected_interval=state.interval,
        max_date=datetime.now(IST).date().isoformat(), csrf=csrf_token(), owner=OWNER_INFO_HTML)


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
        s.symbol=symbol; s.real_data_json="[]"; s.candle_index=0; s.candle_progress=0
        s.is_playing=True; s.needs_history_reload=True; s.smooth_price=s.current_high=s.current_low=0
        s.last_tick_at=time.time(); db.session.commit()
    return jsonify(status="success")


@app.route("/api/set_interval", methods=["POST"])
@login_required
def set_interval():
    interval=(request.get_json(silent=True) or {}).get("interval")
    if interval not in INTERVALS: return jsonify(status="error", message="Unsupported timeframe."), 400
    user,s=current_user_state(); s.interval=interval; s.real_data_json="[]"; s.candle_index=0; s.candle_progress=0
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
        s.candle_index+=1; s.candle_progress=0; reset_bounds(s,rows); s.last_tick_at=time.time()
    db.session.commit(); return jsonify(status="success")


@app.route("/api/reset_sim", methods=["POST"])
@login_required
def reset_sim():
    user,s=current_user_state()
    if s.position and s.last_ltp>0: closed_trades(s, "Simulation reset")
    s.candle_index=0; s.candle_progress=0; s.is_playing=True; s.needs_history_reload=True
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
    if not rows_of(s) or s.last_ltp<=0: return jsonify(status="error",message="Historical candles are still loading."),409
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
        rows=rows_of(s)
        if not rows:
            try: rows=fetch_candles(s.symbol,s.interval,s.selected_date)
            except ValueError as e:return jsonify(status="error",message=str(e)),409
            except Exception as e:
                log.exception("Historical candle load failed")
                return jsonify(status="error",message=str(e)),503
            if not rows:return jsonify(status="error",message="No candles for this date (holiday, weekend, or broker data limit)."),404
            s.real_data_json=json.dumps(rows,separators=(",",":"));s.candle_index=0;s.candle_progress=0;s.needs_history_reload=True;s.last_tick_at=time.time();reset_bounds(s,rows)
        s.candle_index=min(max(s.candle_index,0),len(rows)-1);now=time.time()
        if s.is_playing:
            s.candle_progress+=max(0,now-(s.last_tick_at or now))/300.0*s.playback_speed
            while s.candle_progress>=1 and s.candle_index<len(rows)-1:
                s.candle_progress-=1;s.candle_index+=1;reset_bounds(s,rows)
            if s.candle_index==len(rows)-1 and s.candle_progress>=1:s.candle_progress=1;s.is_playing=False
        s.last_tick_at=now
        history=[]
        if s.needs_history_reload:history=[chart_candle(r) for r in rows[:s.candle_index]];s.needs_history_reload=False
        c=rows[s.candle_index];op,hi,lo,cl=map(float,c[1:5]);p=min(max(s.candle_progress,0),1)
        if p<.35:
            target=lo if cl>=op else hi;guide=op+(target-op)*(p/.35)
        elif p<.75:
            start=lo if cl>=op else hi;target=hi if cl>=op else lo;guide=start+(target-start)*((p-.35)/.40)
        else:
            start=hi if cl>=op else lo;guide=start+(cl-start)*min((p-.75)/.25,1)
        if s.smooth_price==0:s.smooth_price=op
        noise=random.uniform(-.05,.05)*max(1,(hi-lo)*.01)
        s.smooth_price+=(guide-s.smooth_price)*.15+noise
        price=round(max(lo,min(hi,s.smooth_price)),2)
        if p>.94:price=cl;s.current_high=hi;s.current_low=lo
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
        result={"status":"success","is_playing":bool(s.is_playing),"current_candle":{"time":int(dt.timestamp())+19800,"open":op,"high":s.current_high,"low":s.current_low,"close":price},"history":history,"trade_info":ti,"realized_pnl":s.realized_pnl,"trade_history":history_of(s),"symbol":s.symbol,"exchange":INDICES[s.symbol]["exchange"],"interval_label":INTERVALS.get(s.interval, "5m"),"change_value":round(change,2),"change_str":f"{'+' if change>=0 else ''}{change:.2f} ({pct:.2f}%)","selected_date":s.selected_date,"synced_time_str":dt.strftime("%d %b %Y  %H:%M:%S IST")}
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

