#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import threading
import http.server
import socketserver
import socket
import csv
import json
import argparse
import sys
import base64
import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ================= ARGUMENTS =================

parser = argparse.ArgumentParser(description="P2000 FLEX monitor")
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
clients = set()
capcodes = {}

# ================= HELPERS =================

def extract_prio(text):
    m = re.search(r"\b(A0|A1|A2|B1|B2|P\s*1|TEST)\b", text, re.I)
    return m.group(1).replace(" ", "").upper() if m else "-"

def capcode_candidates(raw):
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

def resolve_capcodes(raw):
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

def save_history():
    with open(DB_FILE, "w") as f:
        json.dump(messages, f, indent=2)

# ================= DECODER =================

def start_decoder():
    cmd = "rtl_fm -f 169.65M -M fm -s 22050 -p 83 -g 30 | multimon-ng -a FLEX -t raw -"
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, text=True)

    for l in p.stdout:
        if not l.startswith("FLEX|"):
            continue
        _, ts, *_ , caps, typ, txt = l.strip().split("|", 6)
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
        save_history()
        broadcast(json.dumps(e))

# ================= WEBSOCKET =================

def ws_accept(c):
    data = c.recv(2048).decode(errors="ignore")
    key = next((l.split(":")[1].strip() for l in data.splitlines()
                if l.lower().startswith("sec-websocket-key")), None)
    if not key:
        return False

    acc = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()

    c.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {acc}\r\n\r\n"
    ).encode())

    threading.Thread(target=ws_read_loop, args=(c,), daemon=True).start()
    return True

def ws_read_loop(c):
    try:
        while True:
            h = c.recv(2)
            if not h:
                break
            ln = h[1] & 0x7F
            if ln == 126:
                c.recv(2)
            elif ln == 127:
                c.recv(8)
            c.recv(4)
            c.recv(ln)
            if (h[0] & 0x0F) == 0x9:
                c.sendall(b"\x8A\x00")
    finally:
        clients.discard(c)
        c.close()

def ws_frame(m):
    b = m.encode()
    ln = len(b)
    h = b"\x81"
    if ln <= 125:
        h += bytes([ln])
    elif ln <= 65535:
        h += b"\x7e" + ln.to_bytes(2, "big")
    else:
        h += b"\x7f" + ln.to_bytes(8, "big")
    return h + b

def broadcast(m):
    f = ws_frame(m)
    for c in list(clients):
        try:
            c.sendall(f)
        except:
            clients.discard(c)

def ws_server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", WS_PORT))
    s.listen()
    while True:
        c, _ = s.accept()
        if ws_accept(c):
            clients.add(c)

# ================= HTTP =================

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page().encode())

def page():
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

# ================= MAIN =================

if __name__ == "__main__":
    load_capcodes()
    load_history()
    threading.Thread(target=start_decoder, daemon=True).start()
    threading.Thread(target=ws_server, daemon=True).start()
    socketserver.TCPServer.allow_reuse_address = True
    socketserver.TCPServer(("", HTTP_PORT), Handler).serve_forever()

