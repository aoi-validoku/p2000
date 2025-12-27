# 📟 p2000-mon

**p2000-mon** is een realtime P2000-monitor voor Nederland, gebaseerd op **RTL-SDR**, **multimon-ng** en een **Python WebSocket-server**.  
Het project decodeert P2000/FLEX-berichten en toont ze live in een webinterface met filtering, prioriteiten, kleurcodering en historie.

---

## ✨ Features

- 📡 Realtime P2000/FLEX decoding via `rtl_fm` + `multimon-ng`
- 🌐 Webinterface (HTTP + WebSockets)
- 🔔 Nieuwe meldingen gemarkeerd (bel-emoji bij < 5 minuten oud)
- 🚑 Prioriteitenkolom (A0 / A1 / A2 / B1 / B2 / P1 / TEST)
- 🎨 Kleurcodering per dienst:
  - Brandweer → rood
  - Ambulance → lichtblauw
  - Politie → donkerblauw
  - Traumaheli (MMT/Lifeliner) → paars
- 🔍 Filters:
  - **Alles** (geen filter)
  - **Beemster** (filtert op tekst “Beemster”)
- 🕒 UTC / Zulu klok
- 💾 JSON-database met **3 dagen retentie**
- ⚙️ Geschikt voor headless systemen (zoals RevPi)

---

## 🧱 Architectuur

RTL-SDR  
→ rtl_fm (FM demodulatie)  
→ multimon-ng (FLEX/P2000 decoder)  
→ p2000_server.py  
  • WebSocket server (live updates)  
  • HTTP server (web UI)  
  • JSON database (p2000_history.json)

---

## 📂 Bestanden

| Bestand | Functie |
|-------|--------|
| `p2000_server.py` | Hoofdapplicatie |
| `capcodelijst.csv` | Capcode-database |
| `p2000_history.json` | Historie (laatste 3 dagen) |
| `README.md` | Documentatie |

---

## 🚀 Installatie (algemeen)

### Vereisten
- Linux (getest op Debian 12 / RevPi)
- Python ≥ 3.9
- RTL-SDR dongle
- Internettoegang

### Benodigde pakketten

```bash
apt update
apt install -y git build-essential cmake pkg-config \
  libusb-1.0-0-dev libpulse-dev libx11-dev rtl-sdr \
  python3 python3-pip
```

---

## 🔧 multimon-ng bouwen

```bash
git clone https://github.com/Zanoroy/multimon-ng.git
cd multimon-ng
mkdir build && cd build
cmake ..
make
make install
```

Test decoder:

```bash
rtl_fm -f 169.65M -M fm -s 22050 -p 83 -g 30 | multimon-ng -a FLEX -t raw -
```

---

## 📥 Capcodelijst

```bash
wget https://p2000.bommel.net/cap2csv.php
mv cap2csv.php capcodelijst.csv
```

---

## ▶️ Starten

```bash
python3 p2000_server.py
```

Debug:

```bash
python3 p2000_server.py -v
```

Webinterface:

http://<ip>:8112

---

## 🏭 Deployment op Revolution Pi (RevPi)

Gebaseerd op praktijkgebruik:

```bash
rtl_biast -b 1
rtl_tcp -a 0.0.0.0 -g 45
```

Installeer dependencies:

```bash
apt install -y git cmake build-essential libusb-1.0-0-dev
```

Headless starten:

```bash
nohup python3 p2000_server.py -v &
```

Controleer:

```bash
ss -tulpen | grep 811
```

---

## 🗃️ Dataretentie

- Historie wordt opgeslagen in `p2000_history.json`
- Automatisch opgeschoond tot **3 dagen**
- Instelbaar via:

```python
RETENTION_DAYS = 3
```

---

## 📜 Licentie

Gebruik op eigen risico.  
Bedoeld voor hobby, monitoring en educatie.
