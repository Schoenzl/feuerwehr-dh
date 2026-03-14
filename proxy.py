#!/usr/bin/env python3
"""
WASTL + LSZ Proxy – FF Deutsch Haslau
NÖ Bruck/Leitha (WASTL) + Bgld Neusiedl/See (LSZ HTML scraping)
"""
import json, time, threading, os, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from html.parser import HTMLParser

WASTL_URL = "https://www.feuerwehr-krems.at/ShowArtikelSpeed.asp?Artikel=5078&filter=03"
LSZ_URL   = "https://einsatz.lsz-b.at/"
REFRESH_INTERVAL = 60
PORT = int(os.environ.get("PORT", 10000))

# ── WASTL Parser ──────────────────────────────────────────
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

def fetch_wastl():
    req = Request(WASTL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        html = resp.read().decode("windows-1252", errors="replace")
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
    stand = ""
    if "Stand" in html:
        idx = html.find("Stand")
        stand = html[idx:idx+80].split("<")[0].strip()
    return einsaetze, stand

# ── LSZ Parser ────────────────────────────────────────────
class LSZParser(HTMLParser):
    """
    Parst die gerenderte LSZ Einsatzkarte.
    Struktur: Bezirks-Header → darunter Einsatz-Blöcke mit
    Alarmstufe, Feuerwehr, Ort, Zeit
    """
    def __init__(self):
        super().__init__()
        self.einsaetze = []
        self.aktueller_bezirk = ""
        self.stack = []          # tag stack
        self.text_buf = ""
        self.in_einsatz = False
        self.aktuell = {}
        # Rohe Textblöcke sammeln
        self.alle_texte = []
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        self.stack.append((tag, cls))
        self.text_buf = ""

    def handle_endtag(self, tag):
        text = self.text_buf.strip()
        if self.stack:
            top_tag, top_cls = self.stack[-1]
            if text:
                self.alle_texte.append((top_cls, text))
            self.stack.pop()
        self.text_buf = ""

    def handle_data(self, data):
        self.text_buf += data

def fetch_lsz():
    req = Request(LSZ_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-AT,de;q=0.9",
    })
    with urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Nur den "Laufend"-Bereich – alles VOR "Zuletzt aktualisiert"
    # Die Seite zeigt: Laufend → 12h → 24h
    # "Zuletzt aktualisiert" markiert Ende des Laufend-Blocks
    if "Zuletzt aktualisiert" in html:
        html = html[:html.index("Zuletzt aktualisiert")]

    # Rohen Text extrahieren
    clean = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL)
    tokens = re.findall(r'>([^<]+)<', clean)
    texte = [t.strip() for t in tokens if t.strip()]

    # Bekannte Bezirke
    bezirke = [
        "Neusiedl am See", "Neusiedl", "Eisenstadt", "Eisenstadt-Umgebung",
        "Rust", "Mattersburg", "Oberpullendorf", "Oberwart", "Güssing", "Jennersdorf"
    ]

    # Alarmstufen Muster
    alarmstufen = re.compile(r'^(B[0-4]|T[0-4]|F[0-4]|S[0-4]|G[0-4]|BMA|ÖBRD|VU|THL|MANV)\d*$', re.IGNORECASE)

    einsaetze = []
    aktueller_bezirk = ""
    i = 0

    while i < len(texte):
        t = texte[i]

        # Bezirks-Header erkennen
        for b in bezirke:
            if t == b or t.startswith(b):
                aktueller_bezirk = b
                break

        # Nur Neusiedl am See filtern
        if aktueller_bezirk not in ["Neusiedl am See", "Neusiedl"]:
            i += 1
            continue

        # Alarmstufe erkennen → Start eines Einsatzes
        if alarmstufen.match(t):
            alarmstufe = t
            feuerwehr = texte[i+1] if i+1 < len(texte) else ""
            ort = texte[i+2] if i+2 < len(texte) else ""
            zeit = texte[i+3] if i+3 < len(texte) else ""

            # Plausibilitätsprüfung: Feuerwehr beginnt meist mit "FW" oder enthält einen Ortsnamen
            if feuerwehr and ort:
                einsaetze.append({
                    "baz": "ND",
                    "ort": f"{ort} ({feuerwehr})",
                    "meldebild": alarmstufe,
                    "beginn": zeit
                })
                i += 4
                continue

        i += 1

    return einsaetze

# ── Cache ─────────────────────────────────────────────────
cache = {
    "einsaetze_bl": [],
    "einsaetze_nd": [],
    "stand_bl": "",
    "fehler_bl": None,
    "fehler_nd": None,
    "letzte_aktualisierung": ""
}

def fetch_loop():
    while True:
        # WASTL
        try:
            e_bl, stand = fetch_wastl()
            cache["einsaetze_bl"] = e_bl
            cache["stand_bl"] = stand
            cache["fehler_bl"] = None
            print(f"WASTL: {len(e_bl)} Einsätze", flush=True)
        except Exception as ex:
            cache["fehler_bl"] = str(ex)
            print(f"WASTL Fehler: {ex}", flush=True)

        # LSZ
        try:
            e_nd = fetch_lsz()
            cache["einsaetze_nd"] = e_nd
            cache["fehler_nd"] = None
            print(f"LSZ: {len(e_nd)} Einsätze (Neusiedl/See)", flush=True)
        except Exception as ex:
            cache["fehler_nd"] = str(ex)
            print(f"LSZ Fehler: {ex}", flush=True)

        cache["letzte_aktualisierung"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        gesamt = len(cache["einsaetze_bl"]) + len(cache["einsaetze_nd"])
        print(f"[{cache['letzte_aktualisierung']}] Gesamt: {gesamt}", flush=True)
        time.sleep(REFRESH_INTERVAL)

# ── HTTP Handler ──────────────────────────────────────────
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
    print(f"WASTL+LSZ Proxy startet auf Port {PORT}...", flush=True)
    threading.Thread(target=fetch_loop, daemon=True).start()
    time.sleep(4)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
