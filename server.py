"""
ESP32 Control Hub — Serveur Python
Requiert : pip install flask flask-socketio simple-websocket
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, disconnect
import sqlite3, os, secrets, json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'db', 'esp32.db')
STATIC   = os.path.join(BASE_DIR, 'public')

os.makedirs(os.path.join(BASE_DIR, 'db'), exist_ok=True)

app = Flask(__name__, static_folder=STATIC)
app.config['SECRET_KEY'] = secrets.token_hex(32)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── sid → device_id (pour ESP32 authentifiés)
sid_to_device  = {}   # sid  -> device_id
device_to_sid  = {}   # device_id -> sid  (ESP32)
browser_sids   = set()

# ─── BASE DE DONNÉES ──────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            secret_key TEXT NOT NULL,
            api_token TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            last_seen TEXT,
            is_online INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sensor_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            received_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(device_id) REFERENCES devices(id)
        );
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            command TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            sent_at TEXT DEFAULT (datetime('now')),
            executed_at TEXT
        );
        """)
    print("✅ Base de données prête")

init_db()

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def broadcast_to_browsers(event, data):
    for sid in list(browser_sids):
        socketio.emit('message', {'event': event, 'data': data}, to=sid)

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ─── WEBSOCKET EVENTS ─────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    print(f"[WS] Connexion: {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    browser_sids.discard(sid)
    device_id = sid_to_device.pop(sid, None)
    if device_id:
        device_to_sid.pop(device_id, None)
        with get_db() as db:
            db.execute("UPDATE devices SET is_online=0 WHERE id=?", (device_id,))
        print(f"[ESP32] Déconnecté: {device_id}")
        broadcast_to_browsers('device_offline', {'id': device_id})

@socketio.on('message')
def on_message(raw):
    sid = request.sid
    try:
        msg = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception:
        return

    msg_type = msg.get('type')

    # ── Navigateur
    if msg_type == 'browser_connect':
        browser_sids.add(sid)
        with get_db() as db:
            devices = rows_to_list(db.execute("SELECT * FROM devices").fetchall())
        # Marquer les online
        for d in devices:
            d['is_online'] = 1 if d['id'] in device_to_sid else 0
        emit('message', {'event': 'device_list', 'data': devices})
        return

    # ── Authentification ESP32
    if msg_type == 'esp32_auth':
        did   = msg.get('device_id', '')
        token = msg.get('api_token', '')
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM devices WHERE id=? AND api_token=?", (did, token)
            ).fetchone()
        if not row:
            emit('message', {'type': 'auth_error', 'message': 'Identifiants invalides'})
            disconnect()
            return
        sid_to_device[sid] = did
        device_to_sid[did]  = sid
        with get_db() as db:
            db.execute("UPDATE devices SET is_online=1, last_seen=datetime('now') WHERE id=?", (did,))
        emit('message', {'type': 'auth_ok', 'message': 'Connecté'})
        print(f"[ESP32] Authentifié: {row['name']} ({did})")
        broadcast_to_browsers('device_online', {'id': did, 'name': row['name']})
        # Commandes en attente
        with get_db() as db:
            pending = rows_to_list(db.execute(
                "SELECT * FROM commands WHERE device_id=? AND status='pending'", (did,)
            ).fetchall())
        for cmd in pending:
            emit('message', {'type': 'command', 'id': cmd['id'], 'command': cmd['command']})
        return

    device_id = sid_to_device.get(sid)
    if not device_id:
        return

    # ── Données capteurs
    if msg_type == 'sensor_data':
        payload = json.dumps(msg.get('data') or msg.get('payload') or {})
        with get_db() as db:
            db.execute("INSERT INTO sensor_data (device_id, payload) VALUES (?,?)", (device_id, payload))
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
        broadcast_to_browsers('new_data', {
            'device_id': device_id,
            'payload': json.loads(payload),
            'ts': datetime.utcnow().isoformat() + 'Z'
        })

    # ── Confirmation de commande
    if msg_type == 'command_ack':
        cmd_id = msg.get('command_id')
        with get_db() as db:
            db.execute(
                "UPDATE commands SET status='executed', executed_at=datetime('now') WHERE id=?",
                (cmd_id,)
            )
        broadcast_to_browsers('command_executed', {'command_id': cmd_id, 'device_id': device_id})

