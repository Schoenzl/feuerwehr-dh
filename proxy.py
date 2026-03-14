#!/usr/bin/env python3
import json, time, threading, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from html.parser import HTMLParser

WASTL_URL = "https://www.feuerwehr-krems.at/ShowArtikelSpeed.asp?Artikel=5078&filter=03"
REFRESH_INTERVAL = 60
PORT = int(os.environ.get("PORT", 10000))

class WastlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.current_row = []
        self.current_cell = ""
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_row = True
            self.current_row = []
        if tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.current_row.append(self.current_cell.strip())
            self.in_cell = False
        if tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_row = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell += data.strip()

def parse_einsaetze(html):
    parser = WastlParser()
    parser.feed(html)
    einsaetze = []
    header_passed = False
    for row in parser.rows:
        if any("BAZ" in cell for cell in row):
            header_passed = True
            continue
        if not header_passed:
            continue
        cells = [c for c in row if c]
        if len(cells) >= 4:
            einsaetze.append({"baz": cells[0], "ort": cells[1], "meldebild": cells[2], "beginn": cells[3]})
        elif len(cells) == 3:
            einsaetze.append({"baz": cells[0], "ort": cells[1], "meldebild": cells[2], "beginn": ""})
    return einsaetze

cache = {"einsaetze": [], "stand": "", "fehler": None, "letzte_aktualisierung": ""}

def fetch_loop():
    while True:
        try:
            req = Request(WASTL_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as resp:
                html = resp.read().decode("windows-1252", errors="replace")
            einsaetze = parse_einsaetze(html)
            stand = ""
            if "Stand" in html:
                idx = html.find("Stand")
                stand = html[idx:idx+80].split("<")[0].strip()
            cache["einsaetze"] = einsaetze
            cache["stand"] = stand
            cache["fehler"] = None
            cache["letzte_aktualisierung"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"[{cache['letzte_aktualisierung']}] {len(einsaetze)} Einsaetze", flush=True)
        except Exception as e:
            cache["fehler"] = str(e)
            print(f"Fehler: {e}", flush=True)
        time.sleep(REFRESH_INTERVAL)

class Handler(BaseHTTPRequestHandler):
    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Origin, X-Requested-With")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_cors()
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(cache, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    print(f"WASTL Proxy startet auf Port {PORT}...", flush=True)
    threading.Thread(target=fetch_loop, daemon=True).start()
    time.sleep(3)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
