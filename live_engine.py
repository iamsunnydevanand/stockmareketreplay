from flask import Flask, jsonify, render_template_string, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from SmartApi import SmartConnect
from pyotp import TOTP
from datetime import datetime, timedelta
import time
import random
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sunnys_super_secret_key_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- SMARTAPI CONFIGURATION ---
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

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active_user = db.Column(db.Boolean, default=True)
    
    # Trading State
    current_symbol = db.Column(db.String(20), default="SENSEX")
    current_interval = db.Column(db.String(20), default="FIVE_MINUTE")
    selected_date = db.Column(db.String(20), default="2026-10-26")
    position = db.Column(db.String(10), nullable=True)
    entry_price = db.Column(db.Float, default=0.0)
    sl_price = db.Column(db.Float, default=0.0)
    target_price = db.Column(db.Float, default=0.0)
    tsl_points = db.Column(db.Float, default=0.0)
    highest_price = db.Column(db.Float, default=0.0)
    lowest_price = db.Column(db.Float, default=0.0)
    qty_lots = db.Column(db.Integer, default=1)
    pnl = db.Column(db.Float, default=0.0)
    realized_pnl = db.Column(db.Float, default=0.0)
    last_ltp = db.Column(db.Float, default=0.0)
    trade_history = db.Column(db.Text, default="[]")

with app.app_context():
    # Force recreate tables to match columns cleanly
    db.drop_all()
    db.create_all()
    
    # Create default admin
    admin_user = User(
        username='admin',
        password=generate_password_hash('admin123'),
        is_admin=True,
        is_active_user=True
    )
    db.session.add(admin_user)
    db.session.commit()

# Global Cache for Candle Data
cached_real_data = {}
candle_indices = {}
candle_progresses = {}
last_real_times = {}
smooth_prices = {}
current_highs = {}
current_lows = {}

def ensure_login():
    global session_initialized
    if not session_initialized:
        try:
            totp = TOTP(totp_secret).now()
            res = obj.generateSession(client_id, password, totp)
            if res and res.get('status'):
                session_initialized = True
            else:
                raise Exception(res.get('message', 'SmartAPI Login Failed'))
        except Exception as e:
            session_initialized = False

# --- HTML TEMPLATES ---
OWNER_INFO_HTML = """
<div style="margin-top:15px; padding:10px; background:#1e222d; border:1px solid #2a2e39; border-radius:6px; font-size:12px; color:#d1d4dc; text-align:center;">
    <strong>👤 Owner: Sunny Dev Anand</strong><br>
    Software Engineer | itmywayokok@gmail.com | +91 7667765215
</div>
"""

