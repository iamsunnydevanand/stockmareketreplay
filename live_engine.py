import os
from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from SmartApi import SmartConnect
from pyotp import TOTP
from datetime import datetime
import time
import random
import json

app = Flask(__name__)
app.secret_key = "sunny_dev_anand_secret_123"

# Render ke liye database path fix (Disk attach kiya hai toh /data/final_app.db banega)
db_path = os.environ.get('DATABASE_PATH', 'final_app.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    position = db.Column(db.String(10), default=None)
    entry_price = db.Column(db.Float, default=0.0)
    sl_price = db.Column(db.Float, default=0.0)
    target_price = db.Column(db.Float, default=0.0)
    pnl = db.Column(db.Float, default=0.0)
    realized_pnl = db.Column(db.Float, default=0.0)
    last_ltp = db.Column(db.Float, default=0.0)

with app.app_context():
    db.create_all()
    if not User.query.filter_by(username="admin").first():
        # 🔐 Admin Password Yahan Change Karein (Abhi admin123 hai)
        admin = User(username="admin", password=generate_password_hash("admin123"), is_admin=True)
        db.session.add(admin)
        db.session.commit()

api_key = "BAq0de8E"                 
client_id = "S1263568"               
password = "7313"                    
totp_secret = "LY4QG5NPAOBDYIZXODIDWLQKN4"  
obj = SmartConnect(api_key=api_key)
session_initialized = False

INDICES = {
    "SENSEX": {"token": "99919000", "exchange": "BSE", "name": "SENSEX", "lot": 10},
    "NIFTY":  {"token": "99926000", "exchange": "NSE", "name": "NIFTY 50", "lot": 50},
    "BANKNIFTY": {"token": "99926009", "exchange": "NSE", "name": "BANKNIFTY", "lot": 15}
}

def ensure_login():
    global session_initialized
    if not session_initialized:
        try:
            totp = TOTP(totp_secret).now()
            res = obj.generateSession(client_id, password, totp)
            if res and res.get('status'): session_initialized = True
        except: pass

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if session.get('is_admin'):
        return redirect(url_for('admin_panel'))
    return redirect(url_for('terminal'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if not user.is_active:
                return render_template_string(LOGIN_HTML, error="Account blocked by Admin.")
            session['user_id'] = user.id
            session['username'] = user.username
            session['is_admin'] = user.is_admin
            session['login_time'] = datetime.now().timestamp()
            if user.is_admin: return redirect(url_for('admin_panel'))
            return redirect(url_for('terminal'))
        return render_template_string(LOGIN_HTML, error="Invalid Credentials")
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            return render_template_string(REGISTER_HTML, error="Username exists")
        new_user = User(username=username, password=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template_string(REGISTER_HTML, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    users = User.query.all()
    return render_template_string(ADMIN_HTML, users=users)

@app.route('/admin/toggle/<int:user_id>')
def toggle_user(user_id):
    if session.get('is_admin'):
        user = User.query.get(user_id)
        if user:
            user.is_active = not user.is_active
            db.session.commit()
    return redirect(url_for('admin_panel'))

@app.route('/terminal')
def terminal():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    
    if not user or not user.is_active: 
        session.clear()
        return redirect(url_for('login'))
    
    # Admin ko full access (bina trial ke)
    if user.is_admin:
        return render_template_string(TERMINAL_HTML, trial=999999, username=session['username'])
        
    # Normal users ko 2 minute ka trial
    trial_remaining = max(0, 120 - int(time.time() - session.get('login_time', time.time())))
    return render_template_string(TERMINAL_HTML, trial=trial_remaining, username=session['username'])

@app.route('/api/get_tick')
def get_tick():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    if not user: return jsonify({"status": "error"})
    
    symbol = request.args.get('symbol', 'SENSEX')
    date = request.args.get('date', '2026-10-26')
    
    try:
        ensure_login()
        idx = INDICES.get(symbol, INDICES['SENSEX'])
        params = {"exchange": idx["exchange"], "symboltoken": idx["token"], "interval": "FIVE_MINUTE", "fromdate": f"{date} 09:15", "todate": f"{date} 15:30"}
        response = obj.getCandleData(params)
        if response and response.get('status') and response.get('data'):
            c = response['data'][int(time.time()) % len(response['data'])]
            open_p, high_p, low_p, close_p = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        else:
            open_p, high_p, low_p, close_p = 73000, 73200, 72800, 73100
    except:
        open_p, high_p, low_p, close_p = 73000, 73200, 72800, 73100

    progress = random.uniform(0, 1)
    current_price = open_p + (close_p - open_p) * progress
    current_candle = {"time": int(time.time()), "open": open_p, "high": max(current_price, open_p)+10, "low": min(current_price, open_p)-10, "close": current_price}
    user.last_ltp = round(current_price, 2)
    
    trade_info = None
    if user.position:
        lot = INDICES.get(symbol, INDICES['SENSEX'])["lot"]
        pnl = (current_price - user.entry_price) * lot if user.position == 'LONG' else (user.entry_price - current_price) * lot
        user.pnl = round(pnl, 2)
        trade_info = {"auto_closed": False, "position": user.position, "pnl": user.pnl}
    db.session.commit()
    
    return jsonify({"status": "success", "current_candle": current_candle, "trade_info": trade_info, "realized_pnl": user.realized_pnl, "symbol": INDICES.get(symbol, INDICES['SENSEX'])["name"]})

@app.route('/api/trade', methods=['POST'])
def trade():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    data = request.get_json() or {}
    if user.position: return jsonify({"status": "error", "message": "Position open!"})
    user.position = data.get('action')
    user.entry_price = round(user.last_ltp, 2)
    user.sl_price = round(user.entry_price - 20, 2) if user.position == 'LONG' else round(user.entry_price + 20, 2)
    user.target_price = round(user.entry_price + 40, 2) if user.position == 'LONG' else round(user.entry_price - 40, 2)
    db.session.commit()
    return jsonify({"status": "success"})

@app.route('/api/close_trade', methods=['POST'])
def close_trade():
    if 'user_id' not in session: return jsonify({"status": "error"})
    user = User.query.get(session['user_id'])
    if not user.position: return jsonify({"status": "error"})
    final_pnl = user.pnl
    user.realized_pnl += final_pnl
    user.position = None; user.entry_price = 0.0; user.pnl = 0.0
    db.session.commit()
    return jsonify({"status": "success", "final_pnl": final_pnl})

# --- HTML TEMPLATES ---
OWNER_HTML = """
<div style="margin-top:20px; padding:15px; background:#131722; border:1px solid #2962ff; border-radius:8px; text-align:center; font-size:12px;">
    <h4 style="margin:0 0 5px 0; color:#089981;">Owner Info</h4>
    <p style="margin:2px; font-weight:bold; color:white;">Sunny Dev Anand</p>
    <p style="margin:2px; color:#787b86;">Software Engineer</p>
    <p style="margin:2px;"><a href="mailto:itsmywayokok@gmail.com" style="color:#2962ff; text-decoration:none;">itsmywayokok@gmail.com</a></p>
    <p style="margin:2px;"><a href="https://wa.me/917667765215" style="color:#089981; text-decoration:none;">+91 7667765215 (WhatsApp)</a></p>
</div>
"""

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
<table><tr><th>ID</th><th>Username</th><th>Status</th><th>PnL</th><th>Action</th></tr>
{% for user in users %}<tr><td>{{ user.id }}</td><td>{{ user.username }}</td>
<td style="color:{% if user.is_active %}#089981{% else %}#ef5350{% endif %};">{% if user.is_active %}Active{% else %}Blocked{% endif %}</td>
<td>₹ {{ user.realized_pnl }}</td>
<td>{% if user.is_active %}<a href="/admin/toggle/{{ user.id }}" class="btn btn-red">Block</a>{% else %}<a href="/admin/toggle/{{ user.id }}" class="btn btn-green">Activate</a>{% endif %}</td></tr>{% endfor %}
</table></body></html>
"""

TERMINAL_HTML = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Sunny's Terminal</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    body {{ font-family: -apple-system, sans-serif; background-color: #131722; color: #d1d4dc; margin: 0; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }}
    .header {{ background: #1e222d; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2a2e39; }}
    .header h1 {{ margin: 0; font-size: 18px; color: #2962ff; }}
    .header .timer {{ color: #ef5350; font-weight: bold; }}
    .header a {{ color: #2962ff; text-decoration: none; font-weight: bold; }}
    .chart {{ flex: 1; position: relative; }}
    #chart-container {{ width: 100%; height: 100%; }}
    .footer {{ background: #1e222d; padding: 10px; text-align: center; border-top: 1px solid #2a2e39; }}
    .trial-overlay {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.95); z-index: 9999; display: none; justify-content: center; align-items: center; flex-direction: column; color: white; }}
    .trial-box {{ background: #1e222d; padding: 30px; border-radius: 10px; border: 1px solid #2962ff; text-align: center; max-width: 400px; }}
    .trial-box h2 {{ color: #2962ff; }}
    .trial-box a {{ color: #089981; text-decoration: none; font-weight: bold; }}
    .floating-owner {{ position: absolute; bottom: 20px; left: 20px; cursor: pointer; background: #2962ff; color: white; padding: 8px 12px; border-radius: 20px; font-weight: bold; z-index: 10; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>Sunny's Terminal</h1>
    <div>
      <select id="symbolSelect" style="background:#131722; color:white; border:1px solid #2a2e39; padding:5px; border-radius:5px;">
        <option value="SENSEX">SENSEX</option>
        <option value="NIFTY">NIFTY</option>
      </select>
      <span class="timer" id="timerDisplay" style="margin-left:15px;"></span>
      <a href="/logout" style="margin-left:15px;">Logout ({{{{ username }}}})</a>
    </div>
  </div>
  <div class="chart">
    <div id="chart-container"></div>
    <div class="floating-owner" onclick="alert('Owner: Sunny Dev Anand\\nSoftware Engineer\\nEmail: itsmywayokok@gmail.com\\nWhatsApp: +917667765215')">👤 Owner Info</div>
  </div>
  <div class="footer">
    Realized PnL: <strong id="realizedPnl">₹ 0.00</strong>
  </div>
  <div class="trial-overlay" id="trialOverlay">
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
    let trialSeconds = {{{{ trial }}}};
    function updateTimer() {{
        if (trialSeconds <= 0) {{ document.getElementById('trialOverlay').style.display = 'flex'; return; }}
        let m = Math.floor(trialSeconds / 60), s = trialSeconds % 60;
        document.getElementById('timerDisplay').innerText = `⏳ ${{m}}:${{s < 10 ? '0' : ''}}${{s}}`;
        trialSeconds--;
    }}
    if (trialSeconds <= 0) document.getElementById('trialOverlay').style.display = 'flex';
    else {{ updateTimer(); setInterval(updateTimer, 1000); }}
    
    const chart = LightweightCharts.createChart(document.getElementById('chart-container'), {{
      layout: {{ background: {{ color: '#131722' }}, textColor: '#d1d4dc' }},
      grid: {{ vertLines: {{ color: '#1e222d' }}, horzLines: {{ color: '#1e222d' }} }},
      timeScale: {{ timeVisible: true }}
    }});
    const candleSeries = chart.addCandlestickSeries({{ upColor: '#089981', downColor: '#ef5350', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#ef5350' }});
    
    async function fetchData() {{
      if (trialSeconds <= 0) return;
      let sym = document.getElementById('symbolSelect').value;
      try {{
        const res = await fetch(`/api/get_tick?symbol=${{sym}}`);
        const data = await res.json();
        if (data.status === 'success') {{
          candleSeries.update(data.current_candle);
          document.getElementById('realizedPnl').innerText = `₹ ${{data.realized_pnl.toFixed(2)}}`;
        }}
      }} catch(e) {{}}
    }}
    setInterval(fetchData, 1000);
    window.addEventListener('resize', () => {{ chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight - 80 }}); }});
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
