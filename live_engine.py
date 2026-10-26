import os
from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from SmartApi import SmartConnect
from pyotp import TOTP
from datetime import datetime
import time
import random

app = Flask(__name__)
app.secret_key = "sunny_dev_anand_secret_123"

# Render ke liye database path
db_path = os.environ.get('DATABASE_PATH', 'final_app.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- DATABASE MODEL ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)

with app.app_context():
    db.create_all()
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", password=generate_password_hash("admin123"), is_admin=True)
        db.session.add(admin)
        db.session.commit()

# --- APNI SMARTAPI DETAILS ---
api_key = "BAq0de8E"                 
client_id = "S1263568"               
password = "7313"                    
totp_secret = "LY4QG5NPAOBDYIZXODIDWLQKN4"  

obj = SmartConnect(api_key=api_key)
session_initialized = False

# --- ALL MAJOR INDIAN INDICES (NSE & BSE) ---
INDICES = {
    "SENSEX":     {"token": "99919000", "exchange": "BSE", "name": "SENSEX",     "lot": 10, "step": 100},
    "BANKEX":     {"token": "99919001", "exchange": "BSE", "name": "BANKEX",     "lot": 15, "step": 100},
    "NIFTY":      {"token": "99926000", "exchange": "NSE", "name": "NIFTY 50",   "lot": 50, "step": 50},
    "BANKNIFTY":  {"token": "99926009", "exchange": "NSE", "name": "BANKNIFTY",  "lot": 15, "step": 100},
    "FINNIFTY":   {"token": "99926037", "exchange": "NSE", "name": "FINNIFTY",   "lot": 40, "step": 50},
    "MIDCPNIFTY": {"token": "99926074", "exchange": "NSE", "name": "MIDCPNIFTY", "lot": 75, "step": 25},
    "NIFTYIT":    {"token": "99926008", "exchange": "NSE", "name": "NIFTY IT",   "lot": 25, "step": 100},
    "NIFTYAUTO":  {"token": "99926029", "exchange": "NSE", "name": "NIFTY AUTO", "lot": 25, "step": 100},
    "NIFTYPHARMA":{"token": "99926023", "exchange": "NSE", "name": "NIFTY PHARMA","lot": 25, "step": 50}
}

# --- GLOBAL STATE (Data fetching & simulation) ---
current_symbol = "SENSEX"
current_interval = "FIVE_MINUTE"
selected_date = "2026-10-26" 
real_data = []
current_candle_index = 0
candle_progress = 0.0 
last_real_time = time.time()
is_playing = True
playback_speed = 1 
needs_history_reload = True

# Smooth Tick State
smooth_price = 0.0
current_high = 0.0
current_low = 0.0

# Paper Trading State (Global as per original code)
position = None          
instrument_label = "SENSEX"
entry_price = 0.0
sl_price = 0.0
target_price = 0.0
tsl_points = 0.0
highest_price = 0.0      
lowest_price = 0.0
qty_lots = 1
pnl = 0.0
realized_pnl = 0.0
last_ltp = 0.0
trade_history = []

def ensure_login():
    global session_initialized
    if not session_initialized:
        try:
            totp = TOTP(totp_secret).now()
            res = obj.generateSession(client_id, password, totp)
            if res and res.get('status'): session_initialized = True
        except: pass

def format_candle(c):
    ts = int(datetime.fromisoformat(c[0]).timestamp()) + 19800
    return {"time": ts, "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}

def reset_candle_bounds():
    global smooth_price, current_high, current_low
    if len(real_data) > 0 and current_candle_index < len(real_data):
        o = float(real_data[current_candle_index][1])
        smooth_price = o
        current_high = o
        current_low = o

def execute_square_off(exit_price, reason="Manual"):
    global position, instrument_label, entry_price, sl_price, target_price, tsl_points, pnl, realized_pnl, trade_history
    if position is None: return 0.0
    lot_size = INDICES[current_symbol]["lot"]
    total_qty = lot_size * qty_lots
    final_pnl = (exit_price - entry_price) * total_qty if position == 'LONG' else (entry_price - exit_price) * total_qty
    final_pnl = round(final_pnl, 2)
    realized_pnl = round(realized_pnl + final_pnl, 2)
    trade_history.insert(0, {
        "symbol": instrument_label, "type": position, "qty": total_qty,
        "entry": round(entry_price, 2), "exit": round(exit_price, 2),
        "pnl": final_pnl, "reason": reason
    })
    position = None; entry_price = 0.0; sl_price = 0.0; target_price = 0.0; tsl_points = 0.0; pnl = 0.0
    return final_pnl

# --- OWNER INFO HTML ---
OWNER_HTML = """
<div style="margin-top:20px; padding:15px; background:#131722; border:1px solid #2962ff; border-radius:8px; text-align:center; font-size:12px;">
    <h4 style="margin:0 0 5px 0; color:#089981;">Owner Info</h4>
    <p style="margin:2px; font-weight:bold; color:white;">Sunny Dev Anand</p>
    <p style="margin:2px; color:#787b86;">Software Engineer</p>
    <p style="margin:2px;"><a href="mailto:itsmywayokok@gmail.com" style="color:#2962ff; text-decoration:none;">itsmywayokok@gmail.com</a></p>
    <p style="margin:2px;"><a href="https://wa.me/917667765215" style="color:#089981; text-decoration:none;">+91 7667765215 (WhatsApp)</a></p>
</div>
"""

# --- AUTH ROUTES ---
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if session.get('is_admin'):
        return redirect(url_for('admin_panel'))
    return redirect(url_for('home'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if not user.is_active:
                return render_template_string(LOGIN_HTML, error="Account blocked by Admin.", owner=OWNER_HTML)
            session['user_id'] = user.id
            session['username'] = user.username
            session['is_admin'] = user.is_admin
            session['login_time'] = datetime.now().timestamp()
            if user.is_admin: return redirect(url_for('admin_panel'))
            return redirect(url_for('home'))
        return render_template_string(LOGIN_HTML, error="Invalid Credentials", owner=OWNER_HTML)
    return render_template_string(LOGIN_HTML, error=None, owner=OWNER_HTML)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            return render_template_string(REGISTER_HTML, error="Username exists", owner=OWNER_HTML)
        new_user = User(username=username, password=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template_string(REGISTER_HTML, error=None, owner=OWNER_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'): return redirect(url_for('login'))
    users = User.query.all()
    return render_template_string(ADMIN_HTML, users=users)

@app.route('/admin/toggle/<int:user_id>')
def toggle_user(user_id):
    if session.get('is_admin'):
        user = User.query.get(user_id)
        if user: user.is_active = not user.is_active; db.session.commit()
    return redirect(url_for('admin_panel'))

# --- TERMINAL ROUTE ---
@app.route('/terminal')
def home():
    if 'user_id' not in session: return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if not user or not user.is_active: session.clear(); return redirect(url_for('login'))
    
    # Admin ko full access
    if user.is_admin:
        return render_template_string(HTML_PAGE, trial=999999, username=session['username'])
        
    trial_remaining = max(0, 120 - int(time.time() - session.get('login_time', time.time())))
    return render_template_string(HTML_PAGE, trial=trial_remaining, username=session['username'])

# --- API ROUTES ---
@app.route('/api/change_symbol/<symbol>')
def change_symbol(symbol):
    global current_symbol, real_data, current_candle_index, candle_progress, needs_history_reload
    if symbol in INDICES:
        if position is not None and last_ltp > 0: execute_square_off(last_ltp, reason="Symbol Switch")
        current_symbol = symbol
        real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})

@app.route('/api/set_interval/<interval>')
def set_interval(interval):
    global current_interval, real_data, current_candle_index, candle_progress, needs_history_reload
    current_interval = interval
    real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
    return jsonify({"status": "success"})

@app.route('/api/set_speed/<int:speed>')
def set_speed(speed):
    global playback_speed, last_real_time
    playback_speed = speed; last_real_time = time.time()
    return jsonify({"status": "success", "speed": playback_speed})

@app.route('/api/set_date')
def set_date():
    global selected_date, real_data, current_candle_index, candle_progress, needs_history_reload
    selected_date = request.args.get('date')
    real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
    return jsonify({"status": "success", "selected_date": selected_date})

@app.route('/api/toggle_play', methods=['POST'])
def toggle_play():
    global is_playing, last_real_time
    is_playing = not is_playing; last_real_time = time.time()
    return jsonify({"status": "success", "is_playing": is_playing})

@app.route('/api/step_candle', methods=['POST'])
def step_candle():
    global current_candle_index, candle_progress, last_real_time
    if len(real_data) > 0 and current_candle_index < len(real_data) - 1:
        current_candle_index += 1
        candle_progress = 0.0
        reset_candle_bounds()
        last_real_time = time.time()
    return jsonify({"status": "success"})

@app.route('/api/reset_sim', methods=['POST'])
def reset_sim():
    global current_candle_index, candle_progress, needs_history_reload, last_real_time, is_playing
    if position is not None and last_ltp > 0: execute_square_off(last_ltp, reason="Day Reset")
    current_candle_index = 0; candle_progress = 0.0; is_playing = True
    reset_candle_bounds(); needs_history_reload = True; last_real_time = time.time()
    return jsonify({"status": "success", "selected_date": selected_date})

@app.route('/api/get_tick')
def get_tick():
    global real_data, current_candle_index, candle_progress, last_real_time, is_playing, playback_speed
    global position, instrument_label, entry_price, sl_price, target_price, tsl_points, highest_price, lowest_price, qty_lots, pnl, realized_pnl, last_ltp
    global current_symbol, selected_date, current_interval, needs_history_reload, session_initialized
    global smooth_price, current_high, current_low

    if len(real_data) == 0:
        try:
            ensure_login()
            idx = INDICES[current_symbol]
            params = {
                "exchange": idx["exchange"], "symboltoken": idx["token"], "interval": current_interval,
                "fromdate": f"{selected_date} 09:15", "todate": f"{selected_date} 15:30"
            }
            response = obj.getCandleData(params)
            if response and response.get('status') and response.get('data'):
                real_data = response['data']
                current_candle_index = 0; candle_progress = 0.0
                reset_candle_bounds(); needs_history_reload = True; last_real_time = time.time()
            else:
                session_initialized = False
                return jsonify({"status": "error", "message": "No data found."})
        except Exception as e:
            session_initialized = False
            return jsonify({"status": "error", "message": str(e)})

    if len(real_data) == 0: return jsonify({"status": "error"})
    if current_candle_index >= len(real_data):
        current_candle_index = len(real_data) - 1
        is_playing = False

    now = time.time()
    if is_playing:
        candle_progress += ((now - last_real_time) / 12.0) * playback_speed
        last_real_time = now
        if candle_progress >= 1.0:
            candle_progress = 0.0
            if current_candle_index < len(real_data) - 1:
                current_candle_index += 1
                reset_candle_bounds()
            else:
                is_playing = False
    else:
        last_real_time = now

    history_payload = []
    if needs_history_reload:
        history_payload = [format_candle(row) for row in real_data[:current_candle_index]]
        needs_history_reload = False

    c = real_data[current_candle_index]
    open_p, high_p, low_p, close_p = float(c[1]), float(c[2]), float(c[3]), float(c[4])

    if candle_progress < 0.35:
        target_p = low_p if close_p >= open_p else high_p
        guide_p = open_p + (target_p - open_p) * (candle_progress / 0.35)
    elif candle_progress < 0.75:
        start_p = low_p if close_p >= open_p else high_p
        target_p = high_p if close_p >= open_p else low_p
        guide_p = start_p + (target_p - start_p) * ((candle_progress - 0.35) / 0.40)
    else:
        start_p = high_p if close_p >= open_p else low_p
        guide_p = start_p + (close_p - start_p) * min((candle_progress - 0.75) / 0.25, 1.0)

    if smooth_price == 0.0: smooth_price = open_p
    micro_noise = random.uniform(-0.35, 0.35) * max(1.0, (high_p - low_p) * 0.02)
    smooth_price = smooth_price + (guide_p - smooth_price) * 0.35 + micro_noise
    current_price = round(max(low_p, min(high_p, smooth_price)), 2)

    if candle_progress > 0.94:
        current_price = close_p; current_high = high_p; current_low = low_p
    else:
        current_high = round(max(current_high, open_p, current_price), 2)
        current_low = round(min(current_low, open_p, current_price), 2)

    last_ltp = current_price

    if position == 'LONG':
        if current_price > highest_price:
            highest_price = current_price
            if tsl_points > 0:
                new_tsl = highest_price - tsl_points
                if new_tsl > sl_price: sl_price = round(new_tsl, 2)
    elif position == 'SHORT':
        if current_price < lowest_price or lowest_price == 0:
            lowest_price = current_price
            if tsl_points > 0:
                new_tsl = lowest_price + tsl_points
                if new_tsl < sl_price or sl_price == 0: sl_price = round(new_tsl, 2)

    trade_info = None
    if position == 'LONG':
        if sl_price > 0 and current_price <= sl_price:
            closed_pnl = execute_square_off(sl_price, reason="Stop Loss / TSL")
            trade_info = {"auto_closed": True, "reason": "Stop Loss / TSL", "pnl": closed_pnl}
        elif target_price > 0 and current_price >= target_price:
            closed_pnl = execute_square_off(target_price, reason="Target")
            trade_info = {"auto_closed": True, "reason": "Target", "pnl": closed_pnl}
    elif position == 'SHORT':
        if sl_price > 0 and current_price >= sl_price:
            closed_pnl = execute_square_off(sl_price, reason="Stop Loss / TSL")
            trade_info = {"auto_closed": True, "reason": "Stop Loss / TSL", "pnl": closed_pnl}
        elif target_price > 0 and current_price <= target_price:
            closed_pnl = execute_square_off(target_price, reason="Target")
            trade_info = {"auto_closed": True, "reason": "Target", "pnl": closed_pnl}

    if position is not None and trade_info is None:
        lot_size = INDICES[current_symbol]["lot"]
        total_qty = lot_size * qty_lots
        pnl = (current_price - entry_price) * total_qty if position == 'LONG' else (entry_price - current_price) * total_qty
        trade_info = {
            "auto_closed": False, "position": position, "label": instrument_label, "qty": total_qty,
            "entry_price": entry_price, "sl_price": sl_price, "target_price": target_price, "pnl": round(pnl, 2)
        }

    candle_dt = datetime.fromisoformat(c[0])
    interval_seconds_map = {"ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "ONE_HOUR": 3600}
    total_sec = interval_seconds_map.get(current_interval, 300)
    elapsed_sec = int(min(candle_progress, 0.99) * total_sec)
    sim_dt = datetime.fromtimestamp(candle_dt.timestamp() + elapsed_sec)
    synced_time_str = f"{sim_dt.strftime('%d %b %Y  %H:%M:%S')} UTC+5:30"

    current_candle = {
        "time": int(candle_dt.timestamp()) + 19800,
        "open": open_p, "high": current_high, "low": current_low, "close": current_price
    }

    interval_map = {"ONE_MINUTE": "1", "FIVE_MINUTE": "5", "FIFTEEN_MINUTE": "15", "ONE_HOUR": "60"}
    interval_name = interval_map.get(current_interval, "5")
    exchange_name = INDICES[current_symbol]["exchange"]

    day_open = float(real_data[0][1])
    change_val = current_price - day_open
    change_pct = (change_val / day_open) * 100 if day_open != 0 else 0
    change_str = f"{'+' if change_val >= 0 else ''}{change_val:.2f} ({change_pct:.2f}%)"

    return jsonify({
        "status": "success", "is_playing": is_playing, "current_candle": current_candle,
        "history": history_payload, "trade_info": trade_info, "realized_pnl": realized_pnl,
        "trade_history": trade_history, "symbol": INDICES[current_symbol]["name"],
        "step_size": INDICES[current_symbol]["step"], "interval_name": interval_name,
        "exchange": exchange_name, "change_str": change_str, "selected_date": selected_date,
        "synced_time_str": synced_time_str
    })

@app.route('/api/trade', methods=['POST'])
def trade():
    global position, instrument_label, entry_price, sl_price, target_price, tsl_points, highest_price, lowest_price, qty_lots, last_ltp
    data = request.get_json() or {}
    action = data.get('action')
    lots = int(data.get('lots', 1))
    sl_points = float(data.get('sl_points', 20))
    target_points = float(data.get('target_points', 40))
    tsl_points = float(data.get('tsl_points', 0))
    custom_label = data.get('label')

    if len(real_data) == 0 or last_ltp == 0:
        return jsonify({"status": "error", "message": "Market data loading..."})
    if position is not None:
        return jsonify({"status": "error", "message": "Position already open!"})

    position = action
    instrument_label = custom_label if custom_label else INDICES[current_symbol]["name"]
    qty_lots = max(1, lots)
    entry_price = round(last_ltp, 2)
    highest_price = entry_price; lowest_price = entry_price

    if action == 'LONG':
        sl_price = round(entry_price - sl_points, 2) if sl_points > 0 else 0.0
        target_price = round(entry_price + target_points, 2) if target_points > 0 else 0.0
    else:
        sl_price = round(entry_price + sl_points, 2) if sl_points > 0 else 0.0
        target_price = round(entry_price - target_points, 2) if target_points > 0 else 0.0

    lot_size = INDICES[current_symbol]["lot"]
    trade_info = {
        "position": position, "label": instrument_label, "qty": lot_size * qty_lots,
        "entry_price": entry_price, "sl_price": sl_price, "target_price": target_price, "pnl": 0.0
    }
    return jsonify({"status": "success", "trade_info": trade_info})

@app.route('/api/close_trade', methods=['POST'])
def close_trade():
    if position is None:
        return jsonify({"status": "error", "message": "No open position"})
    final_pnl = execute_square_off(last_ltp, reason="Manual")
    return jsonify({"status": "success", "final_pnl": final_pnl})

# --- HTML TEMPLATES ---
LOGIN_HTML = f"""
<!DOCTYPE html><html><head><title>Login</title>
<style>body{{background:#131722;color:white;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}}.box{{background:#1e222d;padding:40px;border-radius:10px;width:350px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,0.5);}}h2{{margin-top:0;color:#2962ff;}}input{{width:100%;padding:10px;margin:10px 0;background:#131722;border:1px solid #2a2e39;color:white;border-radius:5px;box-sizing:border-box;}}button{{width:100%;padding:10px;background:#2962ff;color:white;border:none;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:10px;}}.error{{color:#ef5350;margin-bottom:10px;}}</style></head>
<body><div class="box"><h2>Login</h2>{{% if error %}}<div class="error">{{{{ error }}}}</div>{{% endif %}}
<form method="POST"><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Login</button></form>
<div style="margin-top:15px; font-size:12px;"><a href="/register" style="color:#2962ff;">Create Account</a></div>
{OWNER_HTML}
</div></body></html>
"""

REGISTER_HTML = f"""
<!DOCTYPE html><html><head><title>Register</title>
<style>body{{background:#131722;color:white;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}}.box{{background:#1e222d;padding:40px;border-radius:10px;width:350px;text-align:center;}}h2{{margin-top:0;color:#089981;}}input{{width:100%;padding:10px;margin:10px 0;background:#131722;border:1px solid #2a2e39;color:white;border-radius:5px;box-sizing:border-box;}}button{{width:100%;padding:10px;background:#089981;color:white;border:none;border-radius:5px;cursor:pointer;font-weight:bold;}}.error{{color:#ef5350;margin-bottom:10px;}}</style></head>
<body><div class="box"><h2>Register</h2>{{% if error %}}<div class="error">{{{{ error }}}}</div>{{% endif %}}
<form method="POST"><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Register</button></form>
<div style="margin-top:15px; font-size:12px;"><a href="/login" style="color:#2962ff;">Login</a></div>
{OWNER_HTML}
</div></body></html>
"""

ADMIN_HTML = """
<!DOCTYPE html><html><head><title>Admin Panel</title>
<style>body{background:#131722;color:white;font-family:sans-serif;padding:20px;}h2{color:#2962ff;}table{width:100%;border-collapse:collapse;margin-top:20px;}th,td{padding:10px;border-bottom:1px solid #2a2e39;text-align:left;}th{color:#787b86;}.btn{padding:5px 10px;border-radius:4px;text-decoration:none;font-size:12px;color:white;}.btn-red{background:#ef5350;}.btn-green{background:#089981;}</style></head>
<body><h2>Admin Panel</h2><a href="/terminal" style="color:#2962ff;">&larr; Terminal</a> | <a href="/logout" style="color:#ef5350;">Logout</a>
<table><tr><th>ID</th><th>Username</th><th>Status</th><th>Action</th></tr>
{% for user in users %}<tr><td>{{ user.id }}</td><td>{{ user.username }}</td>
<td style="color:{% if user.is_active %}#089981{% else %}#ef5350{% endif %};">{% if user.is_active %}Active{% else %}Blocked{% endif %}</td>
<td>{% if user.is_active %}<a href="/admin/toggle/{{ user.id }}" class="btn btn-red">Block</a>{% else %}<a href="/admin/toggle/{{ user.id }}" class="btn btn-green">Activate</a>{% endif %}</td></tr>{% endfor %}
</table></body></html>
"""

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Sunny's Terminal</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    /* [Aapka same CSS, jo aapne diya hai] */
    :root { --bg-main: #ffffff; --bg-panel: #f8f9fd; --bg-hover: #f0f3fa; --bg-active: #e1ecff; --text-main: #131722; --text-muted: #787b86; --border-color: #e0e3eb; --grid-color: #f2f4f9; --overlay-bg: rgba(255, 255, 255, 0.90); --card-shadow: rgba(0, 0, 0, 0.10); }
    body.dark-mode { --bg-main: #131722; --bg-panel: #1e222d; --bg-hover: #2a2e39; --bg-active: #1e3a8a; --text-main: #d1d4dc; --text-muted: #868993; --border-color: #2a2e39; --grid-color: #1f232e; --overlay-bg: rgba(19, 23, 34, 0.90); --card-shadow: rgba(0, 0, 0, 0.50); }
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-font-smoothing: antialiased; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, Ubuntu, sans-serif; background-color: var(--bg-main); color: var(--text-main); height: 100vh; width: 100vw; overflow: hidden; display: flex; flex-direction: column; font-size: 13px; transition: background-color 0.15s, color 0.15s; }
    .icon-btn { display: inline-flex; align-items: center; justify-content: center; height: 30px; min-width: 30px; padding: 0 5px; border-radius: 4px; color: var(--text-main); cursor: pointer; background: transparent; border: none; font-family: inherit; }
    .icon-btn:hover { background-color: var(--bg-hover); }
    .icon-btn.active { background-color: var(--bg-active); color: #2962ff; }
    .icon-btn svg { width: 18px; height: 18px; stroke: currentColor; fill: none; stroke-width: 1.5; }
    .v-divider { width: 1px; height: 22px; background-color: var(--border-color); margin: 0 4px; flex-shrink: 0; }
    .top-bar { height: 40px; border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 0 0 6px; background: var(--bg-main); flex-shrink: 0; z-index: 30; }
    .top-left, .top-right { display: flex; align-items: center; gap: 2px; }
    .dhan-logo-wrap { display: flex; align-items: center; gap: 3px; padding: 2px 4px; cursor: pointer; }
    .dhan-circle { width: 22px; height: 22px; border-radius: 50%; background-color: #00b07c; color: #fff; font-weight: 800; font-size: 12px; display: flex; align-items: center; justify-content: center; letter-spacing: -0.5px; }
    .chevron-down { width: 12px !important; height: 12px !important; stroke-width: 1.8 !important; }
    .symbol-search { display: flex; align-items: center; gap: 5px; padding: 4px 8px; font-weight: 700; font-size: 13.5px; cursor: pointer; border-radius: 4px; }
    .symbol-search:hover { background-color: var(--bg-hover); }
    .symbol-search select { border: none; background: transparent; font-weight: 700; font-size: 13.5px; outline: none; cursor: pointer; color: var(--text-main); }
    .symbol-search select option { background: var(--bg-main); color: var(--text-main); }
    .tf-group { display: flex; align-items: center; gap: 1px; }
    .tf-item { padding: 5px 7px; border-radius: 4px; font-size: 13px; color: var(--text-main); cursor: pointer; }
    .tf-item:hover { background-color: var(--bg-hover); }
    .tf-item.active { background-color: var(--bg-hover); font-weight: 600; color: #2962ff; }
    .btn-sell { background-color