# ─── API REST ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC, 'index.html')

@app.route('/api/devices', methods=['GET'])
def list_devices():
    with get_db() as db:
        devices = rows_to_list(db.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall())
    for d in devices:
        d['is_online'] = 1 if d['id'] in device_to_sid else 0
    return jsonify(devices)

@app.route('/api/devices', methods=['POST'])
def add_device():
    body = request.get_json() or {}
    name = body.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Nom requis'}), 400
    did        = 'ESP32_' + secrets.token_hex(4).upper()
    secret_key = secrets.token_hex(16)
    api_token  = secrets.token_hex(24)
    desc       = body.get('description', '')
    with get_db() as db:
        db.execute(
            "INSERT INTO devices (id,name,secret_key,api_token,description) VALUES (?,?,?,?,?)",
            (did, name, secret_key, api_token, desc)
        )
    device_data = {'id': did, 'name': name, 'description': desc, 'is_online': 0, 'created_at': datetime.utcnow().isoformat()}
    broadcast_to_browsers('device_added', device_data)
    return jsonify({'id': did, 'name': name, 'secret_key': secret_key, 'api_token': api_token, 'description': desc})

@app.route('/api/devices/<did>', methods=['GET'])
def get_device(did):
    with get_db() as db:
        row = db.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
    if not row:
        return jsonify({'error': 'Introuvable'}), 404
    d = row_to_dict(row)
    d['is_online'] = 1 if did in device_to_sid else 0
    return jsonify(d)

@app.route('/api/devices/<did>', methods=['DELETE'])
def delete_device(did):
    if did in device_to_sid:
        socketio.disconnect(device_to_sid[did])
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE id=?", (did,))
        db.execute("DELETE FROM sensor_data WHERE device_id=?", (did,))
        db.execute("DELETE FROM commands WHERE device_id=?", (did,))
    broadcast_to_browsers('device_removed', {'id': did})
    return jsonify({'success': True})

@app.route('/api/devices/<did>/data', methods=['GET'])
def get_data(did):
    limit  = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    with get_db() as db:
        rows = rows_to_list(db.execute(
            "SELECT * FROM sensor_data WHERE device_id=? ORDER BY received_at DESC LIMIT ? OFFSET ?",
            (did, limit, offset)
        ).fetchall())
    for r in rows:
        try:
            r['payload'] = json.loads(r['payload'])
        except Exception:
            pass
    return jsonify(rows)

@app.route('/api/devices/<did>/data', methods=['DELETE'])
def clear_data(did):
    with get_db() as db:
        db.execute("DELETE FROM sensor_data WHERE device_id=?", (did,))
    return jsonify({'success': True})

@app.route('/api/devices/<did>/command', methods=['POST'])
def send_command(did):
    body = request.get_json() or {}
    command = body.get('command', '').strip()
    if not command:
        return jsonify({'error': 'Commande requise'}), 400
    with get_db() as db:
        cur = db.execute("INSERT INTO commands (device_id,command) VALUES (?,?)", (did, command))
        cmd_id = cur.lastrowid
    esp_sid = device_to_sid.get(did)
    if esp_sid:
        socketio.emit('message', {'type': 'command', 'id': cmd_id, 'command': command}, to=esp_sid)
        with get_db() as db:
            db.execute("UPDATE commands SET status='sent' WHERE id=?", (cmd_id,))
        return jsonify({'id': cmd_id, 'status': 'sent', 'command': command})
    return jsonify({'id': cmd_id, 'status': 'pending', 'command': command, 'note': 'ESP32 hors ligne, commande en attente'})

@app.route('/api/devices/<did>/commands', methods=['GET'])
def get_commands(did):
    with get_db() as db:
        rows = rows_to_list(db.execute(
            "SELECT * FROM commands WHERE device_id=? ORDER BY sent_at DESC LIMIT 50", (did,)
        ).fetchall())
    return jsonify(rows)

# ─── LANCEMENT ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f"\n🚀 Serveur sur http://localhost:{port}")
    print(f"📡 WebSocket actif sur ws://localhost:{port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
