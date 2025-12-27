#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import subprocess
import csv
import json
import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiohttp import web
import websockets

# ================= ARGUMENTS =================

parser = argparse.ArgumentParser(description="P2000 FLEX monitor (asyncio)")
parser.add_argument("-v", "--verbose", action="store_true")
args = parser.parse_args()
VERBOSE = args.verbose

def log(*msg):
    if VERBOSE:
        print("[DEBUG]", *msg, flush=True)

# ================= CONFIG =================

HTTP_PORT = 8112
WS_PORT = 8113
RETENTION_DAYS = 3

BASE_DIR = Path(__file__).parent
CAPCODE_FILE = BASE_DIR / "capcodelijst.csv"
DB_FILE = BASE_DIR / "p2000_history.json"

messages = []
clients: set[websockets.WebSocketServerProtocol] = set()
capcodes = {}

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

def prune_old_messages():
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    messages[:] = [
        m for m in messages
        if datetime.fromisoformat(m["time_utc"]) >= cutoff
    ]

# ================= CAPCODES =================

def load_capcodes():
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
    log("Capcodes geladen:", len(capcodes))

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

def load_history():
    if not DB_FILE.exists():
        return
    with open(DB_FILE) as f:
        for m in json.load(f):
            m.setdefault("prio", extract_prio(m.get("text", "")))
            messages.append(m)
    prune_old_messages()

async def save_history_async():
    # avoid blocking event loop on file IO
    data = json.dumps(messages, indent=2)
    await asyncio.to_thread(DB_FILE.write_text, data)

# ================= WEBSOCKET =================

async def ws_handler(ws: websockets.WebSocketServerProtocol):
    clients.add(ws)
    try:
        # Keep connection open; browser doesn't send data.
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)

async def broadcast(msg: str):
    if not clients:
        return
    dead = []
    for ws in list(clients):
        try:
            await ws.send(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

async def ws_server():
    async with websockets.serve(ws_handler, "", WS_PORT):
        log("WebSocket listening on", WS_PORT)
        await asyncio.Future()  # run forever

# ================= DECODER =================

async def start_decoder():
    cmd = "rtl_fm -f 169.65M -M fm -s 22050 -p 83 -g 30 | multimon-ng -a FLEX -t raw -"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    log("Decoder started")

    while True:
        line = await proc.stdout.readline()
        if not line:
            log("Decoder process ended")
            break

        l = line.decode(errors="ignore").strip()
        if not l.startswith("FLEX|"):
            continue

        try:
            _, ts, *_, caps, typ, txt = l.split("|", 6)
        except ValueError:
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
        await save_history_async()
        await broadcast(json.dumps(e))

# ================= HTTP =================

async def http_index(request: web.Request):
    html = page()
    # aiohttp requires charset to be separate from content_type
    return web.Response(text=html, content_type="text/html", charset="utf-8")

def page():
    # HTML/JS copied exactly from your original (unchanged)
    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>P2000 Monitor</title>
<style>
body {{ background:#0d1117; color:#e6edf3; font-family: monospace; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border:1px solid #30363d; padding:4px; }}
button {{ margin:5px; padding:6px 12px; cursor:pointer; }}
button.active {{ background:#2563eb; color:white; }}

.dienst-brandweer {{ color:#ff4d4d; }}
.dienst-ambulance {{ color:#58a6ff; }}
.dienst-politie {{ color:#1f6feb; }}
.dienst-trauma {{ color:#c084fc; font-weight:bold; }}
.dienst-onbekend {{ color:#9ca3af; }}

.prio {{ font-weight:bold; text-align:center; }}

#clock {{ position:fixed; top:10px; right:20px; color:#7ee787; }}
</style>
</head>
<body>

<div id="clock"></div>

<div>
  <button id="btnAll" class="active">Alles</button>
  <button id="btnBeemster">Beemster</button>
</div>

<table id="tbl">
<tr><th>Tijd</th><th>Prio</th><th>Capcodes</th><th>Type</th><th>Bericht</th></tr>
</table>

<script>
const table = document.getElementById("tbl");
const INITIAL = {json.dumps(messages)};
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

async def http_server():
    app = web.Application()
    app.router.add_get("/", http_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "", HTTP_PORT)
    await site.start()
    log("HTTP listening on", HTTP_PORT)

# ================= MAIN =================

async def main():
    load_capcodes()
    load_history()

    await asyncio.gather(
        http_server(),
        ws_server(),
        start_decoder(),
    )

if __name__ == "__main__":
    asyncio.run(main())

