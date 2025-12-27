#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#Author: Aviel Ossi
#Date: 2025-12-27
#Version: 1.0.0
#Description: P2000 FLEX monitor
#License: MIT
#Copyright: Aviel Ossi
#Contact: aviel.ossi@gmail.com
#Website: https://github.com/avielossi/p2000-mon
#GitHub: https://github.com/avielossi/p2000-mon
#Twitter: https://twitter.com/avieloss

import asyncio
import subprocess
import csv
import json
import argparse
import re
import logging
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Set, Dict, List, Any

from aiohttp import web
import websockets

# ================= ARGUMENTS =================

parser = argparse.ArgumentParser(description="P2000 FLEX monitor (asyncio)")
parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
args = parser.parse_args()

# ================= LOGGING =================

logging.basicConfig(
    level=logging.DEBUG if args.verbose else logging.INFO,
    format="[%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================

HTTP_PORT = 8112
WS_PORT = 8113
RETENTION_DAYS = 3
MAX_MESSAGES = 10000  # Prevent memory issues
HISTORY_SAVE_INTERVAL = 30  # Save history every 30 seconds instead of every message

BASE_DIR = Path(__file__).parent
CAPCODE_FILE = BASE_DIR / "capcodelijst.csv"
DB_FILE = BASE_DIR / "p2000_history.json"

messages: List[Dict[str, Any]] = []
clients: Set[websockets.WebSocketServerProtocol] = set()
capcodes: Dict[str, Dict[str, str]] = {}
shutdown_event = asyncio.Event()

# ================= HELPERS =================

def extract_prio(text: str) -> str:
    m = re.search(r"\b(A0|A1|A2|B1|B2|P\s*1|TEST)\b", text, re.I)
    return m.group(1).replace(" ", "").upper() if m else "-"

def capcode_candidates(raw: str):
    raw = raw.strip()
    out = set()
    if raw.isdigit() and len(raw) >= 9:
        out.add(raw[-7:])
    if raw.isdigit() and len(raw) == 7:
        out.add(raw)
        out.add(raw.zfill(7))
    return out

def prune_old_messages() -> None:
    """Remove messages older than RETENTION_DAYS and limit total count."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    messages[:] = [
        m for m in messages
        if datetime.fromisoformat(m["time_utc"]) >= cutoff
    ]
    # Also limit total messages to prevent memory issues
    if len(messages) > MAX_MESSAGES:
        messages[:] = messages[:MAX_MESSAGES]

# ================= CAPCODES =================

def load_capcodes() -> None:
    """Load capcode database from CSV file."""
    if not CAPCODE_FILE.exists():
        logger.warning(f"Capcode file not found: {CAPCODE_FILE}")
        return
    
    try:
        with open(CAPCODE_FILE, encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";", quotechar='"')
            for r in reader:
                if len(r) < 5:
                    continue
                capcodes[r[0].zfill(7)] = {
                    "dienst": r[1],
                    "provincie": r[2],
                    "regio": r[3],
                    "eenheid": r[4],
                }
        logger.info(f"Capcodes geladen: {len(capcodes)}")
    except Exception as e:
        logger.error(f"Error loading capcodes: {e}", exc_info=True)

def resolve_capcodes(raw: str) -> str:
    out = []
    for t in raw.split():
        info, used = None, None
        for cc in capcode_candidates(t):
            if cc in capcodes:
                info, used = capcodes[cc], cc
                break

        if info:
            d = info["dienst"].lower()
            e = info["eenheid"].lower()
            if any(x in e for x in ("trauma", "heli", "lifeliner", "mmt")):
                css = "dienst-trauma"
            elif "brandweer" in d:
                css = "dienst-brandweer"
            elif "ambulance" in d or "rav" in d or "ghor" in d:
                css = "dienst-ambulance"
            elif "politie" in d or "kmar" in d:
                css = "dienst-politie"
            else:
                css = "dienst-onbekend"

            out.append(
                f"<span class='{css}'>{used} – {info['dienst']} | {info['eenheid']} ({info['regio']})</span>"
            )
        else:
            out.append(f"<span class='dienst-onbekend'>{t} – Onbekend</span>")
    return "<br>".join(out)

# ================= HISTORY =================

def load_history() -> None:
    """Load message history from JSON file."""
    if not DB_FILE.exists():
        logger.info("No history file found, starting fresh")
        return
    
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            data = json.load(f)
            for m in data:
                m.setdefault("prio", extract_prio(m.get("text", "")))
                messages.append(m)
        prune_old_messages()
        logger.info(f"Loaded {len(messages)} messages from history")
    except Exception as e:
        logger.error(f"Error loading history: {e}", exc_info=True)

async def save_history_async() -> None:
    """Save message history to JSON file asynchronously."""
    try:
        # avoid blocking event loop on file IO
        data = json.dumps(messages, indent=2, ensure_ascii=False)
        await asyncio.to_thread(DB_FILE.write_text, data, encoding="utf-8")
    except Exception as e:
        logger.error(f"Error saving history: {e}", exc_info=True)

# ================= WEBSOCKET =================

async def ws_handler(ws: websockets.WebSocketServerProtocol) -> None:
    """Handle WebSocket client connection."""
    clients.add(ws)
    logger.debug(f"WebSocket client connected. Total clients: {len(clients)}")
    try:
        # Keep connection open; browser doesn't send data.
        async for _ in ws:
            pass
    except websockets.exceptions.ConnectionClosed:
        logger.debug("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        clients.discard(ws)
        logger.debug(f"WebSocket client removed. Total clients: {len(clients)}")

async def broadcast(msg: str) -> None:
    """Broadcast message to all connected WebSocket clients."""
    if not clients:
        return
    dead = []
    for ws in list(clients):
        try:
            await ws.send(msg)
        except websockets.exceptions.ConnectionClosed:
            dead.append(ws)
        except Exception as e:
            logger.debug(f"Error sending to WebSocket client: {e}")
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

async def ws_server() -> None:
    """Start WebSocket server."""
    async with websockets.serve(ws_handler, "", WS_PORT):
        logger.info(f"WebSocket listening on port {WS_PORT}")
        await shutdown_event.wait()

# ================= DECODER =================

async def start_decoder() -> None:
    """Start the RTL-SDR decoder subprocess and process messages."""
    cmd = "rtl_fm -f 169.65M -M fm -s 22050 -p 83 -g 30 | multimon-ng -a FLEX -t raw -"
    proc: Optional[asyncio.subprocess.Process] = None
    
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdout is None:
            logger.error("Decoder process stdout is None")
            return
        
        logger.info("Decoder started")
        last_save_time = datetime.now(timezone.utc)

        while not shutdown_event.is_set():
            line = await proc.stdout.readline()
            if not line:
                logger.warning("Decoder process ended")
                break

            l = line.decode(errors="ignore").strip()
            if not l.startswith("FLEX|"):
                continue

            try:
                _, ts, *_, caps, typ, txt = l.split("|", 6)
            except ValueError as e:
                logger.debug(f"Failed to parse decoder line: {l[:50]}... Error: {e}")
                continue

            e = {
                "time_local": ts,
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "prio": extract_prio(txt),
                "capcodes_named": resolve_capcodes(caps),
                "type": typ,
                "text": txt,
            }

            messages.insert(0, e)
            prune_old_messages()
            
            # Save history periodically instead of on every message
            now = datetime.now(timezone.utc)
            if (now - last_save_time).total_seconds() >= HISTORY_SAVE_INTERVAL:
                await save_history_async()
                last_save_time = now
            
            await broadcast(json.dumps(e, ensure_ascii=False))
            
    except Exception as e:
        logger.error(f"Decoder error: {e}", exc_info=True)
    finally:
        if proc:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Decoder process did not terminate, killing")
                proc.kill()
                await proc.wait()
            except Exception as e:
                logger.error(f"Error terminating decoder: {e}")

# ================= HTTP =================

async def http_index(request: web.Request) -> web.Response:
    """Handle HTTP index page request."""
    html = page()
    # aiohttp requires charset to be separate from content_type
    return web.Response(text=html, content_type="text/html", charset="utf-8")

def page() -> str:
    """Generate HTML page with embedded JavaScript."""
    # Escape JSON to prevent XSS
    messages_json = json.dumps(messages, ensure_ascii=False).replace("</script>", "<\\/script>")
    return f"""
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P2000 Monitor - Live Emergency Alerts</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 50%, #0f1419 100%);
  color: #e6edf3;
  font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
  min-height: 100vh;
  padding: 20px;
  position: relative;
  overflow-x: hidden;
}}

