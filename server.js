const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const sqlite3 = require('sqlite3').verbose();
const crypto = require('crypto');
const path = require('path');
const cors = require('cors');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// ─── BASE DE DONNÉES SQLITE ───────────────────────────────────────────────────
const db = new sqlite3.Database('./db/esp32.db', (err) => {
  if (err) console.error('DB Error:', err);
  else console.log('✅ Base de données connectée');
});

db.serialize(() => {
  db.run(`CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_key TEXT NOT NULL,
    api_token TEXT NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME,
    is_online INTEGER DEFAULT 0
  )`);

  db.run(`CREATE TABLE IF NOT EXISTS sensor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(device_id) REFERENCES devices(id)
  )`);

  db.run(`CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME,
    FOREIGN KEY(device_id) REFERENCES devices(id)
  )`);
});

// ─── STOCKAGE DES CONNEXIONS WS ACTIVES ─────────────────────────────────────
const activeConnections = new Map(); // device_id -> ws
const browserClients = new Set();    // clients navigateur

function broadcastToBrowsers(event, data) {
  const msg = JSON.stringify({ event, data });
  browserClients.forEach(ws => {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  });
}

// ─── WEBSOCKET HANDLER ────────────────────────────────────────────────────────
wss.on('connection', (ws, req) => {
  let deviceId = null;
  let clientType = null;

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }

    // Identification initiale
    if (msg.type === 'browser_connect') {
      clientType = 'browser';
      browserClients.add(ws);
      console.log('🌐 Navigateur connecté');
      // Envoyer la liste des appareils en ligne
      db.all('SELECT * FROM devices', (err, rows) => {
        if (!err) ws.send(JSON.stringify({ event: 'device_list', data: rows.map(d => ({
          ...d,
          is_online: activeConnections.has(d.id) ? 1 : 0
        }))}));
      });
      return;
    }

    if (msg.type === 'esp32_auth') {
      // Authentification de la carte ESP32
      const { device_id, api_token } = msg;
      db.get('SELECT * FROM devices WHERE id = ? AND api_token = ?', [device_id, api_token], (err, row) => {
        if (err || !row) {
          ws.send(JSON.stringify({ type: 'auth_error', message: 'Identifiants invalides' }));
          ws.close();
          return;
        }
        deviceId = device_id;
        clientType = 'esp32';
        activeConnections.set(deviceId, ws);
        db.run('UPDATE devices SET is_online = 1, last_seen = CURRENT_TIMESTAMP WHERE id = ?', [deviceId]);
        ws.send(JSON.stringify({ type: 'auth_ok', message: 'Connecté' }));
        console.log(`✅ ESP32 connecté: ${row.name} (${deviceId})`);
        broadcastToBrowsers('device_online', { id: deviceId, name: row.name });

        // Envoyer les commandes en attente
        db.all('SELECT * FROM commands WHERE device_id = ? AND status = "pending"', [deviceId], (err, cmds) => {
          if (!err && cmds.length > 0) {
            cmds.forEach(cmd => {
              ws.send(JSON.stringify({ type: 'command', id: cmd.id, command: cmd.command }));
            });
          }
        });
      });
      return;
    }

    // Données capteur envoyées par ESP32
    if (msg.type === 'sensor_data' && clientType === 'esp32' && deviceId) {
      const payload = JSON.stringify(msg.data || msg.payload || msg);
      db.run('INSERT INTO sensor_data (device_id, payload) VALUES (?, ?)', [deviceId, payload]);
      db.run('UPDATE devices SET last_seen = CURRENT_TIMESTAMP WHERE id = ?', [deviceId]);
      broadcastToBrowsers('new_data', { device_id: deviceId, payload: JSON.parse(payload), ts: new Date().toISOString() });
    }

    // Confirmation d'exécution de commande par ESP32
    if (msg.type === 'command_ack' && clientType === 'esp32') {
      db.run('UPDATE commands SET status = "executed", executed_at = CURRENT_TIMESTAMP WHERE id = ?', [msg.command_id]);
      broadcastToBrowsers('command_executed', { command_id: msg.command_id, device_id: deviceId });
    }
  });

  ws.on('close', () => {
    if (clientType === 'browser') {
      browserClients.delete(ws);
    }
    if (clientType === 'esp32' && deviceId) {
      activeConnections.delete(deviceId);
      db.run('UPDATE devices SET is_online = 0 WHERE id = ?', [deviceId]);
      console.log(`❌ ESP32 déconnecté: ${deviceId}`);
      broadcastToBrowsers('device_offline', { id: deviceId });
    }
  });
});