LOGIN_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Login - Sunny's Terminal</title>
    <style>
        body {{ background: #131722; color: #d1d4dc; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e222d; padding: 30px; border-radius: 8px; border: 1px solid #2a2e39; width: 320px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
        h2 {{ margin-bottom: 20px; color: #fff; text-align: center; }}
        input {{ width: 100%; padding: 10px; margin-bottom: 12px; background: #131722; border: 1px solid #2a2e39; color: #fff; border-radius: 4px; box-sizing: border-box; }}
        button {{ width: 100%; padding: 10px; background: #2962ff; color: #fff; border: none; border-radius: 4px; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #1e4bd8; }}
        .link {{ text-align: center; margin-top: 15px; font-size: 13px; }}
        .link a {{ color: #2962ff; text-decoration: none; }}
        .error {{ color: #f23645; font-size: 12px; margin-bottom: 10px; text-align: center; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>Sunny's Terminal</h2>
        {{% if error %}}<div class="error">{{{{ error }}}}</div>{{% endif %}}
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <div class="link">Don't have an account? <a href="/register">Register</a></div>
        {OWNER_INFO_HTML}
    </div>
</body>
</html>
"""

REGISTER_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Register - Sunny's Terminal</title>
    <style>
        body {{ background: #131722; color: #d1d4dc; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e222d; padding: 30px; border-radius: 8px; border: 1px solid #2a2e39; width: 320px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
        h2 {{ margin-bottom: 20px; color: #fff; text-align: center; }}
        input {{ width: 100%; padding: 10px; margin-bottom: 12px; background: #131722; border: 1px solid #2a2e39; color: #fff; border-radius: 4px; box-sizing: border-box; }}
        button {{ width: 100%; padding: 10px; background: #089981; color: #fff; border: none; border-radius: 4px; font-weight: bold; cursor: pointer; }}
        button:hover {{ background: #06705d; }}
        .link {{ text-align: center; margin-top: 15px; font-size: 13px; }}
        .link a {{ color: #2962ff; text-decoration: none; }}
        .error {{ color: #f23645; font-size: 12px; margin-bottom: 10px; text-align: center; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>Create Account</h2>
        {{% if error %}}<div class="error">{{{{ error }}}}</div>{{% endif %}}
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Register</button>
        </form>
        <div class="link">Already have an account? <a href="/login">Login</a></div>
        {OWNER_INFO_HTML}
    </div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Admin Panel - Sunny's Terminal</title>
    <style>
        body {{ background: #131722; color: #d1d4dc; font-family: sans-serif; padding: 30px; margin: 0; }}
        h2 {{ color: #fff; }}
        table {{ width: 100%; border-collapse: collapse; background: #1e222d; margin-top: 20px; border: 1px solid #2a2e39; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #2a2e39; font-size: 13px; }}
        th {{ background: #2a2e39; color: #fff; }}
        .btn-action {{ padding: 6px 12px; border-radius: 4px; border: none; font-weight: bold; cursor: pointer; color: #fff; }}
        .btn-block {{ background: #f23645; }}
        .btn-activate {{ background: #089981; }}
        .top-nav {{ display: flex; justify-content: space-between; align-items: center; }}
        a {{ color: #2962ff; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="top-nav">
        <h2>Admin Panel - User Management</h2>
        <a href="/logout">Logout</a>
    </div>
    <table>
        <tr>
            <th>ID</th>
            <th>Username</th>
            <th>Status</th>
            <th>Realized PnL</th>
            <th>Action</th>
        </tr>
        {% for u in users %}
        <tr>
            <td>{{ u.id }}</td>
            <td>{{ u.username }}</td>
            <td>{{ 'Active' if u.is_active_user else 'Blocked/Logged Out' }}</td>
            <td>₹ {{ "%.2f"|format(u.realized_pnl) }}</td>
            <td>
                {% if u.username != 'admin' %}
                <form action="/admin/toggle/{{ u.id }}" method="POST" style="display:inline;">
                    <button type="submit" class="btn-action {{ 'btn-block' if u.is_active_user else 'btn-activate' }}">
                        {{ 'Force Logout / Block' if u.is_active_user else 'Activate' }}
                    </button>
                </form>
                {% else %}
                -
                {% endif %}
            </td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

TERMINAL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sunny's Terminal - Pro Paper Trading</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root {
      --bg-main: #131722; --bg-panel: #1e222d; --bg-hover: #2a2e39; --bg-active: #1e3a8a;
      --text-main: #d1d4dc; --text-muted: #868993; --border-color: #2a2e39; --grid-color: #1f232e;
      --overlay-bg: rgba(19, 23, 34, 0.90); --card-shadow: rgba(0, 0, 0, 0.50);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Trebuchet MS", Roboto, sans-serif; background-color: var(--bg-main); color: var(--text-main); height: 100vh; width: 100vw; overflow: hidden; display: flex; flex-direction: column; font-size: 13px; }
    .top-bar { height: 40px; border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 10px; background: var(--bg-main); flex-shrink: 0; z-index: 30; }
    .top-left, .top-right { display: flex; align-items: center; gap: 6px; }
    .symbol-search select { border: 1px solid var(--border-color); background: var(--bg-panel); color: var(--text-main); font-weight: 700; font-size: 13.5px; outline: none; cursor: pointer; padding: 4px; border-radius: 4px; }
    .tf-group { display: flex; align-items: center; gap: 2px; }
    .tf-item { padding: 4px 8px; border-radius: 4px; font-size: 13px; color: var(--text-main); cursor: pointer; }
    .tf-item:hover, .tf-item.active { background-color: var(--bg-hover); color: #2962ff; font-weight: 600; }
    .btn-sell { background-color: #ef5350; color: #fff; border: none; border-radius: 4px; padding: 5px 12px; font-weight: 600; cursor: pointer; }
    .btn-buy { background-color: #2962ff; color: #fff; border: none; border-radius: 4px; padding: 5px 12px; font-weight: 600; cursor: pointer; }
    .ctrl-btn { background: var(--bg-panel); color: var(--text-main); border: 1px solid var(--border-color); border-radius: 4px; padding: 4px 10px; font-size: 12px; font-weight: 600; cursor: pointer; }
    .ctrl-btn:hover { background: var(--bg-hover); border-color: #2962ff; color: #2962ff; }
    .workspace { flex: 1; display: flex; overflow: hidden; position: relative; }
    .center-column { flex: 1; display: flex; flex-direction: column; position: relative; }
    #chart-container { width: 100%; height: 100%; position: absolute; top: 0; left: 0; }
    .trade-drawer { width: 300px; border-left: 1px solid var(--border-color); background: var(--bg-panel); display: flex; flex-direction: column; z-index: 10; padding: 12px; gap: 12px; }
    .stat-card { background: var(--bg-main); border: 1px solid var(--border-color); border-radius: 6px; padding: 10px; }
    .stat-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; font-size: 12.5px; }
    .pnl-banner { font-size: 20px; font-weight: 800; text-align: center; margin: 10px 0; padding: 10px; border-radius: 6px; background: var(--bg-hover); }
    .owner-float-btn { position: fixed; bottom: 15px; left: 15px; z-index: 1000; background: #2962ff; color: #fff; border: none; padding: 10px 16px; border-radius: 20px; font-weight: bold; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }
    .modal-overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); z-index: 2000; display: none; justify-content: center; align-items: center; }
    .modal-card { background: #1e222d; border: 1px solid #2a2e39; padding: 30px; border-radius: 8px; width: 400px; text-align: center; }
    .modal-card h2 { color: #f23645; margin-bottom: 15px; }
    .modal-card p { color: #d1d4dc; font-size: 14px; line-height: 1.5; margin-bottom: 20px; }
  </style>
</head>
<body>
  <button class="owner-float-btn" onclick="showOwnerInfo()">👤 Owner Info</button>

  <!-- Trial Expired Modal -->
  <div class="modal-overlay" id="expiredModal">
    <div class="modal-card">
      <h2>Trial Expired</h2>
      <p>Your 2-minute free trial has expired.<br>Contact Owner to gain full access.</p>
      <div style="background:#131722; padding:12px; border-radius:6px; font-size:12px; text-align:left; border:1px solid #2a2e39;">
        <strong>Sunny Dev Anand</strong><br>
        Software Engineer<br>
        Email: itmywayokok@gmail.com<br>
        WhatsApp: +91 7667765215
      </div>
      <br>
      <a href="/logout" style="color:#2962ff; font-weight:bold; text-decoration:none;">Logout</a>
    </div>
  </div>

  <header class="top-bar">
    <div class="top-left">
      <strong>Sunny's Terminal</strong>
      <div class="symbol-search">
        <select id="symbolSelector" onchange="changeSymbol()">
          {% for sym in indices %}
          <option value="{{ sym }}" {{ 'selected' if sym == current_symbol else '' }}>{{ sym }} ({{ indices[sym].exchange }})</option>
          {% endfor %}
        </select>
      </div>
      <div class="tf-group">
        <span class="tf-item {{ 'active' if current_interval == 'ONE_MINUTE' else '' }}" onclick="setTimeframe('ONE_MINUTE')">1m</span>
        <span class="tf-item {{ 'active' if current_interval == 'FIVE_MINUTE' else '' }}" onclick="setTimeframe('FIVE_MINUTE')">5m</span>
        <span class="tf-item {{ 'active' if current_interval == 'FIFTEEN_MINUTE' else '' }}" onclick="setTimeframe('FIFTEEN_MINUTE')">15m</span>
      </div>
      <button class="btn-sell" onclick="takeTrade('SHORT')">Sell</button>
      <button class="btn-buy" onclick="takeTrade('LONG')">Buy</button>
    </div>
    <div class="top-right">
      <span id="trialTimer" style="font-weight:bold; color:#f59e0b; background:#1e222d; padding:4px 8px; border-radius:4px; border:1px solid #2a2e39;">Trial: 02:00</span>
      <button class="ctrl-btn" onclick="resetSimulation()">🔄 Reset</button>
      <a href="/logout" class="ctrl-btn" style="text-decoration:none; display:inline-flex; align-items:center;">Logout</a>
    </div>
  </header>

  <div class="workspace">
    <div class="center-column">
      <div id="chart-container"></div>
    </div>
    <aside class="trade-drawer">
      <h3>Paper Trading</h3>
      <div id="noPosition" class="stat-card" style="text-align:center; color:var(--text-muted);">No open positions.<br>Click <b>Buy</b> or <b>Sell</b>.</div>
      <div id="openPosition" class="stat-card" style="display:none;">
        <div class="stat-row"><span>Type:</span> <strong id="posType">-</strong></div>
        <div class="stat-row"><span>Qty:</span> <strong id="posQty">-</strong></div>
        <div class="stat-row"><span>Entry:</span> <strong id="posEntry">-</strong></div>
        <div class="stat-row"><span>SL:</span> <strong id="posSl" style="color:#f23645;">-</strong></div>
        <div class="stat-row"><span>Target:</span> <strong id="posTp" style="color:#089981;">-</strong></div>
        <div class="stat-row"><span>LTP:</span> <strong id="posLtp">-</strong></div>
        <div id="pnlDisplay" class="pnl-banner">₹ 0.00</div>
        <button onclick="closeTrade()" style="width:100%; padding:10px; background:#f59e0b; color:white; border:none; border-radius:4px; font-weight:700; cursor:pointer;">SQUARE OFF</button>
      </div>
      <div class="stat-card">
        <div style="font-weight:bold; margin-bottom:6px;">Realized PnL</div>
        <div id="footerRealizedPnl" style="font-size:16px; font-weight:bold;">₹ 0.00</div>
      </div>
    </aside>
  </div>

  <script>
    function showOwnerInfo() {
        alert("Sunny's Terminal Owner Info:\\n\\nName: Sunny Dev Anand\\nTitle: Software Engineer\\nEmail: itmywayokok@gmail.com\\nWhatsApp: +91 7667765215");
    }

    let trialSeconds = 120;
    const timerInterval = setInterval(() => {
        trialSeconds--;
        if (trialSeconds <= 0) {
            clearInterval(timerInterval);
            document.getElementById('expiredModal').style.display = 'flex';
        } else {
            let m = Math.floor(trialSeconds / 60);
            let s = trialSeconds % 60;
            document.getElementById('trialTimer').innerText = `Trial: ${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
        }
    }, 1000);

    const chartContainer = document.getElementById('chart-container');
    const chart = LightweightCharts.createChart(chartContainer, {
      width: chartContainer.clientWidth, height: chartContainer.clientHeight,
      layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
      grid: { vertLines: { color: '#1f232e' }, horzLines: { color: '#1f232e' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#2a2e39' },
      rightPriceScale: { borderColor: '#2a2e39' }
    });
    const candleSeries = chart.addCandlestickSeries({ upColor: '#089981', downColor: '#f23645', wickUpColor: '#089981', wickDownColor: '#f23645' });

    let allCandles = [];

    async function changeSymbol() {
      const sym = document.getElementById('symbolSelector').value;
      await fetch('/api/change_symbol/' + sym);
      allCandles = [];
      candleSeries.setData([]);
    }

    async function setTimeframe(interval) {
      await fetch('/api/set_interval/' + interval);
      location.reload();
    }

    async function resetSimulation() {
      await fetch('/api/reset_sim', { method: 'POST' });
      allCandles = [];
      candleSeries.setData([]);
    }

    async function takeTrade(action) {
      const res = await fetch('/api/trade', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, lots: 1, sl_points: 20, target_points: 40 })
      });
      const data = await res.json();
      if (data.status !== 'success') alert(data.message);
    }

    async function closeTrade() {
      await fetch('/api/close_trade', { method: 'POST' });
    }

    async function fetchTick() {
      try {
        const res = await fetch('/api/get_tick');
        const data = await res.json();
        if (data.status === 'success') {
          if (data.force_logout) {
              window.location.href = '/logout';
              return;
          }
          if (data.history && data.history.length > 0) {
            allCandles = data.history;
            candleSeries.setData(allCandles);
          }
          if (data.current_candle) {
            candleSeries.update(data.current_candle);
          }
          document.getElementById('footerRealizedPnl').innerText = `₹ ${data.realized_pnl.toFixed(2)}`;

          const ti = data.trade_info;
          if (ti && ti.position) {
            document.getElementById('noPosition').style.display = 'none';
            document.getElementById('openPosition').style.display = 'block';
            document.getElementById('posType').innerText = ti.position;
            document.getElementById('posQty').innerText = ti.qty;
            document.getElementById('posEntry').innerText = ti.entry_price.toFixed(2);
            document.getElementById('posSl').innerText = ti.sl_price.toFixed(2);
            document.getElementById('posTp').innerText = ti.target_price.toFixed(2);
            document.getElementById('posLtp').innerText = data.current_candle.close.toFixed(2);
            
            const pnlEl = document.getElementById('pnlDisplay');
            pnlEl.innerText = (ti.pnl >= 0 ? '+' : '') + `₹ ${ti.pnl.toFixed(2)}`;
            pnlEl.style.color = ti.pnl >= 0 ? '#089981' : '#f23645';
          } else {
            document.getElementById('noPosition').style.display = 'block';
            document.getElementById('openPosition').style.display = 'none';
          }
        }
      } catch (e) { console.error(e); }
    }

    window.addEventListener('resize', () => { chart.applyOptions({ width: chartContainer.clientWidth, height: chartContainer.clientHeight }); });
    setInterval(fetchTick, 1000);
  </script>
</body>
</html>
"""

# --- ROUTES ---
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if not user or not user.is_active_user:
        session.clear()
        return redirect(url_for('login'))
    if user.is_admin:
        return redirect(url_for('admin_panel'))
    return render_template_string(TERMINAL_HTML, indices=INDICES, current_symbol=user.current_symbol, current_interval=user.current_interval)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if not user.is_active_user:
                return render_template_string(LOGIN_HTML, error="Account is blocked or inactive.")
            session['user_id'] = user.id
            session['login_time'] = time.time()
            if user.is_admin:
                return redirect(url_for('admin_panel'))
            return redirect(url_for('index'))
        return render_template_string(LOGIN_HTML, error="Invalid username or password.")
    return render_template_string(LOGIN_HTML)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            return render_template_string(REGISTER_HTML, error="Username already exists.")
        new_user = User(
            username=username,
            password=generate_password_hash(password),
            is_admin=False,
            is_active_user=True
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template_string(REGISTER_HTML)

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user = User.query.get(session['user_id'])
    if not user or not user.is_admin:
        return redirect(url_for('index'))
    users = User.query.all()
    return render_template_string(ADMIN_HTML, users=users)

@app.route('/admin/toggle/<int:user_id>', methods=['POST'])
def toggle_user(user_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    admin = User.query.get(session['user_id'])
    if not admin or not admin.is_admin:
        return redirect(url_for('index'))
    target = User.query.get(user_id)
    if target and target.username != 'admin':
        target.is_active_user = not target.is_active_user
        db.session.commit()
    return redirect(url_for('admin_panel'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- TERMINAL API ENDPOINTS ---
@app.route('/api/change_symbol/<symbol>')
def api_change_symbol(symbol):
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    if symbol in INDICES:
        user.current_symbol = symbol
        user.position = None
        db.session.commit()
        if user.id in cached_real_data: del cached_real_data[user.id]
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})

@app.route('/api/set_interval/<interval>')
def api_set_interval(interval):
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    user.current_interval = interval
    user.position = None
    db.session.commit()
    if user.id in cached_real_data: del cached_real_data[user.id]
    return jsonify({"status": "success"})

@app.route('/api/reset_sim', methods=['POST'])
def api_reset_sim():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    user.position = None
    db.session.commit()
    if user.id in cached_real_data: del cached_real_data[user.id]
    return jsonify({"status": "success"})

@app.route('/api/trade', methods=['POST'])
def api_trade():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    data = request.get_json() or {}
    action = data.get('action')
    lots = int(data.get('lots', 1))
    sl_pts = float(data.get('sl_points', 20))
    tp_pts = float(data.get('target_points', 40))

    if user.position is not None:
        return jsonify({"status": "error", "message": "Position already open!"})
    if user.last_ltp == 0.0:
        return jsonify({"status": "error", "message": "Market data loading..."})

    user.position = action
    user.qty_lots = lots
    user.entry_price = user.last_ltp
    user.highest_price = user.last_ltp
    user.lowest_price = user.last_ltp

    if action == 'LONG':
        user.sl_price = round(user.entry_price - sl_pts, 2)
        user.target_price = round(user.entry_price + tp_pts, 2)
    else:
        user.sl_price = round(user.entry_price + sl_pts, 2)
        user.target_price = round(user.entry_price - tp_pts, 2)

    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/close_trade', methods=['POST'])
def api_close_trade():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    if user.position is not None and user.last_ltp > 0:
        lot_size = INDICES[user.current_symbol]["lot"]
        total_qty = lot_size * user.qty_lots
        pnl = (user.last_ltp - user.entry_price) * total_qty if user.position == 'LONG' else (user.entry_price - user.last_ltp) * total_qty
        user.realized_pnl = round(user.realized_pnl + pnl, 2)
        
        history = json.loads(user.trade_history)
        history.insert(0, {"type": user.position, "entry": user.entry_price, "exit": user.last_ltp, "pnl": round(pnl, 2)})
        user.trade_history = json.dumps(history)
        user.position = None
        db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/get_tick')
def api_get_tick():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    if not user.is_active_user:
        return jsonify({"status": "success", "force_logout": True})

    uid = user.id
    if uid not in cached_real_data or len(cached_real_data[uid]) == 0:
        try:
            ensure_login()
            idx = INDICES[user.current_symbol]
            params = {
                "exchange": idx["exchange"],
                "symboltoken": idx["token"],
                "interval": user.current_interval,
                "fromdate": f"{user.selected_date} 09:15",
                "todate": f"{user.selected_date} 15:30"
            }
            res = obj.getCandleData(params)
            if res and res.get('status') and res.get('data'):
                cached_real_data[uid] = res['data']
                candle_indices[uid] = 0
                candle_progresses[uid] = 0.0
                last_real_times[uid] = time.time()
                smooth_prices[uid] = float(res['data'][0][1])
                current_highs[uid] = float(res['data'][0][1])
                current_lows[uid] = float(res['data'][0][1])
            else:
                return jsonify({"status": "error", "message": "No data"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    real_data = cached_real_data[uid]
    idx_val = candle_indices[uid]
    if idx_val >= len(real_data): idx_val = len(real_data) - 1

    c = real_data[idx_val]
    open_p, high_p, low_p, close_p = float(c[1]), float(c[2]), float(c[3]), float(c[4])

    now = time.time()
    elapsed = now - last_real_times[uid]
    last_real_times[uid] = now
    candle_progresses[uid] += elapsed / 10.0
    if candle_progresses[uid] >= 1.0:
        candle_progresses[uid] = 0.0
        if candle_indices[uid] < len(real_data) - 1:
            candle_indices[uid] += 1
            idx_val = candle_indices[uid]
            c = real_data[idx_val]
            open_p, high_p, low_p, close_p = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            smooth_prices[uid] = open_p
            current_highs[uid] = open_p
            current_lows[uid] = open_p

    micro_noise = random.uniform(-0.2, 0.2)
    smooth_prices[uid] = smooth_prices[uid] + (close_p - smooth_prices[uid]) * 0.2 + micro_noise
    current_price = round(max(low_p, min(high_p, smooth_prices[uid])), 2)
    user.last_ltp = current_price

    current_highs[uid] = round(max(current_highs[uid], open_p, current_price), 2)
    current_lows[uid] = round(min(current_lows[uid], open_p, current_price), 2)

    trade_info = None
    if user.position:
        lot_size = INDICES[user.current_symbol]["lot"]
        total_qty = lot_size * user.qty_lots
        pnl = (current_price - user.entry_price) * total_qty if user.position == 'LONG' else (user.entry_price - current_price) * total_qty
        
        hit_exit = False
        exit_price = current_price

        if user.position == 'LONG':
            if user.sl_price > 0 and current_price <= user.sl_price:
                hit_exit = True; exit_price = user.sl_price
            elif user.target_price > 0 and current_price >= user.target_price:
                hit_exit = True; exit_price = user.target_price
        else:
            if user.sl_price > 0 and current_price >= user.sl_price:
                hit_exit = True; exit_price = user.sl_price
            elif user.target_price > 0 and current_price <= user.target_price:
                hit_exit = True; exit_price = user.target_price

        if hit_exit:
            final_pnl = (exit_price - user.entry_price) * total_qty if user.position == 'LONG' else (user.entry_price - exit_price) * total_qty
            user.realized_pnl = round(user.realized_pnl + final_pnl, 2)
            history = json.loads(user.trade_history)
            history.insert(0, {"type": user.position, "entry": user.entry_price, "exit": exit_price, "pnl": round(final_pnl, 2)})
            user.trade_history = json.dumps(history)
            user.position = None
        else:
            trade_info = {
                "position": user.position,
                "qty": total_qty,
                "entry_price": user.entry_price,
                "sl_price": user.sl_price,
                "target_price": user.target_price,
                "pnl": round(pnl, 2)
            }

    db.session.commit()

    history_payload = [
        {
            "time": int(datetime.fromisoformat(row[0]).timestamp()) + 19800,
            "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4])
        } for row in real_data[:idx_val]
    ]

    candle_dt = datetime.fromisoformat(c[0])
    current_candle = {
        "time": int(candle_dt.timestamp()) + 19800,
        "open": open_p,
        "high": current_highs[uid],
        "low": current_lows[uid],
        "close": current_price
    }

    return jsonify({
        "status": "success",
        "history": history_payload,
        "current_candle": current_candle,
        "realized_pnl": user.realized_pnl,
        "trade_info": trade_info
    })

if __name__ == '__main__':
    print("🚀 Sunny's Terminal running successfully!")
    print(f"  - Main Page: http://127.0.0.1:5000/")
    print(f"  - Login: http://127.0.0.1:5000/login")
    print(f"  - Admin: http://127.0.0.1:5000/admin")
    print(f"  - Admin Credentials: admin / admin123")
    app.run(port=5000, debug=True, use_reloader=False)
