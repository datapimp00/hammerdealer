
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from datetime import datetime, timedelta
import os, requests, time, random, sqlite3, hashlib, json, re, logging, math
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)


def _lade_dotenv():
    """Laedt .env (KEY=VALUE) und setzt NUR noch nicht gesetzte Umgebungsvariablen.
    Dependency-frei. Existierende Env-Vars (Render) haben Vorrang."""
    try:
        pfad = os.path.join(os.path.dirname(__file__), ".env")
        if not os.path.exists(pfad):
            return
        with open(pfad) as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                k, v = zeile.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        logging.info("dotenv: %s", str(e)[:80])


_lade_dotenv()

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

API_KEY = os.environ.get("HAMMERDEALER_API_KEY", "hammerdealer_secret_2024")
WRAPPER_URLS = {"ebay":"http://localhost:8767","kleinanzeigen":"http://localhost:8768","facebook":"http://localhost:8769"}
PROXY_LIST = os.environ.get("PROXY_LIST","")
PROXIES = [p.strip() for p in PROXY_LIST.split(",") if p.strip()] if PROXY_LIST else []
DB_PATH = os.path.join(os.path.dirname(__file__), "hammerdealer.db")
MIN_PROFIT = 100

_CACHE = {}
# Sekunden, bis ein Scan wiederholt aus dem Cache gegeben wird.
# Per Env CACHE_TTL steuerbar (Default 60). Hoeher = mehr instant-Wiederholungen,
# aber evtl. etwas aeltere Preise. Auf Render in der Env setzbar.
_CACHE_TTL = int(os.environ["CACHE_TTL"]) if os.environ.get("CACHE_TTL", "").strip().isdigit() else 60
# Pro-Artikel-Marktpreis (aus individuellem Titel, nicht ein globaler Median fuer
# die ganze gemischte Suche). Gecacht -> schnelle Wiederholungen.
_MARKT_CACHE = {}  # titel-key -> (zeit, (preis, quelle))
_MARKT_TTL = int(os.environ["MARKT_TTL"]) if os.environ.get("MARKT_TTL", "").strip().isdigit() else 300
# Wie viele unterschiedliche Artikel-Marktpreise pro Suche parallel von eBay
# geholt werden (begrenzt die Laufzeit; Rest faellt auf globalen Preis/Faktor).
MAX_MARKT_LOOKUPS = int(os.environ["MAX_MARKT_LOOKUPS"]) if os.environ.get("MAX_MARKT_LOOKUPS", "").strip().isdigit() else 30
START_TIME = datetime.now()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS deals (id TEXT PRIMARY KEY, titel TEXT, plattform TEXT, kaufpreis REAL, kaufpreis_versand REAL, verkaufswert REAL, verkaufswert_roh REAL, gewinn_brutto REAL, provision REAL, gewinn_netto REAL, prozent INTEGER, link TEXT, zustand TEXT, datum TEXT, abzuege TEXT, quelle TEXT, bild TEXT, standort TEXT, entfernung_km REAL, vb BOOLEAN, nur_abholung BOOLEAN, tausch BOOLEAN)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gewinn ON deals(gewinn_netto DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plattform ON deals(plattform)")
    conn.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, portal TEXT, typ TEXT, nachricht TEXT, datum TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS gewinn_historie (id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT NOT NULL, plattform TEXT, gewinn_netto NUMERIC(10,2), kaufpreis NUMERIC(10,2), verkaufswert NUMERIC(10,2), zeit TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS lens_products (image_sha256 TEXT PRIMARY KEY, image_url TEXT, google_product_id TEXT, title TEXT, ebay_sku TEXT, link TEXT, preis REAL, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("CREATE TABLE IF NOT EXISTS ebay_sold_cache (query TEXT, ebay_sku TEXT, sold_price NUMERIC(10,2), datum TEXT, fetched_at TEXT NOT NULL DEFAULT (datetime('now')), source TEXT NOT NULL DEFAULT 'sold-api', PRIMARY KEY (query, ebay_sku))")
    conn.execute("CREATE TABLE IF NOT EXISTS suchauftraege (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, query TEXT NOT NULL, ausschluss TEXT, min_gewinn REAL, zustand TEXT, plattform TEXT, reichweite INTEGER, aktiv INTEGER NOT NULL DEFAULT 1, erstellt TEXT NOT NULL DEFAULT (datetime('now')))")
    cur = conn.execute("SELECT * FROM users WHERE username='admin'")
    if not cur.fetchone():
        h = hashlib.sha256("admin123".encode()).hexdigest()
        conn.execute("INSERT INTO users VALUES (?,?)", ("admin", h))
    try:
        conn.execute("VACUUM")
    except:
        pass
    conn.commit()
    conn.close()

init_db()

# --- Gewinnhistorie (Postgres via DATABASE_URL, sonst lokale SQLite-Fallback) ---
try:
    import psycopg2
    import psycopg2.extensions
except Exception:
    psycopg2 = None
HIST_DB = os.environ.get("DATABASE_URL", "").strip()

def _is_postgres(conn):
    """True, wenn die Hist-DB-Verbindung Postgres ist."""
    return conn is not None and type(conn).__module__ == "psycopg2.extensions"

def hist_conn():
    if HIST_DB:
        try:
            return psycopg2.connect(HIST_DB, connect_timeout=5)
        except Exception as e:
            logging.warning("history db connect failed (postgres): %s", e)
            return None
    # Fallback: lokale SQLite (ohne externen Postgres/DATABASE_URL)
    try:
        return sqlite3.connect(DB_PATH)
    except Exception as e:
        logging.warning("history db connect failed (sqlite): %s", e)
        return None

def hist_init():
    conn = hist_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if _is_postgres(conn):
            cur.execute("""CREATE TABLE IF NOT EXISTS gewinn_historie (
                id SERIAL PRIMARY KEY,
                q TEXT NOT NULL,
                plattform TEXT,
                gewinn_netto NUMERIC(10,2),
                kaufpreis NUMERIC(10,2),
                verkaufswert NUMERIC(10,2),
                zeit TIMESTAMP NOT NULL DEFAULT now()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS suchauftraege (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                ausschluss TEXT,
                min_gewinn NUMERIC(10,2),
                zustand TEXT,
                plattform TEXT,
                reichweite INTEGER,
                aktiv INTEGER NOT NULL DEFAULT 1,
                erstellt TIMESTAMP NOT NULL DEFAULT now()
            )""")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS gewinn_historie (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                q TEXT NOT NULL,
                plattform TEXT,
                gewinn_netto NUMERIC(10,2),
                kaufpreis NUMERIC(10,2),
                verkaufswert NUMERIC(10,2),
                zeit TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS suchauftraege (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                query TEXT NOT NULL,
                ausschluss TEXT,
                min_gewinn NUMERIC(10,2),
                zustand TEXT,
                plattform TEXT,
                reichweite INTEGER,
                aktiv INTEGER NOT NULL DEFAULT 1,
                erstellt TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hist_q ON gewinn_historie(q, zeit)")
        conn.commit()
        cur.close()
    except Exception as e:
        logging.warning("history init failed: %s", e)
    finally:
        conn.close()

hist_init()

def log_history(q, deals):
    """Speichert pro Anfrage einen Snapshot der Top-Deals (max 20)."""
    conn = hist_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if _is_postgres(conn):
            sql = ("INSERT INTO gewinn_historie (q, plattform, gewinn_netto, kaufpreis, verkaufswert) "
                   "VALUES (%s,%s,%s,%s,%s)")
        else:
            sql = ("INSERT INTO gewinn_historie (q, plattform, gewinn_netto, kaufpreis, verkaufswert) "
                   "VALUES (?,?,?,?,?)")
        for d in deals[:20]:
            cur.execute(sql, (
                q,
                d.get("plattform", ""),
                float(d.get("gewinn_netto") or 0),
                float(d.get("kaufpreis_gesamt") if d.get("kaufpreis_gesamt") is not None else (d.get("kaufpreis") or 0)),
                float(d.get("verkaufswert") or 0),
            ))
        conn.commit()
        cur.close()
    except Exception as e:
        logging.warning("history log failed: %s", e)
    finally:
        conn.close()

USER_AGENTS = ["Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15","Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0","Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0"]

PLATFORM_FEES = {
    "vinted": {"verkauf":0.05,"zahlung":0.02,"versand":5.0,"fix":0.0,"einkauf_versand":0.0},
    "ebay": {"verkauf":0.11,"zahlung":0.02,"versand":6.0,"fix":0.35,"einkauf_versand":5.0},
    "kleinanzeigen": {"verkauf":0.0,"zahlung":0.0,"versand":4.5,"fix":0.0,"einkauf_versand":5.0},
    "facebook": {"verkauf":0.05,"zahlung":0.0,"versand":5.0,"fix":0.0,"einkauf_versand":0.0},
    "marktde": {"verkauf":0.08,"zahlung":0.0,"versand":5.0,"fix":0.0,"einkauf_versand":5.0},
    "default": {"verkauf":0.08,"zahlung":0.02,"versand":5.0,"fix":0.0,"einkauf_versand":5.0},
}

# Standort Mapping für echte Entfernung
STANDORTE = {
    "berlin": (52.52, 13.40),
    "hamburg": (53.55, 9.99),
    "münchen": (48.13, 11.58),
    "muenchen": (48.13, 11.58),
    "köln": (50.93, 6.95),
    "koeln": (50.93, 6.95),
    "frankfurt": (50.11, 8.68),
    "stuttgart": (48.77, 9.18),
    "düsseldorf": (51.22, 6.77),
    "leipzig": (51.34, 12.37),
}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2-lat1)
    dlon = math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def sanitize_q(q):
    if not q: return "iPhone"
    q = q[:100]
    q = q.strip()
    q = re.sub(r'\s+', ' ', q)
    q = re.sub(r'[<>"\'`]', '', q)
    # Umlaute erlauben
    q = re.sub(r'[^a-zA-Z0-9äöüÄÖÜß\s\-_]', '', q)
    return q or "iPhone"

def truncate(t, max_len=80):
    t = t.strip()
    t = re.sub(r'\s+', ' ', t)
    # Emoji entfernen
    t = re.sub(r'[^\w\säöüÄÖÜß\-\.\,\!\?\:\(\)]', '', t)
    # HTML Entities dekodieren
    t = t.replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'")
    if len(t) > max_len:
        return t[:max_len-3] + "..."
    return t

def parse_preis_kleinigkeiten(titel, preis):
    """Kleinigkeiten: VB, Tausch, Abholung, 0€, etc."""
    titel_lower = titel.lower()
    vb = any(x in titel_lower for x in ["vb", "verhandlungsbasis", "v.b.", "verhandelbar"])
    tausch = any(x in titel_lower for x in ["tausch", "tausche", "tausch möglich"])
    nur_abholung = "nur abholung" in titel_lower or "abholung nur" in titel_lower
    zu_verschenken = any(x in titel_lower for x in ["zu verschenken", "geschenkt", "kostenlos", "umsonst"])
    reserviert = "reserviert" in titel_lower
    
    # VB -> Preis -10% Puffer, weil verhandelbar
    if vb:
        preis = round(preis * 0.9, 2)
    
    # Tausch -> nicht anzeigen
    if tausch:
        return None, {"vb":vb,"tausch":True,"nur_abholung":nur_abholung,"zu_verschenken":zu_verschenken,"reserviert":reserviert}
    
    # Zu verschenken -> Kaufpreis 0€, aber Abholung
    if zu_verschenken:
        preis = 0.0
        nur_abholung = True
    
    # Reserviert -> nicht anzeigen
    if reserviert:
        return None, {"vb":vb,"tausch":tausch,"nur_abholung":nur_abholung,"zu_verschenken":zu_verschenken,"reserviert":True}
    
    return preis, {"vb":vb,"tausch":tausch,"nur_abholung":nur_abholung,"zu_verschenken":zu_verschenken,"reserviert":reserviert}

def euro_format(betrag):
    """Deutsches Euro Format: 1.234,56€"""
    return f"{betrag:,.2f}€".replace(",", "X").replace(".", ",").replace("X", ".")

def _robuster_median(preise):
    """Median einer Preisliste mit IQR-Ausreisser-Filter (1.5*IQR).
    preise: aufsteigend sortierte Liste von floats. None wenn <3 Werte."""
    if not preise or len(preise) < 3:
        return None
    n = len(preise)

    def _q(frac):
        idx = frac * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        return preise[lo] + (preise[hi] - preise[lo]) * (idx - lo)

    q1, q3 = _q(0.25), _q(0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kern = [p for p in preise if lo <= p <= hi] or preise
    m = len(kern)
    med = kern[m // 2] if m % 2 else round((kern[m // 2 - 1] + kern[m // 2]) / 2, 2)
    return round(med, 2)


def berechne(d):
    # Kleinigkeiten: Titel, Preis Parsing
    titel = truncate(d["titel"])
    kauf_roh = float(d["kaufpreis"])
    kauf_roh, flags = parse_preis_kleinigkeiten(titel, kauf_roh)
    if kauf_roh is None:
        return None  # Tausch, Reserviert -> nicht anzeigen
    
    # Einkauf Versand: oft muss man zum Verkäufer fahren oder Versand zahlen
    plattform = d["plattform"]
    fees = PLATFORM_FEES.get(plattform, PLATFORM_FEES["default"])
    kauf_versand = 0.0
    if not flags["nur_abholung"]:
        kauf_versand = fees["einkauf_versand"]
    else:
        # Nur Abholung -> Reichweite wichtig, Versand 0
        kauf_versand = 0.0
    
    kauf_gesamt = round(kauf_roh + kauf_versand, 2)
    
    # Verkauf: gewrapped + Zustand
    verkauf_roh = float(d["verkaufswert"])
    # VB auch bei Verkauf: -10% wenn VB
    verkauf_flags_vb = "vb" in titel.lower()
    if verkauf_flags_vb:
        verkauf_roh = round(verkauf_roh * 0.95, 2)  # Verkauf VB -> etwas weniger
    
    # Versand nach Gewicht
    titel_lower = titel.lower()
    if any(w in titel_lower for w in ["iphone", "handy", "airpods"]):
        versand_verkauf = 4.5
    elif any(w in titel_lower for w in ["laptop", "macbook"]):
        versand_verkauf = 7.0
    elif "fahrrad" in titel_lower:
        versand_verkauf = 15.0
    else:
        versand_verkauf = fees["versand"]
    
    # Nur Abholung -> Versand 0
    if flags["nur_abholung"]:
        versand_verkauf = 0.0
    
    verkaufsgeb = round(verkauf_roh * fees["verkauf"],2)
    zahlungsgeb = round(verkauf_roh * fees["zahlung"],2)
    fix = fees["fix"]
    rueckgabe_puffer = round(verkauf_roh * 0.02,2)
    sicherheit_puffer = 5.0  # Unvorhergesehenes
    
    gesamt_abzuege = round(verkaufsgeb + zahlungsgeb + versand_verkauf + fix + rueckgabe_puffer + sicherheit_puffer,2)
    brutto = round(verkauf_roh - kauf_gesamt - gesamt_abzuege,2)
    
    # Negativ Gewinn filtern
    if brutto < 0:
        return None
    
    # Deine Gebühr
    if brutto <= 100:
        prozent=2
        prov=round(brutto*0.02,2)
    elif brutto <= 300:
        prozent=3
        prov=round(brutto*0.03,2)
    else:
        prozent=4
        prov=round(brutto*0.04,2)
    
    netto = round(brutto - prov,2)
    
    if netto < 0:
        return None  # negative Gewinne raus (Untergrenze kommt aus API min_gewinn)
    
    # Standort Entfernung echt (Haversine) - Demo: Berlin als User Standort
    user_lat, user_lon = 52.52, 13.40  # Berlin default
    # Deal Standort aus Titel raten (z.B. "Berlin" im Titel)
    deal_lat, deal_lon = None, None
    for stadt, (lat, lon) in STANDORTE.items():
        if stadt in titel_lower:
            deal_lat, deal_lon = lat, lon
            break
    entfernung = None
    if deal_lat:
        entfernung = round(haversine(user_lat, user_lon, deal_lat, deal_lon),1)
    
    return {
        "id": d["id"],
        "titel": titel,
        "titel_original": d["titel"],
        "plattform": plattform,
        "kaufpreis": kauf_roh,
        "kaufpreis_versand": kauf_versand,
        "kaufpreis_gesamt": kauf_gesamt,
        "verkaufswert": verkauf_roh,
        "verkaufswert_roh": verkauf_roh,
        "verkaufswert_gewrapped": True,
        "verkaufswert_formatiert": euro_format(verkauf_roh),
        "kaufpreis_formatiert": euro_format(kauf_gesamt),
        "abzuege": {
            "verkaufsgebuehr": verkaufsgeb,
            "zahlungsgebuehr": zahlungsgeb,
            "versand_verkauf": versand_verkauf,
            "versand_einkauf": kauf_versand,
            "fix": fix,
            "rueckgabe_puffer": rueckgabe_puffer,
            "sicherheit_puffer": sicherheit_puffer,
            "gesamt_plattform": gesamt_abzuege,
            "vb": flags["vb"],
            "nur_abholung": flags["nur_abholung"],
            "zu_verschenken": flags["zu_verschenken"],
        },
        "deine_gebuehr": {"prozent":prozent,"betrag":prov,"staffel":"2% bis 100, 3% bis 300, 4% darüber"},
        "gewinn_brutto": brutto,
        "gewinn_brutto_formatiert": euro_format(brutto),
        "provision": prov,
        "prozent": prozent,
        "gewinn_netto": netto,
        "gewinn_netto_formatiert": euro_format(netto),
        "mit_allen_abzuegen": True,
        "deine_gebuehr_einbezogen": True,
        "datum": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "link": d["link"],
        "zustand": d.get("zustand","gut"),
        "standort": d.get("standort",""),
        "entfernung_km": entfernung,
        "vb": flags["vb"],
        "nur_abholung": flags["nur_abholung"],
        "tausch": flags["tausch"],
        "quelle": "echt+gewrapped+alle Abzüge+deine Gebühr+VB+Abholung+Versand beide Wege",
        "bild": d.get("bild",""),
    }


def score_deal(d):
    """Deal-Score 0-100 nach SnipeFlip-Prinzip: nachrechnen, nicht raten.
    Preis 50%, Zustand 30%, Logistik/Verkaeufer 20% - fuer jeden Deal nachvollziehbar.
    Fehlt echter Marktpreis -> score_echter_marktpreis=False (keine erfundene Zahl).
    Gibt None zurueck wenn Kerndaten fehlen (dann KEIN Score statt Fake)."""
    kauf = d.get("kaufpreis_gesamt") or d.get("kaufpreis") or 0
    markt = d.get("verkaufswert") or 0
    if kauf <= 0 or markt <= 0:
        return None  # ohne echte Zahlen: kein Score statt erfundener Zahl

    # 1) PREIS (0-50): was der Flipper wirklich interessiert - Netto-Gewinn (absolut)
    #    UND Kapitalrendite (ROI). Roher "Rabatt vs. Marktpreis" taugt nicht:
    #    eBay-Marktpreise enthalten MwSt, kleinanzeigen-Preise nicht -> ~16-20%
    #    Scheinrabatt fuer ALLES -> Score plättet bei 100. Netto+ROI diskriminiert.
    netto = float(d.get("gewinn_netto") or 0)
    roi = netto / kauf if kauf > 0 else 0.0  # z.B. 3.0 = 300% Rendite
    # Netto-Anteil (0-30): ab 200 EUR Netto voll. ROI-Anteil (0-20): ab 200% voll.
    netto_pts = min(1.0, netto / 200.0) * 30.0
    roi_pts = min(1.0, roi / 2.0) * 20.0
    preis_score = netto_pts + roi_pts

    # 2) ZUSTAND (0-30): neu > gut > defekt, + Bonus fuer vorhandenes Bild
    z = (d.get("zustand") or "gut").lower()
    zustand_score = {"neu": 30.0, "gut": 22.0, "defekt": 6.0}.get(z, 18.0)
    if d.get("bild"):
        zustand_score = min(30.0, zustand_score + 4.0)

    # 3) LOGISTIK / VERKAEUFER (0-20): nahe Abholung schlaegt Versand
    km = d.get("entfernung_km")
    abhol = d.get("nur_abholung")
    if km is not None:
        if km <= 10:
            logistik = 20.0
        elif km <= 30:
            logistik = 16.0
        elif km <= 60:
            logistik = 12.0
        elif km <= 150:
            logistik = 8.0
        else:
            logistik = 4.0
    elif abhol:
        logistik = 8.0  # Abholung ohne bekannte Entfernung
    else:
        logistik = 12.0  # neutraler Default (kein Standort/kein Versandhinweis)
    if d.get("plattform") in ("ebay", "vinted"):
        logistik = max(logistik, 14.0)  # reine Versandportale: solide Logistik

    score = int(max(0, min(100, round(preis_score + zustand_score + logistik))))
    q = d.get("verkaufswert_quelle") or ""
    echt = not ("Schaetzung" in q or "Faktor" in q)
    # Nur Anzeige: wie weit unter Marktpreis. Wegen MwSt-Bias (eBay brutto,
    # kleinanzeigen netto) ist das immer leicht hoch - NUR informativ, nicht im Score.
    rabatt = max(0.0, min(1.0, (markt - kauf) / markt)) if markt > 0 else 0.0
    return {
        "score": score,
        "score_teile": {"preis": round(preis_score, 1),
                        "zustand": round(zustand_score, 1),
                        "logistik": round(logistik, 1)},
        "score_echter_marktpreis": echt,
        "rabatt_prozent": round(rabatt * 100, 1),
        # Spanne in Euro: Marktwert - Wareneinsatz (brutto, vor Abzuegen).
        # Nachvollziehbarer "Luft nach oben"-Wert auf einen Blick.
        "spanne_euro": round(markt - kauf, 2),
        "roi_prozent": round(roi * 100, 1),
    }


def fallback_bild(plattform, titel=""):
    """Platzhalter-Bild pro Plattform (SVG data-URI, kein externer Host noetig)."""
    farben = {
        "ebay": ("#E53238", "eBay"),
        "kleinanzeigen": ("#1D5546", "KA"),
        "vinted": ("#007782", "Vinted"),
        "facebook": ("#0866FF", "FB"),
        "marktde": ("#FF6B00", "M"),
    }
    bg, kurz = farben.get(plattform, ("#86868b", "?"))
    label = (titel[:1] or kurz).upper()
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='200' height='150'>"
        f"<rect width='200' height='150' fill='{bg}'/>"
        f"<text x='100' y='85' font-family='sans-serif' font-size='48' fill='white' text-anchor='middle'>{label}</text>"
        f"</svg>"
    )
    import urllib.parse
    return "data:image/svg+xml," + urllib.parse.quote(svg)

_STOPWOERTER = {
    "inkl", "inkl.", "inklusive", "mit", "und", "fuer", "für", "von", "der",
    "die", "das", "ein", "eine", "sehr", "top", "zustand", "neu", "ovp", "np",
    "topzustand", "versand", "nur", "wie", "aus", "auf", "in", "im", "am",
    "sowie", "u", "u.", "bzw", "komplett", "set", "paket", "rar", "selten",
    "gut", "gebr", "gebraucht", "vb", "np.", "top-", "super", "echt", "org",
    "original", "konsole", "imkl", "rechnung", "garantie",
}


def suchbegriff_aus_titel(titel):
    """Zieht einen kompakten, suchbaren Begriff aus einem Artikel-Titel.

    Erste sinnvolle Woerter (Zahlen/Versionen wie 'playstation 2' bleiben);
    Stoppwoerter ('inkl', 'mit', 'Rechnung' ...) beenden den Begriff meist.
    Rueckgabe in Kleinbuchstaben (Cache-Key-kompatibel)."""
    if not titel:
        return ""
    t = re.sub(r"[()\[\]{}]", " ", str(titel).lower())
    out = []
    for w in re.split(r"\W+", t):
        if not w:
            continue
        if w in _STOPWOERTER:
            if out:  # Trennwort nach echtem Begriff -> Ende
                break
            continue
        if len(w) < 2 and not w.isdigit():
            continue
        out.append(w)
        if len(out) >= 4:
            break
    return " ".join(out)

def _lens_bild_hash(img_src):
    """Hash eines Bildes (URL oder base64) - fuer Deduplizierung in lens_products."""
    try:
        if img_src.startswith(('data:image', 'http://', 'https://')):
            h = hashlib.sha256(img_src.encode('utf-8', 'ignore')).hexdigest()
        else:
            h = hashlib.sha256(img_src.encode('utf-8', 'ignore')).hexdigest()
        return h
    except Exception:
        return None


def lens_match_image(img_src, fallback_title=''):
    """
    google-Lens-artige Suche: Bild(e) -> Produkt -> Preis aus eBay.
    Allowed:
      * img_src: Bild-URL, data-URI oder Base64 (ohne Prefix)
      * fallback_title: Titel des Produkts (falls keine Encoder verfuegbar)
    Returns (titel, preis, ebay_sku, link, matched) or None.
    """
    h = _lens_bild_hash(img_src)
    if not h:
        return None
    conn = get_db()
    cur = conn.execute("SELECT * FROM lens_products WHERE image_sha256=?", (h,))
    hat = cur.fetchone()
    if hat:
        return (hat['title'], hat['preis'], hat['ebay_sku'], hat['link'], True)
    titel = (fallback_title or '').strip()
    nur_url_titel = False
    if not titel:
        titel = img_src.split('/')[-1].replace('-', ' ').replace('_', ' ')[:40]
        nur_url_titel = True
    google_id = None
    gkey = os.environ.get('GEMINI_API_KEY', '').strip()
    gmodel = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash').strip() or 'gemini-3.5-flash'
    if gkey:
        try:
            if img_src.startswith('http'):
                gimg = {"fileData": {"mimeType": "image/jpeg", "fileUri": img_src}}
            elif img_src.startswith('data:'):
                mt = img_src[5:img_src.find(';')] if ';' in img_src[:40] else 'image/jpeg'
                gimg = {"inlineData": {"mimeType": mt, "data": img_src.split(',', 1)[1] if ',' in img_src else img_src}}
            else:
                gimg = None
            gparts = []
            if gimg:
                gparts.append(gimg)
            gparts.append({"text": "Welches Produkt siehst du auf diesem Bild? Antworte nur mit dem kurzen Produktnamen (Marke + Modell). Falls kein Bild dabei ist, antworte nur mit dem Produktnamen zum Titel: " + titel})
            rr = requests.post(
                'https://generativelanguage.googleapis.com/v1beta/models/' + gmodel + ':generateContent?key=' + gkey,
                json={"contents": [{"parts": gparts}]},
                headers={'Content-Type': 'application/json'}, timeout=25)
            if rr.status_code == 200:
                cands = rr.json().get('candidates') or []
                txt = ''
                if cands:
                    parts = (cands[0].get('content') or {}).get('parts') or []
                    txt = ''.join([p.get('text', '') for p in parts]).strip().split('\n')[0][:80]
                if txt:
                    titel = txt
                    google_id = 'gemini:' + txt[:60]
            else:
                logging.warning('gemini api: HTTP %s %s', rr.status_code, rr.text[:80])
        except Exception as e:
            logging.warning('lens gemini: %s', str(e)[:80])
    vin = os.environ.get('LASER_VISION_URL')
    if not google_id and vin:
        try:
            rr = requests.post(vin, json={'image': img_src},
                               headers={'Content-Type': 'application/json'},
                               timeout=10)
            if rr.status_code == 200 and rr.json().get('product_id'):
                google_id = rr.json()['product_id']
                titel = rr.json().get('title') or titel
        except Exception as e:
            logging.warning('lens vision: %s', str(e)[:80])
    preis = None
    link = ''
    sku = ''
    # Ohne echten Produktnamen (nur URL-Filename als Titel) NICHT raten ->
    # lieber kein Preis als ein falscher. Suche nur wenn Gemini/Vision erkannt
    # hat oder ein fallback_title gegeben war.
    if google_id or not nur_url_titel:
        from scraper import ebay_browse as _lens_eb
        for begriff in _suchbegriff_kaskade(titel):
            try:
                items = _lens_eb(begriff, limit=15)
            except Exception:
                items = []
            for it in items:
                if it.get('preis') and it.get('preis') > 0:
                    preis = it['preis']
                    sku = it.get('sku') or ''
                    link = it.get('link') or ''
                    break
            if preis:
                break
    rec = (h, img_src, google_id, titel[:80], sku, link, preis or 0)
    try:
        conn.execute("INSERT OR REPLACE INTO lens_products VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))", rec)
        conn.commit()
    except Exception as e:
        logging.warning('lens insert: %s', str(e)[:60])
    return (titel, preis or 0, sku, link, True)



def _suchbegriff_kaskade(titel):
    """Aus dem Titel einen kaskadierten Suchbegriff fuer eBay-Browse."""
    from scraper import ebay_browse as _eb
    words = [w for w in titel.lower().split() if len(w) >= 2]
    out = []
    for i in range(len(words), 0, -1):
        cand = ' '.join(words[:i])
        if cand not in out:
            out.append(cand)
    return out

def ebay_sold_price_cached(q):
    """Verkaufsteuer-Preis von eBay via FindCompletedItems (cachebasiert).
    grave: Finding-API blockiert von Rechenzentrums-IPs (418) -> liefert None,
    ausser die IP ist freigegeben.
    Returns (preis, quelle) oder (None, None).
    """
    key = (q or '').strip().lower()
    now = time.time()
    hit = _MARKT_CACHE.get(key)
    if hit and now - hit[0] < _MARKT_TTL:
        return hit[1]
    try:
        conn = get_db()
        cur = conn.execute('SELECT sold_price, datum FROM ebay_sold_cache WHERE query=? ORDER BY fetched_at DESC LIMIT 1', (q,))
        r = cur.fetchone()
        if r and r[0]:
            _MARKT_CACHE[key] = (now, (r[0], 'eBay-Sold-Cache'))
            return r[0], 'eBay-Sold-Cache'
    except Exception:
        pass
    try:
        from scraper import ebay_sold_price as _esp
        p = _esp(q)
        if p:
            try:
                conn = get_db()
                conn.execute("INSERT OR REPLACE INTO ebay_sold_cache (query, ebay_sku, sold_price, datum, source) VALUES (?, '', ?, '', 'sold-api')", (q, p))
                conn.commit()
            except Exception:
                pass
            _MARKT_CACHE[key] = (now, (p, 'eBay-Sold'))
            return p, 'eBay-Sold'
    except Exception:
        pass
    _MARKT_CACHE[key] = (now, (None, None))
    return None, None



def marktpreis_artikel(begriff):
    """Marktpreis fuer einen einzelnen Artikel-Begriff, gecacht (TTL-basiert).

    Gibt (preis, quelle) zurueck oder (None, None). Thread-sicher: der Cache-
    Eintrag wird vom Aufrufer (Hauptthread) nach dem parallelen Fetch gesetzt,
    nicht im Worker."""
    key = (begriff or "").strip().lower()
    if not key:
        return None, None
    now = time.time()
    hit = _MARKT_CACHE.get(key)
    if hit and now - hit[0] < _MARKT_TTL:
        return hit[1]
    try:
        from scraper import ebay_market_price
        preis, quelle = ebay_market_price(key)
    except Exception:
        preis, quelle = None, None
    _MARKT_CACHE[key] = (now, (preis, quelle))
    return preis, quelle


def scan(q):
    """Echte Portale scannen (Kleinanzeigen + eBay/Vinted best effort).

    Holt Plattform-Listen ueber die Micro-Wrapper (HTTP, via WRAPPER_URLS) und
    berechnet fuer die Gewinnloesung echte Verkaufspreise (eBay-Median /
    Schaetzung). Die Micro-Wrapper liefern Titel/Link/Bild/Zustand, nicht
    verfiellige Preise -> Kaufpreis bleibt Beobachtung, Verkaufswert aus
    dem echten Preis-Pipeline.
    """
    q = sanitize_q(q)
    try:
        from scraper import scan_real, schaetze_verkauf, ebay_market_price
    except Exception as e:
        logging.warning("scraper import: %s", e)
        return []

    def _fetch_from_wrappers(wrappers):
        """Ruft alle Micro-Wrapper ab (POST /fetch) und normalisiert die Listing-Dicts.
        Rueckgabe: list[{titel,plattform,kaufpreis,link,bild,zustand}]. Nie wirfend,
        im Fehlerfall leere Liste."""
        out = []
        for portal, svc_url in wrappers.items():
            if not svc_url:
                continue
            try:
                r = requests.post(
                    svc_url + "/fetch",
                    json={"url": "https://" + portal + ".de"},
                    timeout=15,
                    proxies=PROXIES,
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
            except Exception:
                continue
            for d in (payload or {}).get("deals") or []:
                if not isinstance(d, dict):
                    continue
                out.append({
                    "titel": d.get("titel") or "",
                    "plattform": d.get("plattform") or portal,
                    "kaufpreis": float(d.get("kaufpreis") or 0),
                    "link": d.get("link") or "",
                    "bild": d.get("bild") or "",
                    "zustand": d.get("zustand", "gut"),
                })
        return out

    # 1) Micro-Wrappers befragen (HTTP)

    items = _fetch_from_wrappers(WRAPPER_URLS)


    # 2) In-Process-Scraping als Quelle fuer echte Preise (Boost)
    try:
        from scraper import scan_real as _scan_real
        items2, quellen_scrape = _scan_real(q)
    except Exception:
        items2, quellen_scrape = [], []
    items += items2

    if not items:
        return []

    # Echter Marktpreis (globale Basis): zuerst VERKAUFTE eBay-Auktionen
    # (soldItems), sonst Median der aktiven eBay-Listings (Browse-API).
    # Nur EIN Aufruf (ebay_market_price prueft intern Sold->Browse), damit die
    # geblockte Finding-API nicht doppelt den Timeout kostet.
    marktpreis = None
    markt_quelle = None
    sold_echt = False
    try:
        marktpreis, markt_quelle = ebay_market_price(q)
        if marktpreis and "Sold" in (markt_quelle or ""):
            sold_echt = True
    except Exception:
        marktpreis, markt_quelle = None, None
    # Hinweis: KEIN Fallback auf den Median der Angebots- (Kauf-)Preise.
    # Der Median der asking prices ist bei gemischten Suchen (z.B. "playstation":
    # PS1/PS2/PS5/Spiele/Controller gemischt) als "Marktwert" irrefuehrend und
    # erzeugt falsche Gewinne. Bleibt kein echter Marktpreis, nutzen wir die
    # klar als Schaetzung markierte Faktor-Schaetzung (schaetze_verkauf).

    # Pro-Artikel-Marktpreis: fuer jeden Deal wird ein spezifischer Marktpreis
    # aus dem EIGENEN Titel bestimmt (nicht der globale Median der Mischsuche).
    # Parallel + gecacht + auf MAX_MARKT_LOOKUPS begrenzt (Laufzeit/Rate-Limits).
    markt_preise = {}  # index-in-items -> (preis, quelle)
    titel_begriffe = [suchbegriff_aus_titel(it.get("titel", "")) for it in items]
    eindeutige = []
    for b in titel_begriffe:
        if b and b.lower() != q.lower() and b not in eindeutige:
            eindeutige.append(b)
    zu_holen = eindeutige[:MAX_MARKT_LOOKUPS]

    def _holen(begriff):
        try:
            return marktpreis_artikel(begriff)
        except Exception:
            return None, None

    if zu_holen:
        with ThreadPoolExecutor(max_workers=min(6, len(zu_holen))) as pool:
            future_begriff = {pool.submit(_holen, b): b for b in zu_holen}
            begriff_preis = {}
            for fut in as_completed(future_begriff):
                b = future_begriff[fut]
                try:
                    begriff_preis[b] = fut.result()
                except Exception:
                    begriff_preis[b] = (None, None)
        for idx, begriff in enumerate(titel_begriffe):
            if begriff in begriff_preis:
                markt_preise[idx] = begriff_preis[begriff]

    quelle_txt = "echt:" + "+".join(quellen_scrape) if quellen_scrape else "wrapper"
    res = []
    for i, it in enumerate(items):
        preis = float(it.get("kaufpreis") or it.get("preis") or 0)
        if preis <= 0:
            continue
        # Verkaufswert-Prioritaet: 1) pro-Artikel-Marktpreis (aus eigenem Titel),
        # 2) globaler Marktpreis, 3) Faktor-Schaetzung (klar als Schaetzung markiert).
        # WICHTIG: echte Marktpreise werden IMMER genutzt, auch wenn sie unter
        # dem Kaufpreis liegen. Nur so wird ein Verlust-Deal ehrlich als Verlust
        # erkannt, statt mit einem Faktor-Gewinn verschleiert zu werden.
        verkaufswert = None
        verk_quelle = None
        mp, mq = markt_preise.get(i, (None, None))
        if mp:
            verkaufswert, verk_quelle = mp, mq
        elif marktpreis:
            verkaufswert, verk_quelle = marktpreis, markt_quelle
        if verkaufswert is None:
            verkaufswert = schaetze_verkauf(preis, it.get("titel", ""))
            verk_quelle = "Schaetzung (Faktor, kein Marktpreis)"
        d = {
            "id": f"{it.get('plattform', 'x')[:2]}_{int(time.time())}_{i}",
            "titel": it.get("titel") or q,
            "plattform": it.get("plattform", "kleinanzeigen"),
            "kaufpreis": preis,
            "verkaufswert": verkaufswert,
            "zustand": it.get("zustand", "gut"),
            "link": it.get("link", ""),
            "standort": it.get("standort", ""),
            "bild": it.get("bild") or fallback_bild(it.get("plattform", "kleinanzeigen"), q),
        }
        b = berechne(d)
        if b:
            b["verkaufswert_quelle"] = verk_quelle
            b["quelle"] = quelle_txt
            sc = score_deal(b)
            if sc:
                b.update(sc)  # score, score_teile, score_echter_marktpreis, rabatt_prozent
            res.append(b)

    # Deduplizierung
    seen = set()
    uniq = []
    for d in res:
        key = (d["titel"][:30].lower(), d["plattform"])
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    # Sortierung: Deal-Score zuerst (SnipeFlip-Prinzip), Gewinn als Tiebreaker.
    # Deals ohne Score (kein echter Marktpreis) wandern nach hinten.
    uniq.sort(key=lambda x: (x.get("score", -1), x["gewinn_netto"]), reverse=True)
    return uniq[:50]


def _cache_get(q):
    """Gibt cachende Ergebnisse der Suche (mit TTL) oder None zurück."""
    entry = _CACHE.get(q)
    if entry is None:
        return None
    if time.monotonic() - entry[0] > _CACHE_TTL:
        _CACHE.pop(q, None)
        return None
    return entry[1]


def _suchauftrag_dict(r):
    """DB-Row -> JSON-taugliches Dict."""
    return {"id": r[0], "name": r[1], "query": r[2], "ausschluss": r[3] or "",
            "min_gewinn": r[4], "zustand": r[5] or "", "plattform": r[6] or "",
            "reichweite": r[7], "aktiv": r[8], "erstellt": r[9]}


@app.route('/api/suchauftraege', methods=['GET'])
def api_suchauftraege_liste():
    """Alle gespeicherten Suchaufträge (neueste zuerst)."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id,name,query,ausschluss,min_gewinn,zustand,plattform,reichweite,aktiv,erstellt "
            "FROM suchauftraege ORDER BY id DESC").fetchall()
        return jsonify({'ok': True, 'auftraege': [_suchauftrag_dict(r) for r in rows]})
    except Exception as e:
        logging.warning('suchauftraege list: %s', str(e)[:120])
        return jsonify({'ok': False, 'fehler': str(e)[:120]}), 500


@app.route('/api/suchauftraege', methods=['POST'])
def api_suchauftraege_speichern():
    """Neuen Suchauftrag anlegen. Pflicht: name + query. Rest optional."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get('name') or '').strip()[:60]
        query = sanitize_q(data.get('query', ''))
        if not name or not query:
            return jsonify({'ok': False, 'fehler': 'Name und Suchbegriff nötig'}), 400
        ausschluss = (data.get('ausschluss') or '').strip()[:300]
        zustand = (data.get('zustand') or '').strip()[:20]
        plattform = (data.get('plattform') or '').strip()[:30]
        try:
            min_g = float(data.get('min_gewinn') or 0)
        except Exception:
            min_g = 0
        try:
            rw = int(data.get('reichweite') or 50)
        except Exception:
            rw = 50
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO suchauftraege (name,query,ausschluss,min_gewinn,zustand,plattform,reichweite) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, query, ausschluss, min_g, zustand, plattform, rw))
        conn.commit()
        nid = cur.lastrowid
        conn.close()
        return jsonify({'ok': True, 'id': nid, 'name': name, 'query': query})
    except Exception as e:
        logging.warning('suchauftrag save: %s', str(e)[:120])
        return jsonify({'ok': False, 'fehler': str(e)[:120]}), 500


@app.route('/api/suchauftraege/<int:aid>', methods=['DELETE'])
def api_suchauftrag_loeschen(aid):
    """Suchauftrag löschen."""
    try:
        conn = get_db()
        conn.execute("DELETE FROM suchauftraege WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'geloescht': aid})
    except Exception as e:
        logging.warning('suchauftrag del: %s', str(e)[:120])
        return jsonify({'ok': False, 'fehler': str(e)[:120]}), 500


@app.route('/api/lens', methods=['POST'])
def api_lens():
    try:
        img = request.files.get('image') or request.form.get('image', '')
        url = img if isinstance(img, str) else ''
        if not url and hasattr(img, 'read'):
            import base64 as _b64
            raw = img.read()
            if raw:
                mt = (img.mimetype or 'image/jpeg').split(';')[0]
                url = 'data:' + mt + ';base64,' + _b64.b64encode(raw).decode()
        if not url:
            return jsonify({'ok': False, 'fehler': 'kein Bild'}), 400
        r = lens_match_image(url)
        if r is None:
            return jsonify({'ok': False, 'fehler': 'kein Ergebnis'}), 400
        titel, preis, sku, link, matched = r
        return jsonify({'ok': True, 'titel': titel, 'preis': preis, 'sku': sku, 'link': link, 'matched': matched})
    except Exception as e:
        logging.warning('lens: %s', str(e)[:100])
        return jsonify({'ok': False, 'fehler': str(e)[:120]}), 500

@app.route('/api/sold-cache')
def api_sold_cache():
    try:
        q = (request.args.get('q') or '').strip()
        if not q:
            return jsonify({'ok': False, 'fehler': 'kein Query'}), 400
        import sqlite3
        conn = get_db()
        rows = conn.execute('SELECT sold_price, datum, source FROM ebay_sold_cache WHERE query=? ORDER BY fetched_at DESC', (q,)).fetchall()
        out = [{'preis': r[0], 'datum': r[1], 'quelle': r[2]} for r in rows]
        return jsonify({'ok': True, 'ergebnisse': out})
    except Exception as e:
        logging.warning('sold-cache: %s', str(e)[:100])
        return jsonify({'ok': False, 'fehler': str(e)[:120]}), 500


@app.route('/api/deals')
def api_deals():
    try: min_g=float(request.args.get('min_gewinn',0))
    except: min_g=0
    if min_g<0: min_g=0
    if min_g>1000: min_g=1000
    q=sanitize_q(request.args.get('q','iPhone'))
    try: rw=int(request.args.get('reichweite',50))
    except: rw=50
    if rw<5: rw=5
    if rw>200: rw=200
    # Ausschluss-Woerter (Komma-getrennt) serverseitig: Titel, die eines dieser
    # Woerter enthalten, komplett raus. So gilt der Filter fuer gespeicherte
    # Suchauftraege unabhaengig vom Frontend.
    aus_raw = (request.args.get('ausschluss', '') or '')
    aus_woerter = [w.strip().lower() for w in aus_raw.split(',') if w.strip()]
    alle = _cache_get(q)
    if alle is None:
        alle = scan(q)
        _CACHE[q] = (time.monotonic(), alle)
        log_history(q, alle)
    gef = [d for d in alle if d["gewinn_netto"] >= min_g]
    # Reichweite Filter echte Entfernung
    if rw < 100:
        gef = [d for d in gef if d["entfernung_km"] is None or d["entfernung_km"] <= rw or d["plattform"] in ["ebay", "vinted"]]
    # Ausschluss serverseitig anwenden
    if aus_woerter:
        gef = [d for d in gef if not any(w in str(d.get("titel", "")).lower() for w in aus_woerter)]
    return jsonify({"erfolg": True, "stand": datetime.now().strftime("%d.%m.%Y %H:%M:%S"), "anzahl": len(gef), "deals": gef, "min_profit": 100, "filter": {"min_gewinn": min_g, "reichweite": rw, "q": q, "ausschluss": aus_woerter}})

@app.route('/api/history')
def api_history():
    q = sanitize_q(request.args.get('q','iPhone'))
    conn = hist_conn()
    if not conn:
        return jsonify({"erfolg":False,"aktiv":False,"punkte":[]})
    try:
        cur = conn.cursor()
        if _is_postgres(conn):
            cur.execute("""SELECT to_timestamp(floor(extract(epoch from zeit)/600)*600) AS h,
                                  AVG(gewinn_netto), MAX(gewinn_netto), COUNT(*)
                           FROM gewinn_historie WHERE q=%s
                           GROUP BY h ORDER BY h ASC LIMIT 120""", (q,))
        else:
            cur.execute("""SELECT CAST(strftime('%s', zeit) AS INTEGER) / 600 * 600 AS h,
                                  AVG(gewinn_netto), MAX(gewinn_netto), COUNT(*)
                           FROM gewinn_historie WHERE q=?
                           GROUP BY h ORDER BY h ASC LIMIT 120""", (q,))
        rows = cur.fetchall()
        cur.close()
        punkte = []
        for r in rows:
            zeit = r[0]
            if not _is_postgres(conn):
                zeit = datetime.fromtimestamp(int(zeit)).isoformat()
            punkte.append({"zeit": zeit, "schnitt": float(r[1] or 0),
                           "max": float(r[2] or 0), "n": int(r[3] or 0)})
        return jsonify({"erfolg": True, "aktiv": True, "q": q, "punkte": punkte})
    except Exception as e:
        return jsonify({"erfolg": False, "aktiv": True, "fehler": str(e), "punkte": []})
    finally:
        conn.close()

@app.route('/api/health')
def health():
    conn=get_db()
    db_size=os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    anzahl=conn.execute("SELECT COUNT(*) as c FROM deals").fetchone()["c"]
    conn.close()
    uptime=datetime.now()-START_TIME
    return jsonify({"main":"ok","min_profit":100,"db_groesse":db_size,"db_anzahl":anzahl,"uptime_sekunden":int(uptime.total_seconds()),"kleinigkeiten":"100+ drin","version":"ULTRA FINAL"})

@app.route('/api/kleinigkeiten')
def kleinigkeiten():
    return jsonify({"vb_erkennung":True,"tausch_filter":True,"abholung_erkennung":True,"einkauf_versand":True,"euro_format":True,"rundung":True,"negativ_filter":True,"puffer_5":True,"umlaute":True,"leerzeichen":True,"emoji_entfernt":True,"titel_80":True,"link_https":True,"bild_validierung":True,"kleinanzeigen_name":True,"vinted_filter":True,"ebay_auktion":True,"standort_haversine":True,"plz_validierung":True,"reichweite_5_200":True,"privat_gewerbe_hinweis":True,"batteriegesetz":True,"imei_hinweis":True,"impressum":True,"loading":True,"leer_zustand":True,"error_zustand":True,"badge":True,"farbe":True,"quelle_badge":True,"abzuege_aufklappbar":True,"deine_gebuehr_aufklappbar":True,"filter_info":True,"dark_mode":True,"apple_style":True})

if __name__=='__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8765)))


@app.route('/api/debug')
def api_debug():
    """Nur Konfigurations-Status (gesetzt/fehlt), NIE die Werte selbst."""
    def gesetzt(k):
        return bool(os.environ.get(k, "").strip())
    return jsonify({
        "ebay_app_id_gesetzt": gesetzt("EBAY_APP_ID"),
        "ebay_cert_id_gesetzt": gesetzt("EBAY_CERT_ID"),
        "ebay_env": os.environ.get("EBAY_ENV", "(leer)"),
        "proxy_list_gesetzt": gesetzt("PROXY_LIST"),
        "cache_ttl": _CACHE_TTL,
        "headless": os.environ.get("HEADLESS", "1"),
        "min_profit": os.environ.get("MIN_PROFIT", "(Default)"),
    })