// ─── API REST ─────────────────────────────────────────────────────────────────

// Lister tous les appareils
app.get('/api/devices', (req, res) => {
  db.all('SELECT * FROM devices ORDER BY created_at DESC', (err, rows) => {
    if (err) return res.status(500).json({ error: err.message });
    res.json(rows.map(d => ({ ...d, is_online: activeConnections.has(d.id) ? 1 : 0 })));
  });
});

// Ajouter un nouvel appareil ESP32
app.post('/api/devices', (req, res) => {
  const { name, description } = req.body;
  if (!name) return res.status(400).json({ error: 'Nom requis' });

  const id = 'ESP32_' + crypto.randomBytes(4).toString('hex').toUpperCase();
  const secret_key = crypto.randomBytes(16).toString('hex');
  const api_token = crypto.randomBytes(24).toString('hex');

  db.run(
    'INSERT INTO devices (id, name, secret_key, api_token, description) VALUES (?, ?, ?, ?, ?)',
    [id, name, secret_key, api_token, description || ''],
    function(err) {
      if (err) return res.status(500).json({ error: err.message });
      broadcastToBrowsers('device_added', { id, name, description, is_online: 0, created_at: new Date().toISOString() });
      res.json({ id, name, secret_key, api_token, description, message: 'Appareil créé avec succès' });
    }
  );
});

// Supprimer un appareil
app.delete('/api/devices/:id', (req, res) => {
  const { id } = req.params;
  if (activeConnections.has(id)) activeConnections.get(id).close();
  db.run('DELETE FROM devices WHERE id = ?', [id], (err) => {
    if (err) return res.status(500).json({ error: err.message });
    broadcastToBrowsers('device_removed', { id });
    res.json({ success: true });
  });
});

// Informations d'un appareil
app.get('/api/devices/:id', (req, res) => {
  db.get('SELECT * FROM devices WHERE id = ?', [req.params.id], (err, row) => {
    if (err || !row) return res.status(404).json({ error: 'Introuvable' });
    res.json({ ...row, is_online: activeConnections.has(row.id) ? 1 : 0 });
  });
});

// Données capteur d'un appareil (avec pagination)
app.get('/api/devices/:id/data', (req, res) => {
  const limit = parseInt(req.query.limit) || 100;
  const offset = parseInt(req.query.offset) || 0;
  db.all(
    'SELECT * FROM sensor_data WHERE device_id = ? ORDER BY received_at DESC LIMIT ? OFFSET ?',
    [req.params.id, limit, offset],
    (err, rows) => {
      if (err) return res.status(500).json({ error: err.message });
      res.json(rows.map(r => ({ ...r, payload: JSON.parse(r.payload) })));
    }
  );
});

// Envoyer une commande à un ESP32
app.post('/api/devices/:id/command', (req, res) => {
  const { id } = req.params;
  const { command } = req.body;
  if (!command) return res.status(400).json({ error: 'Commande requise' });

  db.run(
    'INSERT INTO commands (device_id, command) VALUES (?, ?)',
    [id, command],
    function(err) {
      if (err) return res.status(500).json({ error: err.message });
      const cmdId = this.lastID;
      const ws = activeConnections.get(id);
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'command', id: cmdId, command }));
        db.run('UPDATE commands SET status = "sent" WHERE id = ?', [cmdId]);
        res.json({ id: cmdId, status: 'sent', command });
      } else {
        res.json({ id: cmdId, status: 'pending', command, note: 'ESP32 hors ligne, commande en attente' });
      }
    }
  );
});

// Historique des commandes
app.get('/api/devices/:id/commands', (req, res) => {
  db.all(
    'SELECT * FROM commands WHERE device_id = ? ORDER BY sent_at DESC LIMIT 50',
    [req.params.id],
    (err, rows) => {
      if (err) return res.status(500).json({ error: err.message });
      res.json(rows);
    }
  );
});

// Supprimer les données d'un appareil
app.delete('/api/devices/:id/data', (req, res) => {
  db.run('DELETE FROM sensor_data WHERE device_id = ?', [req.params.id], (err) => {
    if (err) return res.status(500).json({ error: err.message });
    res.json({ success: true });
  });
});

// ─── DÉMARRAGE ────────────────────────────────────────────────────────────────
const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`\n🚀 Serveur démarré sur http://localhost:${PORT}`);
  console.log(`📡 WebSocket actif sur ws://localhost:${PORT}`);
  console.log(`📁 Base de données: ./db/esp32.db\n`);
});
