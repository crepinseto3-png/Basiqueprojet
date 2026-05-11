# ESP32 Control Hub

Dashboard web temps-réel pour gérer des cartes ESP32 via WebSocket.

---

## 🗂 Structure du projet

```
esp32-dashboard/
├── server.js              ← Backend Node.js (API REST + WebSocket)
├── package.json           ← Dépendances npm
├── esp32_firmware.ino     ← Code Arduino à téléverser sur l'ESP32
├── public/
│   └── index.html         ← Frontend (interface web)
└── db/
    └── esp32.db           ← Base SQLite (créée automatiquement)
```

---

## 🚀 Démarrage du serveur

### 1. Installer les dépendances
```bash
cd esp32-dashboard
npm install
```

### 2. Lancer le serveur
```bash
npm start
# ou en mode développement (redémarrage automatique) :
npm run dev
```

### 3. Ouvrir le site
Navigateur → `http://localhost:3000`

---

## 📡 Configurer une carte ESP32

### Bibliothèques Arduino requises
Dans l'IDE Arduino, installez via le Gestionnaire de bibliothèques :
- `ArduinoWebsockets` (par Gil Maimon)
- `ArduinoJson` (par Benoit Blanchon)

### Configuration du fichier .ino
Ouvrez `esp32_firmware.ino` et modifiez les 5 variables :

```cpp
const char* WIFI_SSID     = "VotreSSID";
const char* WIFI_PASSWORD = "VotreMotDePasse";
const char* DEVICE_ID     = "ESP32_XXXXXXXX";   // Du site web
const char* API_TOKEN     = "token_ici";          // Du site web
const char* SERVER_URL    = "ws://192.168.1.100:3000"; // IP du serveur
```

> **Important** : L'ESP32 et le serveur doivent être sur le **même réseau WiFi**.
> Trouvez l'IP du serveur avec `ipconfig` (Windows) ou `ip addr` (Linux/Mac).

---

## 🌐 Hébergement en ligne (production)

Pour rendre le site accessible depuis Internet :

### Option 1 — VPS / Serveur cloud
```bash
# Sur votre serveur (Ubuntu)
git clone votre-repo
cd esp32-dashboard
npm install
# Utiliser PM2 pour garder le serveur actif
npm install -g pm2
pm2 start server.js --name esp32-hub
pm2 startup
```

Utilisez un domaine + certificat SSL (Nginx + Let's Encrypt) :
- WebSocket passera en `wss://` (sécurisé)
- Mettez à jour `SERVER_URL` dans le code ESP32

### Option 2 — Railway / Render (gratuit)
Ces plateformes supportent Node.js + WebSocket.
Ajoutez la variable d'environnement `PORT` si nécessaire.

---

## 📋 API REST

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/api/devices` | Lister tous les appareils |
| POST | `/api/devices` | Ajouter un appareil |
| DELETE | `/api/devices/:id` | Supprimer un appareil |
| GET | `/api/devices/:id` | Détails d'un appareil |
| GET | `/api/devices/:id/data` | Données capteurs |
| DELETE | `/api/devices/:id/data` | Vider les données |
| POST | `/api/devices/:id/command` | Envoyer une commande |
| GET | `/api/devices/:id/commands` | Historique commandes |

---

## 🔌 Protocole WebSocket

### ESP32 → Serveur
```json
// Authentification (à l'ouverture de la connexion)
{ "type": "esp32_auth", "device_id": "ESP32_XX", "api_token": "token" }

// Envoi de données capteurs
{ "type": "sensor_data", "data": { "temperature": 23.5, "humidity": 60 } }

// Confirmation d'exécution de commande
{ "type": "command_ack", "command_id": 42 }
```

### Serveur → ESP32
```json
// Réponse d'authentification
{ "type": "auth_ok" }

// Envoi d'une commande
{ "type": "command", "id": 42, "command": "LED_ON" }
```

---

## 🔒 Sécurité

Chaque ESP32 reçoit lors de son enregistrement :
- **Device ID** : identifiant unique
- **API Token** : token d'authentification (32 octets hex)
- **Secret Key** : clé secrète (16 octets hex) pour usage futur (HMAC, chiffrement)

Ces identifiants sont stockés dans la base SQLite et ne peuvent pas être récupérés après création (seulement régénérés en supprimant + recréant l'appareil).