body::before {{
  content: '';
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: 
    radial-gradient(circle at 20% 50%, rgba(255, 77, 77, 0.1) 0%, transparent 50%),
    radial-gradient(circle at 80% 80%, rgba(88, 166, 255, 0.1) 0%, transparent 50%),
    radial-gradient(circle at 40% 20%, rgba(192, 132, 252, 0.1) 0%, transparent 50%);
  pointer-events: none;
  z-index: 0;
}}

.container {{
  max-width: 1400px;
  margin: 0 auto;
  position: relative;
  z-index: 1;
}}

.header {{
  background: linear-gradient(135deg, rgba(37, 99, 235, 0.2) 0%, rgba(192, 132, 252, 0.2) 100%);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 20px;
  padding: 30px;
  margin-bottom: 30px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
  text-align: center;
}}

.header h1 {{
  font-size: 2.5em;
  background: linear-gradient(135deg, #ff4d4d 0%, #58a6ff 50%, #c084fc 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 15px;
  font-weight: 700;
  text-shadow: 0 0 30px rgba(88, 166, 255, 0.3);
}}

#clock {{
  font-size: 2.2em;
  font-weight: 700;
  font-family: 'Courier New', monospace;
  background: linear-gradient(135deg, #00ff88 0%, #00d4ff 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  text-shadow: 0 0 20px rgba(0, 255, 136, 0.5);
  letter-spacing: 2px;
  margin-top: 10px;
  display: inline-block;
  padding: 10px 20px;
  background-color: rgba(0, 255, 136, 0.1);
  border-radius: 10px;
  border: 2px solid rgba(0, 255, 136, 0.3);
}}

