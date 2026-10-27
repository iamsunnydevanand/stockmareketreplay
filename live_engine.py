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

# --- SMARTAPI DETAILS ---
api_key = "BAq0de8E"                 
client_id = "S1263568"               
password = "7313"                    
totp_secret = "LY4QG5NPAOBDYIZXODIDWLQKN4"  

obj = SmartConnect(api_key=api_key)
session_initialized = False

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

# --- SIMULATION & TRADING STATE ---
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

smooth_price = 0.0
current_high = 0.0
current_low = 0.0

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
    if 'user_id' not in session: return redirect(url_for('login'))
    if session.get('is_admin'): return redirect(url_for('admin_panel'))
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
    
    if user.is_admin:
        return render_template_string(HTML_PAGE, trial=999999, username=session['username'])
        
    trial_remaining = max(0, 120 - int(time.time() - session.get('login_time', time.time())))
    return render_template_string(HTML_PAGE, trial=trial_remaining, username=session['username'])

# --- API ROUTES ---
@app.route('/api/change_symbol/<symbol>')
def change_symbol(symbol):
    global current_symbol, real_data, current_candle_index, candle_progress, needs_history_reload, is_playing, smooth_price, current_high, current_low
    if symbol in INDICES:
        if position is not None and last_ltp > 0: execute_square_off(last_ltp, reason="Symbol Switch")
        current_symbol = symbol
        real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
        is_playing = True; smooth_price = 0.0; current_high = 0.0; current_low = 0.0
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})

@app.route('/api/set_interval/<interval>')
def set_interval(interval):
    global current_interval, real_data, current_candle_index, candle_progress, needs_history_reload, is_playing, smooth_price, current_high, current_low
    current_interval = interval
    real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
    is_playing = True; smooth_price = 0.0; current_high = 0.0; current_low = 0.0
    return jsonify({"status": "success"})

@app.route('/api/set_speed/<int:speed>')
def set_speed(speed):
    global playback_speed, last_real_time
    playback_speed = speed; last_real_time = time.time()
    return jsonify({"status": "success", "speed": playback_speed})