.clock-label {{
  font-size: 0.5em;
  color: #7ee787;
  text-transform: uppercase;
  letter-spacing: 3px;
  margin-bottom: 5px;
  opacity: 0.8;
}}

.filters {{
  display: flex;
  gap: 15px;
  margin-bottom: 25px;
  flex-wrap: wrap;
  justify-content: center;
}}

button {{
  padding: 12px 24px;
  border: none;
  border-radius: 12px;
  font-size: 1em;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.3s ease;
  background: rgba(255, 255, 255, 0.1);
  color: #e6edf3;
  border: 2px solid rgba(255, 255, 255, 0.2);
  backdrop-filter: blur(10px);
}}

button:hover {{
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3);
  background: rgba(255, 255, 255, 0.15);
}}

button.active {{
  background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
  color: white;
  border-color: rgba(255, 255, 255, 0.3);
  box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4);
}}

.table-wrapper {{
  background: rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 20px;
  padding: 20px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
  overflow-x: auto;
}}

table {{
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
}}

th {{
  background: linear-gradient(135deg, rgba(37, 99, 235, 0.3) 0%, rgba(192, 132, 252, 0.3) 100%);
  color: #fff;
  padding: 15px 12px;
  text-align: left;
  font-weight: 700;
  font-size: 0.95em;
  text-transform: uppercase;
  letter-spacing: 1px;
  border-bottom: 2px solid rgba(255, 255, 255, 0.2);
  position: sticky;
  top: 0;
  z-index: 10;
}}

th:first-child {{ border-top-left-radius: 10px; }}
th:last-child {{ border-top-right-radius: 10px; }}

td {{
  padding: 15px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  transition: all 0.2s ease;
}}

tr {{
  transition: all 0.2s ease;
}}

tr:hover {{
  background: rgba(255, 255, 255, 0.08);
  transform: scale(1.01);
}}

tr:last-child td:first-child {{ border-bottom-left-radius: 10px; }}
tr:last-child td:last-child {{ border-bottom-right-radius: 10px; }}

.dienst-brandweer {{
  color: #ff6b6b;
  font-weight: 600;
  text-shadow: 0 0 10px rgba(255, 107, 107, 0.5);
}}

.dienst-ambulance {{
  color: #4ecdc4;
  font-weight: 600;
  text-shadow: 0 0 10px rgba(78, 205, 196, 0.5);
}}

.dienst-politie {{
  color: #45b7d1;
  font-weight: 600;
  text-shadow: 0 0 10px rgba(69, 183, 209, 0.5);
}}

.dienst-trauma {{
  color: #f093fb;
  font-weight: 700;
  text-shadow: 0 0 15px rgba(240, 147, 251, 0.7);
  animation: pulse 2s ease-in-out infinite;
}}

@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.8; }}
}}

.dienst-onbekend {{
  color: #95a5a6;
  font-weight: 500;
}}

.prio {{
  font-weight: 700;
  text-align: center;
  font-size: 1.1em;
  padding: 8px 12px;
  border-radius: 8px;
  display: inline-block;
  min-width: 50px;
}}