@app.route('/api/set_date')
def set_date():
    global selected_date, real_data, current_candle_index, candle_progress, needs_history_reload, last_real_time, is_playing, smooth_price, current_high, current_low
    selected_date = request.args.get('date')
    
    # Data Reset
    real_data = []; current_candle_index = 0; candle_progress = 0.0; needs_history_reload = True
    last_real_time = time.time()
    
    # 🔥 PLAY BUG FIX 🔥
    is_playing = True 
    smooth_price = 0.0
    current_high = 0.0
    current_low = 0.0
    
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
        # 300 Seconds Divisor
        candle_progress += ((now - last_real_time) / 300.0) * playback_speed
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

    # Smooth Tick Logic
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
    
    # Tight Micro-Noise
    micro_noise = random.uniform(-0.05, 0.05) * max(1.0, (high_p - low_p) * 0.01)
    
    smooth_price = smooth_price + (guide_p - smooth_price) * 0.15 + micro_noise
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
    .btn-sell { background-color: #ef5350; color: #ffffff; border: none; border-radius: 4px; padding: 4px 11px; font-size: 12.5px; font-weight: 600; cursor: pointer; margin: 0 3px; }
    .btn-sell:hover { background-color: #d32f2f; }
    .btn-buy { background-color: #2962ff; color: #ffffff; border: none; border-radius: 4px; padding: 4px 11px; font-size: 12.5px; font-weight: 600; cursor: pointer; margin-right: 3px; }
    .btn-buy:hover { background-color: #1e4bd8; }
    .btn-scalper { display: flex; align-items: center; gap: 5px; background-color: var(--bg-hover); color: #2962ff; border: 1px solid #2962ff; border-radius: 4px; padding: 3px 10px; font-size: 12.5px; font-weight: 600; cursor: pointer; margin-left: 3px; }
    .btn-scalper.active { background-color: #2962ff; color: #ffffff; }
    .top-input { border: 1px solid var(--border-color); background: var(--bg-panel); color: var(--text-main); border-radius: 4px; padding: 3px 6px; margin-right: 3px; font-family: inherit; font-size: 12px; outline: none; cursor: pointer; }
    .ctrl-btn { display: inline-flex; align-items: center; justify-content: center; gap: 3px; background: var(--bg-panel); color: var(--text-main); border: 1px solid var(--border-color); border-radius: 4px; padding: 3px 8px; font-size: 12px; font-weight: 600; cursor: pointer; margin-right: 3px; height: 26px; }
    .ctrl-btn:hover { background: var(--bg-hover); border-color: #2962ff; color: #2962ff; }
    .ctrl-btn.active { background: #2962ff; color: #ffffff; border-color: #2962ff; }
    .tc-badge { width: 22px; height: 22px; border-radius: 50%; background: #131722; border: 1px solid var(--border-color); color: #fff; font-size: 9px; font-weight: 800; display: flex; align-items: center; justify-content: center; position: relative; margin: 0 3px; cursor: pointer; }
    .tc-badge::after { content: ""; width: 5px; height: 5px; background: #ff6d00; border-radius: 50%; position: absolute; right: 2px; bottom: 4px; }
    .right-collapse-tab { width: 18px; height: 40px; background-color: #878993; color: #ffffff; display: flex; align-items: center; justify-content: center; cursor: pointer; margin-left: 2px; }
    .workspace { flex: 1; display: flex; overflow: hidden; position: relative; }
    .left-toolbar { width: 50px; border-right: 1px solid var(--border-color); display: flex; flex-direction: column; align-items: center; padding-top: 6px; background: var(--bg-main); flex-shrink: 0; position: relative; justify-content: space-between; z-index: 10; }
    .tool-list { display: flex; flex-direction: column; align-items: center; gap: 4px; width: 100%; }
    .tool-btn { width: 34px; height: 34px; border-radius: 4px; display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--text-main); }
    .tool-btn:hover { background-color: var(--bg-hover); }
    .tool-btn.active { background-color: var(--bg-active); color: #2962ff; }
    .tool-btn svg { width: 20px; height: 20px; stroke: currentColor; fill: none; stroke-width: 1.4; }
    .h-divider { width: 26px; height: 1px; background-color: var(--border-color); margin: 3px 0; }
    .bottom-star-box { width: 34px; height: 24px; background-color: #363a45; border-top-left-radius: 4px; border-top-right-radius: 4px; display: flex; align-items: center; justify-content: center; cursor: pointer; }
    .center-column { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .chart-and-axes { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
    .chart-row { flex: 1; display: flex; overflow: hidden; position: relative; gap: 0; }
    .chart-canvas { flex: 1; position: relative; background-color: var(--bg-main); overflow: hidden; height: 100%; }
    #chart-container { width: 100%; height: 100%; position: absolute; top: 0; left: 0; z-index: 1; }
    .ohlc-overlay { position: absolute; top: 10px; left: 12px; z-index: 5; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
    .ohlc-row { display: flex; align-items: center; gap: 8px; font-size: 13px; background: var(--overlay-bg); padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border-color); pointer-events: auto; }
    .sensex-mini-badge { width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--border-color); display: flex; align-items: center; justify-content: center; font-size: 6px; font-weight: 800; color: #2962ff; background: var(--bg-panel); }
    .instrument-title { font-weight: 600; font-size: 15px; color: var(--text-main); }
    .market-status-pill { width: 18px; height: 18px; border-radius: 50%; background: var(--bg-hover); display: flex; align-items: center; justify-content: center; }
    .market-status-pill span { width: 8px; height: 3px; background: #089981; border-radius: 2px; }
    .ohlc-values { font-size: 12.5px; color: var(--text-main); display: flex; gap: 6px; font-family: monospace; }
    .ohlc-values .val { color: #089981; font-weight: 600; }
    .quick-trade-row { display: flex; align-items: center; gap: 8px; pointer-events: auto; }
    .qt-box { border-radius: 4px; padding: 4px 16px; display: flex; flex-direction: column; align-items: center; justify-content: center; background: var(--bg-main); cursor: pointer; line-height: 1.25; box-shadow: 0 2px 5px var(--card-shadow); transition: transform 0.1s; }
    .qt-box:active { transform: scale(0.96); }
    .qt-box.sell { border: 1px solid #f23645; color: #f23645; }
    .qt-box.buy { border: 1px solid #2962ff; color: #2962ff; }
    .qt-price { font-size: 12.5px; font-weight: 700; }
    .qt-label { font-size: 10.5px; font-weight: 500; }
    .qt-spread { font-size: 11px; color: var(--text-muted); background: var(--overlay-bg); padding: 1px 4px; border-radius: 3px; }
    .scalper-floating { position: absolute; top: 12px; right: 82px; z-index: 20; background: var(--bg-main); border: 1.5px solid #2962ff; border-radius: 6px; padding: 12px; box-shadow: 0 6px 20px var(--card-shadow); display: none; width: 290px; }
    .scalper-header { display: flex; justify-content: space-between; align-items: center; font-weight: 700; color: #2962ff; margin-bottom: 8px; font-size: 13px; }
    .scalper-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
    .scalper-field label { display: block; font-size: 10.5px; color: var(--text-muted); margin-bottom: 3px; font-weight: 600; }
    .scalper-field input { width: 100%; padding: 5px 6px; border: 1px solid var(--border-color); background: var(--bg-panel); color: var(--text-main); border-radius: 4px; font-size: 12.5px; font-weight: 700; outline: none; }
    .scalper-field input:focus { border-color: #2962ff; }
    .bottom-footer { height: 38px; border-top: 1px solid var(--border-color); background: var(--bg-main); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; flex-shrink: 0; z-index: 10; }
    .range-list { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-main); }
    .range-list span.range-btn { cursor: pointer; padding: 3px 6px; border-radius: 3px; font-weight: 500; }
    .range-list span.range-btn:hover { background: var(--bg-hover); color: #2962ff; }
    .footer-right { display: flex; align-items: center; gap: 12px; font-size: 12.5px; color: var(--text-main); }
    .dhan-clock-box { font-family: -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, monospace; font-weight: 600; color: var(--text-main); background: var(--bg-panel); padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border-color); }
    .scale-modes { display: flex; align-items: center; gap: 8px; }
    .scale-modes span { cursor: pointer; padding: 2px 5px; border-radius: 3px; }
    .scale-modes span.active { color: #2962ff; font-weight: 700; background: var(--bg-hover); }
    .trade-drawer { width: 270px; border-left: 1px solid var(--border-color); background: var(--bg-main); display: none; flex-direction: column; z-index: 10; flex-shrink: 0; }
    .drawer-header { padding: 10px 14px; border-bottom: 1px solid var(--border-color); font-weight: 700; font-size: 14px; display: flex; justify-content: space-between; align-items: center; }
    .drawer-body { padding: 12px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 14px; }
    .stat-card { background: var(--bg-panel); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; }
    .stat-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 12.5px; }
    .stat-row input { width: 90px; padding: 3px 5px; border: 1px solid var(--border-color); background: var(--bg-main); color: var(--text-main); border-radius: 3px; font-size: 12px; }
    .pnl-banner { font-size: 20px; font-weight: 800; text-align: center; margin: 10px 0; padding: 10px; border-radius: 6px; background: var(--bg-hover); color: var(--text-main); }
    .history-item { border-bottom: 1px solid var(--border-color); padding: 6px 0; font-size: 11.5px; display: flex; justify-content: space-between; align-items: center; }
    .right-sidebar { width: 44px; border-left: 1px solid var(--border-color); background: var(--bg-main); display: flex; flex-direction: column; align-items: center; padding-top: 8px; gap: 14px; flex-shrink: 0; z-index: 12; }
    
    #trialOverlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.95); z-index: 9999; display: none; justify-content: center; align-items: center; flex-direction: column; color: white; }
    .trial-box { background: #1e222d; padding: 30px; border-radius: 10px; border: 1px solid #2962ff; text-align: center; max-width: 400px; }
    .trial-box h2 { color: #2962ff; margin-top: 0; }
    .trial-box a { color: #089981; text-decoration: none; font-weight: bold; }
    .trial-timer { font-weight: bold; color: #ef5350; margin-left: 10px; font-size: 14px; }
  </style>
</head>
<body>
  <header class="top-bar">
    <div class="top-left">
      <div class="dhan-logo-wrap">
        <div class="dhan-circle">dh</div>
        <svg class="chevron-down" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
      <div class="symbol-search">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"></circle><line x1="20" y1="20" x2="16" y2="16"></line></svg>
        <select id="symbolSelector" onchange="changeSymbol()">
          <option value="SENSEX">SENSEX (BSE)</option>
          <option value="NIFTY">NIFTY 50 (NSE)</option>
          <option value="BANKNIFTY">BANKNIFTY (NSE)</option>
          <option value="FINNIFTY">FINNIFTY (NSE)</option>
          <option value="MIDCPNIFTY">MIDCPNIFTY (NSE)</option>
          <option value="BANKEX">BANKEX (BSE)</option>
          <option value="NIFTYIT">NIFTY IT (NSE)</option>
          <option value="NIFTYAUTO">NIFTY AUTO (NSE)</option>
          <option value="NIFTYPHARMA">NIFTY PHARMA (NSE)</option>
        </select>
      </div>
      <button class="icon-btn" title="Fit Chart" onclick="chart.timeScale().fitContent()"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg></button>
      <div class="v-divider"></div>
      <div class="tf-group">
        <span class="tf-item" onclick="setTimeframe('ONE_MINUTE', this)">1m</span>
        <span class="tf-item active" onclick="setTimeframe('FIVE_MINUTE', this)">5m</span>
        <span class="tf-item" onclick="setTimeframe('FIFTEEN_MINUTE', this)">15m</span>
        <span class="tf-item" onclick="setTimeframe('ONE_HOUR', this)">1h</span>
      </div>
      <div class="v-divider"></div>
      <button class="icon-btn" id="emaBtn" onclick="toggleEMA()" title="Toggle 20 EMA Indicator" style="font-style:italic; font-family:serif; font-size:17px; font-weight:600;">f<sub style="font-size:11px; font-style:normal;">x</sub></button>
      <button class="icon-btn" id="splitChartBtn" onclick="toggleSplitScreen()" title="Split-Screen Dual Chart"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="8" height="16" rx="1"/><rect x="13" y="4" width="8" height="16" rx="1"/></svg></button>
      <button class="icon-btn" onclick="toggleOptionChain()" title="Option Chain Simulator"><svg viewBox="0 0 24 24"><rect x="5" y="10" width="3" height="8"/><rect x="10.5" y="6" width="3" height="12"/><rect x="16" y="12" width="3" height="6"/></svg></button>
      <div class="v-divider"></div>
      <button class="btn-sell" onclick="takeTrade('SHORT')" title="Shortcut: S">Sell</button>
      <button class="btn-buy" onclick="takeTrade('LONG')" title="Shortcut: B">Buy</button>
      <button class="btn-scalper" id="scalperBtn" onclick="toggleScalper()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg>
        <span>Scalper</span>
      </button>
    </div>
    <div class="top-right">
      <span class="trial-timer" id="trialTimerDisplay"></span>
      <button class="ctrl-btn" id="barCutterBtn" onclick="toggleBarCutter()" title="Click any candle to replay from there">✂️ <span>Cut</span></button>
      <!-- DATE PICKER YAHAN HAI -->
      <input type="date" id="datePicker" class="top-input" value="2026-10-26" title="Select Replay Date" style="width: 130px;">
      <button class="ctrl-btn" id="playPauseBtn" onclick="togglePlay()" title="Play/Pause (Space)">⏸ Pause</button>
      <button class="ctrl-btn" onclick="stepForward()" title="Next Candle (&gt;|)">&gt;|</button>
      <button class="ctrl-btn" onclick="resetSimulation()" title="Reset (R)">🔄 Reset</button>
      <select id="speedControl" class="top-input" onchange="changeSpeed()">
        <option value="1">1x Speed</option>
        <option value="2">2x Speed</option>
        <option value="5">5x Speed</option>
        <option value="10">10x Speed</option>
        <option value="25">25x Speed</option>
        <option value="50">50x Speed</option>
      </select>
      <button class="icon-btn" id="themeToggleBtn" onclick="toggleTheme()" title="Day/Night Mode" style="font-size:15px;">🌙</button>
      <button class="icon-btn" onclick="toggleDrawer()" title="Trading Panel"><svg viewBox="0 0 24 24"><rect x="5" y="8" width="14" height="12" rx="2"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg></button>
      <div class="tc-badge" title="Connected">TC</div>
      <div class="right-collapse-tab" onclick="toggleDrawer()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5"><path d="M9 6l6 6-6 6"/></svg></div>
      <a href="/logout" style="color:#2962ff; text-decoration:none; font-weight:bold; margin-left:10px;">Logout ({{ username }})</a>
    </div>
  </header>

  <div class="workspace">
    <aside class="left-toolbar">
      <div class="tool-list">
        <div class="tool-btn active" id="cursorTool" onclick="setDrawingMode(false)" title="Crosshair"><svg viewBox="0 0 24 24"><line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/></svg></div>
        <div class="tool-btn" id="hlineTool" onclick="setDrawingMode(true)" title="Draw Support/Resistance"><svg viewBox="0 0 24 24"><line x1="3" y1="12" x2="21" y2="12"/><circle cx="12" cy="12" r="2.2" fill="currentColor"/></svg></div>
        <div class="tool-btn" id="leftCutTool" onclick="toggleBarCutter()" title="Bar Cutter"><svg viewBox="0 0 24 24"><polygon points="11 17 6 12 11 7 11 17"/><polygon points="18 17 13 12 18 7 18 17"/></svg></div>
        <div class="tool-btn" onclick="toggleOptionChain()" title="Option Chain"><svg viewBox="0 0 24 24"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="11" x2="20" y2="11"/><line x1="4" y1="16" x2="20" y2="16"/><circle cx="7" cy="6" r="1.5"/><circle cx="16" cy="16" r="1.5"/></svg></div>
        <div class="tool-btn" onclick="toggleScalper()" title="Scalper"><svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1"/><line x1="4" y1="12" x2="20" y2="12"/><path d="M8 9l3 3 5-5"/></svg></div>
        <div class="h-divider"></div>
        <div class="tool-btn" onclick="chart.timeScale().fitContent()" title="Zoom Reset"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><line x1="20" y1="20" x2="15.5" y2="15.5"/><path d="M11 8v6M8 11h6"/></svg></div>
        <div class="h-divider"></div>
        <div class="tool-btn" onclick="clearDrawings()" title="Clear Lines"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></div>
      </div>
      <div class="bottom-star-box"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="1.8"><polygon points="12 2 15 9 22 9 16.5 14 18.5 21 12 17 5.5 21 7.5 14 2 9 9 9"/></svg></div>
    </aside>

    <div class="center-column">
      <div class="chart-and-axes">
        <div class="chart-row">
          <div class="chart-canvas" id="primaryCanvas">
            <div id="chart-container"></div>
            <div class="ohlc-overlay">
              <div class="ohlc-row">
                <div class="sensex-mini-badge" id="miniBadge">IDX</div>
                <span class="instrument-title" id="chSymbol">SENSEX · 5 · BSE</span>
                <div class="market-status-pill"><span id="statusDot"></span></div>
                <div class="ohlc-values" id="chOhlc">
                  <span>O<span class="val" id="chO">0.00</span></span>
                  <span>H<span class="val" id="chH">0.00</span></span>
                  <span>L<span class="val" id="chL">0.00</span></span>
                  <span>C<span class="val" id="chC">0.00</span></span>
                  <span class="val" id="chChange">0.00 (0.00%)</span>
                </div>
              </div>
              <div class="quick-trade-row">
                <div class="qt-box sell" onclick="takeTrade('SHORT')"><span class="qt-price" id="floatSellPrice">0.00</span><span class="qt-label">SELL</span></div>
                <span class="qt-spread">0.00</span>
                <div class="qt-box buy" onclick="takeTrade('LONG')"><span class="qt-price" id="floatBuyPrice">0.00</span><span class="qt-label">BUY</span></div>
              </div>
            </div>

            <div class="scalper-floating" id="scalperPanel">
              <div class="scalper-header">
                <span>⚡ Scalper Order</span>
                <span style="cursor:pointer; color:var(--text-muted);" onclick="toggleScalper()">✕</span>
              </div>
              <div class="scalper-grid">
                <div class="scalper-field"><label>Lots</label><input type="number" id="inpLots" value="1" min="1" max="500"></div>
                <div class="scalper-field"><label>SL Pts</label><input type="number" id="inpSlPts" value="20" min="0"></div>
                <div class="scalper-field"><label>Target Pts</label><input type="number" id="inpTpPts" value="40" min="0"></div>
                <div class="scalper-field"><label>TSL Pts</label><input type="number" id="inpTslPts" value="0" min="0"></div>
              </div>
              <div style="display:flex; gap:6px;">
                <button class="btn-sell" style="flex:1; margin:0; padding:7px;" onclick="takeTrade('SHORT')">SELL</button>
                <button class="btn-buy" style="flex:1; margin:0; padding:7px;" onclick="takeTrade('LONG')">BUY</button>
              </div>
            </div>

            <div class="toast-msg" id="toastMsg"></div>
          </div>
        </div>
      </div>

      <footer class="bottom-footer">
        <div class="range-list">
          <span style="font-weight:600; color:var(--text-muted);">Realized PnL: <strong id="footerRealizedPnl" style="color:var(--text-main);">₹ 0.00</strong></span>
        </div>
        <div class="footer-right">
          <span id="syncedDhanClock" class="dhan-clock-box">26 Oct 2026  09:15:00 UTC+5:30</span>
          <div class="v-divider"></div>
          <div class="scale-modes"><span>%</span><span>log</span><span class="active">auto</span></div>
        </div>
      </footer>
    </div>

    <aside class="trade-drawer" id="tradePanel">
      <div class="drawer-header"><span>Paper Trading</span><span style="cursor:pointer; color:var(--text-muted);" onclick="toggleDrawer()">✕</span></div>
      <div class="drawer-body">
        <div id="noPosition" class="stat-card" style="text-align:center; color:var(--text-muted);">No open positions.<br>Click <b>Buy</b> or <b>Sell</b>.</div>
        <div id="openPosition" class="stat-card" style="display:none;">
          <div class="stat-row"><span>Instrument:</span> <strong id="posSymbol">-</strong></div>
          <div class="stat-row"><span>Type:</span> <strong id="posType">-</strong></div>
          <div class="stat-row"><span>Quantity:</span> <strong id="posQty">-</strong></div>
          <div class="stat-row"><span>Entry:</span> <strong id="posEntry">-</strong></div>
          <div class="stat-row"><span>SL Price:</span> <strong id="posSl" style="color:#f23645;">-</strong></div>
          <div class="stat-row"><span>Target Price:</span> <strong id="posTp" style="color:#089981;">-</strong></div>
          <div class="stat-row"><span>LTP:</span> <strong id="posLtp">-</strong></div>
          <div id="pnlDisplay" class="pnl-banner">₹ 0.00</div>
          <button onclick="closeTrade()" style="width:100%; padding:10px; background:#f59e0b; color:white; border:none; border-radius:4px; font-weight:700; cursor:pointer;">SQUARE OFF</button>
        </div>
        <div>
          <div style="font-weight:700; margin-bottom:8px; font-size:12px; color:var(--text-muted); text-transform:uppercase;">Closed Trades</div>
          <div id="historyList" style="display:flex; flex-direction:column; gap:4px;"><div style="color:var(--text-muted); font-size:12px;">No closed trades.</div></div>
        </div>
      </div>
    </aside>

    <aside class="right-sidebar">
      <button class="icon-btn active" onclick="toggleDrawer()" title="Positions"><svg viewBox="0 0 24 24"><path d="M6 4h12v16l-6-4-6 4V4z"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="11" x2="15" y2="11"/></svg></button>
      <button class="icon-btn" onclick="toggleScalper()" title="Scalper"><svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h4M10 7v4M8 14h8M8 17h6"/></svg></button>
    </aside>
  </div>

  <div id="trialOverlay">
    <div class="trial-box">
        <h2>⏳ Trial Expired</h2>
        <p>Aapka 2 minute ka free trial khatam ho gaya hai.</p>
        <p>Full access ke liye contact karein:</p>
        <hr style="border-color:#2a2e39; margin:20px 0;">
        <h3 style="color:#089981; margin:0;">Sunny Dev Anand</h3>
        <p>Software Engineer</p>
        <p>📧 <a href="mailto:itsmywayokok@gmail.com">itsmywayokok@gmail.com</a></p>
        <p>📱 <a href="https://wa.me/917667765215">+91 7667765215 (WhatsApp)</a></p>
    </div>
  </div>

  <script>
    let trialSeconds = {{ trial }};
    function updateTrialTimer() {
        if (trialSeconds <= 0) { document.getElementById('trialOverlay').style.display = 'flex'; return; }
        let m = Math.floor(trialSeconds / 60), s = trialSeconds % 60;
        document.getElementById('trialTimerDisplay').innerText = `⏳ ${m}:${s < 10 ? '0' : ''}${s}`;
        trialSeconds--;
    }
    if (trialSeconds <= 0) document.getElementById('trialOverlay').style.display = 'flex';
    else { updateTrialTimer(); setInterval(updateTrialTimer, 1000); }

    let isDarkMode = false;
    const chartContainer = document.getElementById('chart-container');
    const chart = LightweightCharts.createChart(chartContainer, {
      width: chartContainer.clientWidth, height: chartContainer.clientHeight,
      layout: { background: { color: '#ffffff' }, textColor: '#131722', fontSize: 11.5 },
      grid: { vertLines: { color: '#f2f4f9', style: 0 }, horzLines: { color: '#f2f4f9', style: 0 } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#e0e3eb', rightOffset: 8, barSpacing: 8 },
      rightPriceScale: { borderColor: '#e0e3eb', autoScale: true, scaleMargins: { top: 0.12, bottom: 0.12 } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal }
    });

    const candleSeries = chart.addCandlestickSeries({ upColor: '#089981', downColor: '#f23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#f23645' });

    let allCandles = [];
    let entryLine = null, slLine = null, tpLine = null, userLines = [];
    let drawingHLineMode = false, lastCrosshairPrice = null;
    let currentUnderlyingPrice = 0;

    function toggleTheme() {
      isDarkMode = !isDarkMode;
      document.body.classList.toggle('dark-mode', isDarkMode);
      document.getElementById('themeToggleBtn').innerText = isDarkMode ? '☀️' : '🌙';
      chart.applyOptions({
        layout: { background: { color: isDarkMode ? '#131722' : '#ffffff' }, textColor: isDarkMode ? '#d1d4dc' : '#131722' },
        grid: { vertLines: { color: isDarkMode ? '#1f232e' : '#f2f4f9' }, horzLines: { color: isDarkMode ? '#1f232e' : '#f2f4f9' } },
        timeScale: { borderColor: isDarkMode ? '#2a2e39' : '#e0e3eb' },
        rightPriceScale: { borderColor: isDarkMode ? '#2a2e39' : '#e0e3eb' }
      });
    }

    chart.subscribeCrosshairMove(param => {
      if (param.point) lastCrosshairPrice = candleSeries.coordinateToPrice(param.point.y);
    });

    chart.subscribeClick(async (param) => {
      if (drawingHLineMode && lastCrosshairPrice !== null) {
        const line = candleSeries.createPriceLine({ price: lastCrosshairPrice, color: '#2962ff', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'S/R' });
        userLines.push(line);
        setDrawingMode(false);
      }
    });

    function setDrawingMode(active) {
      drawingHLineMode = active;
      document.getElementById('hlineTool').classList.toggle('active', active);
      document.getElementById('cursorTool').classList.toggle('active', !active);
    }

    function clearDrawings() {
      userLines.forEach(l => candleSeries.removePriceLine(l));
      userLines = [];
    }

    function removePositionLines() {
      if (entryLine) { candleSeries.removePriceLine(entryLine); entryLine = null; }
      if (slLine) { candleSeries.removePriceLine(slLine); slLine = null; }
      if (tpLine) { candleSeries.removePriceLine(tpLine); tpLine = null; }
    }

    function drawPositionLines(ti) {
      removePositionLines();
      if (!ti || !ti.position) return;
      entryLine = candleSeries.createPriceLine({ price: ti.entry_price, color: '#2962ff', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: `ENTRY` });
      if (ti.sl_price > 0) slLine = candleSeries.createPriceLine({ price: ti.sl_price, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'SL' });
      if (ti.target_price > 0) tpLine = candleSeries.createPriceLine({ price: ti.target_price, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'TARGET' });
    }

    function showToast(msg) {
      const el = document.getElementById('toastMsg');
      el.innerText = msg; el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 3000);
    }

    function toggleScalper() {
      const panel = document.getElementById('scalperPanel');
      panel.style.display = panel.style.display === 'block' ? 'none' : 'block';
      document.getElementById('scalperBtn').classList.toggle('active', panel.style.display === 'block');
    }

    function toggleDrawer() {
      const drawer = document.getElementById('tradePanel');
      drawer.style.display = drawer.style.display === 'flex' ? 'none' : 'flex';
      setTimeout(() => { chart.applyOptions({ width: chartContainer.clientWidth, height: chartContainer.clientHeight }); }, 50);
    }

    async function changeSymbol() {
      allCandles = []; candleSeries.setData([]); removePositionLines();
      await fetch('/api/change_symbol/' + document.getElementById('symbolSelector').value);
    }

    async function setTimeframe(interval, btn) {
      document.querySelectorAll('.tf-item').forEach(el => el.classList.remove('active'));
      btn.classList.add('active');
      allCandles = []; candleSeries.setData([]); removePositionLines();
      await fetch('/api/set_interval/' + interval);
    }

    async function changeSpeed() {
      await fetch('/api/set_speed/' + document.getElementById('speedControl').value);
    }

    // 🔥 DATE PICKER CHANGE EVENT 🔥
    document.getElementById('datePicker').addEventListener('change', async () => {
      let selectedDate = document.getElementById('datePicker').value;
      if(!selectedDate) return;
      
      allCandles = []; 
      candleSeries.setData([]); 
      removePositionLines();
      document.getElementById('tradePanel').style.display = 'none';
      document.getElementById('chSymbol').innerText = "Loading data...";
      document.getElementById('playPauseBtn').innerText = '⏸ Pause';
      
      // Backend ko date bhejo (Yeh is_playing = True karega)
      await fetch('/api/set_date?date=' + selectedDate);
      
      // UI update karo
      document.getElementById('syncedDhanClock').innerText = `${selectedDate} 09:15:00 UTC+5:30`;
      showToast(`Data loading for ${selectedDate}...`);
      
      // 1.5 sec baad verify karein
      setTimeout(async () => {
        try {
            const res = await fetch('/api/get_tick');
            const data = await res.json();
            if (data.status === 'success' && data.current_candle) {
                showToast(`✅ Loaded: ${selectedDate}`);
            } else {
                showToast(`❌ No data for ${selectedDate} (Weekend/Holiday?)`);
            }
        } catch(e) {}
      }, 1500);
    });

    async function togglePlay() {
      const res = await fetch('/api/toggle_play', { method: 'POST' });
      const data = await res.json();
      document.getElementById('playPauseBtn').innerText = data.is_playing ? '⏸ Pause' : '▶ Play';
    }

    async function stepForward() { await fetch('/api/step_candle', { method: 'POST' }); }

    async function resetSimulation() {
      allCandles = []; candleSeries.setData([]); removePositionLines();
      document.getElementById('tradePanel').style.display = 'none';
      await fetch('/api/reset_sim', { method: 'POST' });
      document.getElementById('playPauseBtn').innerText = '⏸ Pause';
      chart.timeScale().fitContent();
    }

    async function takeTrade(type) {
      const lots = parseInt(document.getElementById('inpLots').value) || 1;
      const slPoints = parseFloat(document.getElementById('inpSlPts').value) || 20;
      const targetPoints = parseFloat(document.getElementById('inpTpPts').value) || 40;
      const tslPoints = parseFloat(document.getElementById('inpTslPts').value) || 0;

      const res = await fetch('/api/trade', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: type, lots: lots, sl_points: slPoints, target_points: targetPoints, tsl_points: tslPoints })
      });
      const data = await res.json();
      if (data.status === 'success') {
        const ti = data.trade_info;
        document.getElementById('tradePanel').style.display = 'flex';
        document.getElementById('noPosition').style.display = 'none';
        document.getElementById('openPosition').style.display = 'block';
        document.getElementById('posSymbol').innerText = ti.label;
        document.getElementById('posType').innerText = ti.position;
        document.getElementById('posQty').innerText = ti.qty;
        document.getElementById('posEntry').innerText = ti.entry_price.toFixed(2);
        document.getElementById('posSl').innerText = ti.sl_price.toFixed(2);
        document.getElementById('posTp').innerText = ti.target_price.toFixed(2);
        drawPositionLines(ti);
      } else { alert(data.message); }
    }

    async function closeTrade() {
      const res = await fetch('/api/close_trade', { method: 'POST' });
      const data = await res.json();
      if (data.status === 'success') {
        removePositionLines();
        alert(`Trade Closed! Final PnL: ₹ ${data.final_pnl.toFixed(2)}`);
        document.getElementById('tradePanel').style.display = 'none';
      }
    }

    function renderTradeHistory(list) {
      const container = document.getElementById('historyList');
      if (!list || list.length === 0) { container.innerHTML = '<div style="color:var(--text-muted); font-size:12px;">No closed trades.</div>'; return; }
      container.innerHTML = list.slice(0, 8).map(item => {
        const color = item.pnl >= 0 ? '#089981' : '#f23645';
        return `<div class="history-item"><div><strong>${item.symbol}</strong> <span style="color:${item.type === 'LONG' ? '#2962ff' : '#f23645'}">${item.type}</span> (${item.reason})<br><span style="color:var(--text-muted);">${item.entry} → ${item.exit}</span></div><div style="font-weight:700; color:${color};">${item.pnl >= 0 ? '+' : ''}₹${item.pnl.toFixed(2)}</div></div>`;
      }).join('');
    }

    let isFetchingTick = false;
    async function fetchLiveData() {
      if (isFetchingTick) return;
      isFetchingTick = true;
      try {
        const response = await fetch('/api/get_tick');
        const result = await response.json();
        if (result.status === 'success') {
          if (result.synced_time_str) document.getElementById('syncedDhanClock').innerText = result.synced_time_str;
          
          if (result.history && result.history.length > 0) {
            allCandles = result.history;
            candleSeries.setData(allCandles);
          }
          
          if (result.current_candle) {
            const c = result.current_candle;
            currentUnderlyingPrice = c.close;
            candleSeries.update(c);
            if (allCandles.length > 0 && allCandles[allCandles.length - 1].time === c.time) {
              allCandles[allCandles.length - 1] = c;
            } else { allCandles.push(c); }

            document.getElementById('chO').innerText = c.open.toFixed(2);
            document.getElementById('chH').innerText = c.high.toFixed(2);
            document.getElementById('chL').innerText = c.low.toFixed(2);
            document.getElementById('chC').innerText = c.close.toFixed(2);
            document.getElementById('floatSellPrice').innerText = c.close.toFixed(2);
            document.getElementById('floatBuyPrice').innerText = c.close.toFixed(2);
            document.getElementById('chChange').innerText = result.change_str;
            document.getElementById('chSymbol').innerText = `${result.symbol} · ${result.interval_name} · ${result.exchange}`;
            document.getElementById('miniBadge').innerText = result.symbol.substring(0, 6);
          }
          
          const rPnl = result.realized_pnl || 0;
          document.getElementById('footerRealizedPnl').innerText = `${rPnl >= 0 ? '+' : ''}₹ ${rPnl.toFixed(2)}`;
          renderTradeHistory(result.trade_history);

          if (result.trade_info && result.trade_info.auto_closed) {
            removePositionLines();
            document.getElementById('tradePanel').style.display = 'none';
            alert(`Trade Auto Closed! Hit ${result.trade_info.reason}. Final PnL: ₹ ${result.trade_info.pnl.toFixed(2)}`);
          } else if (result.trade_info && result.trade_info.position) {
            const pnl = result.trade_info.pnl;
            const pnlEl = document.getElementById('pnlDisplay');
            document.getElementById('posLtp').innerText = result.current_candle.close.toFixed(2);
            document.getElementById('posSl').innerText = result.trade_info.sl_price.toFixed(2);
            pnlEl.innerText = (pnl >= 0) ? `+₹ ${pnl.toFixed(2)}` : `-₹ ${Math.abs(pnl).toFixed(2)}`;
            pnlEl.style.color = (pnl >= 0) ? '#089981' : '#f23645';
          }
        }
      } catch (error) { console.error(error); } finally { isFetchingTick = false; }
    }

    window.addEventListener('resize', () => { chart.applyOptions({ width: chartContainer.clientWidth, height: chartContainer.clientHeight }); });
    setInterval(fetchLiveData, 50);
  </script>
</body>
</html>
"""

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 SUNNY'S TERMINAL STARTING...")
    print("="*50)
    print("👇 Click the links below (Ctrl + Click):")
    print("🔗 MAIN PAGE:     http://127.0.0.1:5000/")
    print("🔗 LOGIN PAGE:    http://127.0.0.1:5000/login")
    print("🔗 ADMIN PANEL:   http://127.0.0.1:5000/admin")
    print("="*50)
    print("🔑 Admin Credentials: admin / admin123")
    print("="*50 + "\n")
    app.run(port=5000, debug=True, use_reloader=False)