.prio-A0, .prio-A1 {{
  background: linear-gradient(135deg, #ff6b6b 0%, #ee5a6f 100%);
  color: white;
  box-shadow: 0 4px 15px rgba(255, 107, 107, 0.4);
}}

.prio-A2, .prio-B1 {{
  background: linear-gradient(135deg, #feca57 0%, #ff9ff3 100%);
  color: white;
  box-shadow: 0 4px 15px rgba(254, 202, 87, 0.4);
}}

.prio-B2, .prio-P1 {{
  background: linear-gradient(135deg, #48dbfb 0%, #0abde3 100%);
  color: white;
  box-shadow: 0 4px 15px rgba(72, 219, 251, 0.4);
}}

.new-message {{
  animation: slideIn 0.5s ease-out;
  background: rgba(0, 255, 136, 0.1) !important;
  border-left: 4px solid #00ff88;
}}

@keyframes slideIn {{
  from {{
    opacity: 0;
    transform: translateX(-20px);
  }}
  to {{
    opacity: 1;
    transform: translateX(0);
  }}
}}

@media (max-width: 768px) {{
  .header h1 {{ font-size: 1.8em; }}
  #clock {{ font-size: 1.5em; }}
  th, td {{ padding: 10px 8px; font-size: 0.9em; }}
  .table-wrapper {{ padding: 10px; }}
}}
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <h1>📡 P2000 Monitor</h1>
    <div class="clock-label">Zulu Time (UTC)</div>
    <div id="clock"></div>
  </div>

  <div class="filters">
    <button id="btnAll" class="active">🌐 Alles</button>
    <button id="btnBeemster">📍 Beemster</button>
  </div>

  <div class="table-wrapper">
    <table id="tbl">
      <thead>
        <tr>
          <th>⏰ Tijd</th>
          <th>⚡ Prio</th>
          <th>📋 Capcodes</th>
          <th>📝 Type</th>
          <th>💬 Bericht</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
const table = document.getElementById("tbl");
const INITIAL = {messages_json};
let filter = "ALL";

function isRecent(utc) {{
  return (Date.now() - new Date(utc).getTime()) <= 5 * 60 * 1000;
}}

function matchFilter(m) {{
  if (filter === "ALL") return true;
  if (filter === "BEEMSTER") return m.text.toLowerCase().includes("beemster");
  return true;
}}

function renderRow(m) {{
  const r = document.createElement("tr");
  r.dataset.utc = m.time_utc;
  r.innerHTML =
    `<td>${{isRecent(m.time_utc) ? "🔔 " : ""}}${{m.time_local}}</td>
     <td class="prio">${{m.prio}}</td>
     <td>${{m.capcodes_named}}</td>
     <td>${{m.type}}</td>
     <td>${{m.text}}</td>`;
  return r;
}}

function rebuild() {{
  [...table.rows].slice(1).forEach(r => r.remove());
  INITIAL.filter(matchFilter).forEach(m => table.appendChild(renderRow(m)));
}}

btnAll.onclick = () => {{
  filter = "ALL";
  btnAll.classList.add("active");
  btnBeemster.classList.remove("active");
  rebuild();
}};
btnBeemster.onclick = () => {{
  filter = "BEEMSTER";
  btnBeemster.classList.add("active");
  btnAll.classList.remove("active");
  rebuild();
}};

INITIAL
  .slice()
  .sort((a,b)=>new Date(b.time_utc)-new Date(a.time_utc))
  .forEach(m=>table.appendChild(renderRow(m)));

const ws = new WebSocket("ws://" + location.hostname + ":{WS_PORT}");
ws.onmessage = e => {{
  const m = JSON.parse(e.data);
  INITIAL.unshift(m);
  if (matchFilter(m))
    table.insertBefore(renderRow(m), table.rows[1] || null);
}};

setInterval(() => {{
  [...table.rows].slice(1).forEach(row => {{
    const utc = row.dataset.utc;
    const cell = row.cells[0];
    const t = cell.textContent.replace("🔔 ","");
    cell.textContent = (isRecent(utc) ? "🔔 " : "") + t;
  }});
}}, 30000);

setInterval(() => {{
  clock.textContent = new Date().toISOString().replace("T"," ").substring(0,19) + " Z";
}}, 1000);
</script>

</body>
</html>
"""

async def http_server() -> None:
    """Start HTTP server."""
    app = web.Application()
    app.router.add_get("/", http_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "", HTTP_PORT)
    await site.start()
    logger.info(f"HTTP listening on port {HTTP_PORT}")
    await shutdown_event.wait()

# ================= SIGNAL HANDLING =================

def setup_signal_handlers() -> None:
    """Setup signal handlers for graceful shutdown."""
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

# ================= MAIN =================

async def main() -> None:
    """Main entry point."""
    setup_signal_handlers()
    
    try:
        load_capcodes()
        load_history()

        # Start all services
        await asyncio.gather(
            http_server(),
            ws_server(),
            start_decoder(),
            return_exceptions=True
        )
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        # Final save on shutdown
        logger.info("Saving history before shutdown...")
        await save_history_async()
        logger.info("Shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")

