"""Echtes Scraping fuer HAMMERDEALER: Kleinanzeigen (Hauptquelle), eBay + Vinted best effort."""
import os, re, json, random, logging, time
import requests
from bs4 import BeautifulSoup

from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def _proxy_list():
    raw = os.environ.get("PROXY_LIST", "").strip()
    if not raw:
        try:
            raw = open(os.path.join(os.path.dirname(__file__), "PROXY_LIST.txt")).read().strip()
        except Exception:
            raw = ""
    return [p.strip() for p in raw.split(",") if p.strip()]

def _get(url, headers=None, timeout=12, use_proxy=True, tries=3):
    """Direkt zuerst (schnell), dann bis zu `tries` zufaellige Proxies."""
    pool = [None]
    if use_proxy:
        pl = _proxy_list()
        if pl:
            pool += random.sample(pl, min(tries, len(pl)))
    for px in pool:
        try:
            r = requests.get(url, headers=headers or HEADERS,
                             proxies=({"http": px, "https": px} if px else None),
                             timeout=timeout)
            if r.status_code == 200:
                return r
            log.info("GET %s -> %s (%s)", url[:70], r.status_code, "proxy" if px else "direct")
        except Exception as e:
            log.info("GET %s Fehler %s", url[:70], str(e)[:60])
    return None

def _parse_preis(text):
    """'1.629 €' -> 1629.0 ; '45,90 EUR' -> 45.90 ; '€ 1.990' -> 1990.0 ; sonst None"""
    if not text:
        return None
    for m in (re.search(r"(\d{1,4}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?\s*(?:€|EUR)", text),
              re.search(r"(?:€|EUR)\s*(\d{1,4}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?", text)):
        if not m:
            continue
        raw = m.group(1).replace(".", "")
        try:
            return float(raw) + (float("0." + m.group(2)) if m.group(2) else 0.0)
        except Exception:
            continue
    return None

def _zustand(titel):
    t = (titel or "").lower()
    if any(w in t for w in ("defekt", "kaputt", "schaden", "reparatur", "screen aus")):
        return "defekt"
    if any(w in t for w in ("neu", "versiegelt", "ungeoeffnet", "ungeöffnet", "ovp", "fabrikneu")):
        return "neu"
    return "gut"

def schaetze_verkauf(kauf, titel):
    """Verkaufspreis-Schaetzung ohne eBay-Sold-Daten -> Faktor je Kategorie."""
    t = (titel or "").lower()
    if any(w in t for w in ("versiegelt", "neu &", "fabrikneu", "ovp", "ungeöffnet")):
        f = 1.22
    elif any(w in t for w in ("auto", "audi", "bmw", "vw", "mercedes", "pkw", "kombi", "suv")):
        f = 1.2
    elif any(w in t for w in ("iphone", "samsung", "handy", "smartphone", "macbook", "laptop", "ipad", "tablet")):
        f = 1.45
    elif any(w in t for w in ("konsole", "ps5", "xbox", "switch", "grafikkarte", "gaming", "pc")):
        f = 1.35
    elif any(w in t for w in ("fahrrad", "e-bike", "ebike", "mountainbike", "renner", "kamera", "objektiv", "sony", "canon", "nikon")):
        f = 1.4
    elif any(w in t for w in ("schuhe", "jacke", "kleid", "mode", "pullover", "hose", "tasche", "uhr", "sneaker")):
        f = 1.9
    elif any(w in t for w in ("möbel", "sofa", "tisch", "stuhl", "kommode", "schrank")):
        f = 1.3
    else:
        f = 1.5
    return round(kauf * f, 2)

def kleinanzeigen(q, limit=75, pages=3):
    """Echte Kleinanzeigen-Suche ueber mehrere Ergebnisseiten (Paging).
    Liefert [] wenn Portal nicht erreichbar."""
    slug = re.sub(r"[^a-z0-9äöü]+", "-", (q or "").lower()).strip("-") or "iphone"
    out = []
    for page in range(1, pages + 1):
        if len(out) >= limit:
            break
        url = ("https://www.kleinanzeigen.de/s-" + slug + "/k0" if page == 1
               else "https://www.kleinanzeigen.de/s-" + slug + "/k0seite%dc0LpZ" % page)
        r = _get(url)
        if not r:
            break  # weitere Seiten ebenfalls blockiert -> aufhoeren
        soup = BeautifulSoup(r.text, "html.parser")
        got_before = len(out)
        for a in soup.select("article")[: limit * 2]:
            try:
                titel, bild, desc = "", "", ""
                for sc in a.select('script[type="application/ld+json"]'):
                    try:
                        d = json.loads(sc.string or "{}")
                    except Exception:
                        continue
                    titel = d.get("title") or titel
                    bild = d.get("contentUrl") or bild
                    desc = (d.get("description") or "")[:300] or desc
                if not titel:
                    h = a.select_one("[data-testid='aditem-title'], h2, .ellipsis")
                    titel = h.get_text(strip=True) if h else a.get_text(" ", strip=True)[:80]
                if not titel:
                    continue
                # Titel-Header-Muell entfernen: "45888 Gelsenkirchen Heute, 06:22 Apple..."
                titel = re.sub(r"^\d{5}\s+\S+\s+(Heute|Gestern|vor \d+|\d{2}:\d{2})[^A-Za-zÄÖÜ]*",
                               "", titel).strip() or titel
                href = a.get("data-href") or ""
                link = ("https://www.kleinanzeigen.de" + href) if href.startswith("/") else href
                text = a.get_text(" ", strip=True)
                preis = _parse_preis(text)
                if preis is None:
                    continue
                m = re.match(r"(\d{5})\s+([A-Za-zÄÖÜäöü\- ]{2,30}?)\s+[A-ZÄÖÜ]", text)
                standort = (m.group(2).strip() if m else "")
                out.append({
                    "titel": titel[:80], "preis": preis, "link": link, "bild": bild,
                    "standort": standort, "zustand": _zustand(titel + " " + desc),
                    "plattform": "kleinanzeigen", "beschreibung": desc,
                })
                if len(out) >= limit:
                    break
            except Exception as e:
                log.info("KA-Parse-Fehler: %s", str(e)[:80])
        # Keine neuen Artikel auf dieser Seite -> naechste bringt vermutlich auch nichts
        if len(out) == got_before:
            break
    return out

def ebay(q, limit=15):
    """Best effort - eBay blockt viele IPs; [] wenn gesperrt."""
    url = "https://www.ebay.de/sch/i.html?_nkw=" + requests.utils.quote(q) + "&_ipg=60"
    r = _get(url, tries=1)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for el in soup.select(".s-item")[:limit]:
        try:
            t = el.select_one(".s-item__title")
            p = el.select_one(".s-item__price")
            i = el.select_one(".s-item__image-img")
            h = el.select_one("a.s-item__link")
            titel = t.get_text(strip=True) if t else ""
            preis = _parse_preis(p.get_text() if p else "")
            if not titel or titel == "Shop auf eBay" or preis is None:
                continue
            out.append({
                "titel": titel[:80], "preis": preis,
                "link": (h.get("href", "").split("?")[0] if h else "https://www.ebay.de"),
                "bild": ((i.get("src") or "") if i else ""), "standort": "",
                "zustand": _zustand(titel), "plattform": "ebay", "beschreibung": "",
            })
        except Exception:
            continue
    return out[:limit]

def vinted(q, limit=15):
    """Best effort - Vinted (DataDome) blockt oft; [] wenn gesperrt."""
    url = ("https://www.vinted.de/api/v2/catalog/items?search_text="
           + requests.utils.quote(q) + "&per_page=" + str(limit) + "&page=1")
    r = _get(url, tries=1, headers={**HEADERS, "Accept": "application/json",
                           "Referer": "https://www.vinted.de/"})
    if not r:
        return []
    try:
        items = (r.json() or {}).get("items", [])
    except Exception:
        return []
    out = []
    for it in items[:limit]:
        try:
            pr = it.get("price")
            price = float(str(pr).replace(",", ".")) if pr else 0.0
            if not price:
                continue
            photo = (it.get("photo") or {}).get("url") or ""
            user = it.get("user") if isinstance(it.get("user"), dict) else {}
            out.append({
                "titel": str(it.get("title") or "")[:80], "preis": price,
                "link": "https://www.vinted.de/items/" + str(it.get("id") or ""),
                "bild": photo, "standort": user.get("city", ""),
                "zustand": _zustand(str(it.get("title") or "")),
                "plattform": "vinted", "beschreibung": "",
            })
        except Exception:
            continue
    return out

def scan_real(q):
    """Alle Quellen abfragen; liefert (items, quellen_namen)."""
    items, quellen = [], []
    try:
        ka = kleinanzeigen(q)
        if ka:
            items += ka
            quellen.append("kleinanzeigen")
    except Exception as e:
        log.warning("KA: %s", str(e)[:100])
    for fn, name in ((ebay, "ebay"), (vinted, "vinted")):
        try:
            got = fn(q)
            if got:
                items += got
                quellen.append(name)
        except Exception as e:
            log.warning("%s: %s", name, str(e)[:100])
    return items, quellen

def _jsonld_products(html):
    """Generischer JSON-LD-Extractor: Produkt/Angebot/ListItem -> Titel/Preis/Bild/URL."""
    out = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for sc in soup.select('script[type="application/ld+json"]'):
            try:
                d = json.loads(sc.string or "")
            except Exception:
                continue
            items = d if isinstance(d, list) else [d]
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("@type") == "ItemList":
                    items += it.get("itemListElement", [])
                    continue
                t = it.get("name") or ""
                if not t:
                    continue
                price = None
                offers = it.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price") or offers.get("lowPrice")
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price") if isinstance(offers[0], dict) else None
                img = it.get("image") or it.get("thumbnailUrl")
                if isinstance(img, list) and img:
                    img = img[0]
                url = it.get("url") or ""
                if price is not None and t:
                    try:
                        price = float(str(price).replace(",", "."))
                    except Exception:
                        continue
                    out.append({"titel": str(t)[:80], "preis": price,
                                "bild": str(img or ""), "link": str(url or "")})
    except Exception:
        pass
    return out

def _generic_articles(html, base_url, plattform, price_sel=None, title_sel=None, img_sel=None, link_sel=None):
    """Fallback: HTML-Artikel mit CSS-Selektoren."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    arts = soup.select("article, [data-testid], .aditem, li.ad, .item, .listing")
    for a in arts[:30]:
        try:
            t = a.select_one(title_sel or "h2, h3, a") if title_sel or True else a.select_one("h2, h3, a")
            title = t.get_text(strip=True) if t else ""
            if not title:
                continue
            p = a.select_one(price_sel) if price_sel else None
            preis = _parse_preis(p.get_text(" ", strip=True)) if p else None
            if preis is None:
                txt = a.get_text(" ", strip=True)
                preis = _parse_preis(txt)
            if preis is None:
                continue
            i = a.select_one(img_sel) if img_sel else a.select_one("img")
            bild = (i.get("src") or i.get("data-src") or "") if i else ""
            l = a.select_one(link_sel) if link_sel else a.select_one("a[href]")
            href = l.get("href") if l else ""
            if href and href.startswith("/"):
                href = base_url + href
            out.append({"titel": title[:80], "preis": preis, "bild": bild, "link": href or base_url})
        except Exception:
            continue
    return out[:20]

def _portal(q, name, url, base_url, extra_headers=None, tries=2):
    """Ein Portal abfragen: JSON-LD zuerst, dann generische Artikel."""
    r = _get(url, headers=({**HEADERS, **(extra_headers or {})}), tries=tries)
    if not r:
        return []
    items = _jsonld_products(r.text)
    if not items:
        items = _generic_articles(r.text, base_url, name)
    for it in items:
        it["plattform"] = name
        it["zustand"] = _zustand(it.get("titel", ""))
    return items

def marktde(q):
    url = "https://www.markt.de/kleinanzeigen/?q=" + requests.utils.quote(q)
    r = _get(url, tries=1)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select(".clsy-c-carousel__item")[:20]:
        try:
            pr = item.select_one(".clsy-c-carousel__item-price")
            preis = _parse_preis(pr.get_text(strip=True)) if pr else None
            a = item.select_one("a[href]")
            titel = a.get_text(strip=True) if a else ""
            if not titel:
                txt = item.select_one(".clsy-c-carousel__item-text")
                titel = txt.get_text(" ", strip=True) if txt else ""
                titel = re.sub(r"\d[\d\.]*\s*€.*$", "", titel).strip()
            if not titel or preis is None:
                continue
            href = a.get("href", "") if a else ""
            link = ("https://www.markt.de" + href) if href.startswith("/") else href
            img = item.select_one("img")
            out.append({"titel": titel[:80], "preis": preis,
                        "bild": (img.get("src") or "") if img else "",
                        "link": link or "https://www.markt.de",
                        "standort": "", "zustand": _zustand(titel), "plattform": "marktde",
                        "beschreibung": ""})
        except Exception:
            continue
    return out[:20]

def autoscout24(q):
    url = "https://www.autoscout24.de/lst?query=" + requests.utils.quote(q)
    r = _get(url, tries=1)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("article")[:20]:
        try:
            h = a.select_one("h2, h1")
            titel = h.get_text(strip=True) if h else ""
            text = a.get_text(" ", strip=True)
            preis = _parse_preis(text)
            if not titel or preis is None:
                continue
            l = a.select_one("a[href]")
            href = l.get("href", "") if l else ""
            link = ("https://www.autoscout24.de" + href) if href.startswith("/") else href
            img = a.select_one("img")
            out.append({"titel": titel[:80], "preis": preis,
                        "bild": (img.get("src") or "") if img else "",
                        "link": link or "https://www.autoscout24.de",
                        "standort": "", "zustand": _zustand(titel), "plattform": "autoscout24",
                        "beschreibung": ""})
        except Exception:
            continue
    return out[:20]

def willhaben(q):
    return _portal(q, "willhaben",
        "https://www.willhaben.at/iad/kaufen-und-verkaufen?keyword=" + requests.utils.quote(q),
        "https://www.willhaben.at")

def shpock(q):
    return _portal(q, "shpock",
        "https://www.shpock.com/de-de/search?q=" + requests.utils.quote(q),
        "https://www.shpock.com")

def autoscout24(q):
    return _portal(q, "autoscout24",
        "https://www.autoscout24.de/lst?query=" + requests.utils.quote(q),
        "https://www.autoscout24.de")

def refurbed(q):
    return _portal(q, "refurbed",
        "https://www.refurbed.de/suchergebnisse/?q=" + requests.utils.quote(q),
        "https://www.refurbed.de")

def nebenan(q):
    return _portal(q, "nebenan",
        "https://www.nebenan.de/suche?query=" + requests.utils.quote(q),
        "https://www.nebenan.de")

# Registrierte Portale: (name, funktion)
# Nur Quellen, die WIRKLICH such-relevante Daten liefern.
# kleinanzeigen (HTML, Multi-Page) + ebay (Browse-API).
# JS-Portale (markt.de, vinted, willhaben) laufen ueber headless_scan (Playwright).
def portale():
    """Liefert die Plattform-Liste (Nur-Quellelistierung). Liefert exakt die aktiven Portale."""
    return [
        ("kleinanzeigen", kleinanzeigen),
        ("ebay", ebay_browse),
    ]


def headless_aktiv():
    """Headless-Scraping an/aus. Per Env abschaltbar (HEADLESS=0)."""
    return os.environ.get("HEADLESS", "1").strip() not in ("0", "false", "off", "no")


def scan_real(q):
    """Alle registrierten Portale parallel abfragen (best effort).
    HTTP-Portale + optional Headless-Browser-Scan (JS-Portale) parallel."""
    items, quellen = [], []

    def grab(name, fn):
        got = fn(q)
        if got:
            return got, name
        return None, None

    futures = []
    with ThreadPoolExecutor(max_workers=len(portale())) as pool:
        for name, fn in portale():
            futures.append(pool.submit(grab, name, fn))
        # Headless-Scan parallel dazuschalten (eigener Browser-Thread)
        if headless_aktiv():
            def grab_headless():
                try:
                    from headless_scraper import headless_scan
                    got = headless_scan(q)
                    if got:
                        return got, "headless"
                except Exception as e:
                    log.warning("headless_scan: %s", str(e)[:100])
                return None, None
            futures.append(pool.submit(grab_headless))
        for fut in as_completed(futures):
            got, name = fut.result()
            if got:
                items += got
                quellen.append(name)

    return items, quellen

# ---------- eBay Browse API (offiziell, kostenlos) ----------
_EBAY_TOKEN = {"token": None, "exp": 0}

def _ebay_base() -> str:
    """Hostname der eBay-API (Produktion oder Sandbox)."""
    env = os.environ.get("EBAY_ENV", "").strip().lower()
    sandbox = env in ("sandbox", "test", "dev") or "SBX" in os.environ.get("EBAY_APP_ID", "").upper()
    return "api.sandbox.ebay.com" if sandbox else "api.ebay.com"
def _ebay_creds():
    app = os.environ.get("EBAY_APP_ID", "").strip()
    cert = os.environ.get("EBAY_CERT_ID", "").strip()
    return app, cert

def ebay_token():
    """OAuth client_credentials -> access_token (gecacht)."""
    app, cert = _ebay_creds()
    if not app or not cert:
        return None
    if _EBAY_TOKEN["token"] and time.time() < _EBAY_TOKEN["exp"] - 60:
        return _EBAY_TOKEN["token"]
    try:
        import base64
        cred = base64.b64encode((app + ":" + cert).encode()).decode()
        r = requests.post(
            "https://" + _ebay_base() + "/identity/v1/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic " + cred,
            },
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=12,
        )
        if r.status_code == 200:
            d = r.json()
            _EBAY_TOKEN["token"] = d.get("access_token")
            _EBAY_TOKEN["exp"] = time.time() + int(d.get("expires_in", 7200))
            return _EBAY_TOKEN["token"]
        log.info("eBay-Token: HTTP %s %s", r.status_code, r.text[:120])
    except Exception as e:
        log.warning("eBay-Token: %s", str(e)[:100])
    return None

def ebay_browse(q, limit=30):
    """Offizielle eBay-Suche (item_summary). [] wenn kein Key oder Fehler."""
    token = ebay_token()
    if not token:
        return []
    try:
        r = requests.get(
            "https://" + _ebay_base() + "/buy/browse/v1/item_summary/search",
            headers={"Authorization": "Bearer " + token},
            params={"q": q, "limit": limit, "fieldgroups": "EXTENDED"},
            timeout=15,
        )
        if r.status_code != 200:
            log.info("eBay-Browse: HTTP %s %s", r.status_code, r.text[:120])
            return []
        data = r.json()
    except Exception as e:
        log.warning("eBay-Browse: %s", str(e)[:100])
        return []
    out = []
    for it in (data.get("itemSummaries") or [])[:limit]:
        try:
            price = it.get("price", {}).get("value")
            if price is None:
                continue
            img = it.get("image", {}) or {}
            loc = it.get("itemLocation", {}) or {}
            standort = (loc.get("city") or "") + ((", " + loc.get("country")) if loc.get("country") else "")
            cond = str(it.get("condition") or "").lower()
            zustand = "neu" if "new" in cond else "defekt" if ("defect" in cond or "damaged" in cond) else "gut"
            out.append({
                "titel": str(it.get("title") or "")[:80],
                "preis": float(price),
                "link": it.get("itemWebUrl") or "https://www.ebay.de",
                "bild": img.get("imageUrl") or "",
                "standort": standort,
                "zustand": zustand,
                "plattform": "ebay",
                "beschreibung": "",
            })
        except Exception:
            continue
    return out

def ebay_market_price(q):
    """Durchschnittspreis der eBay-Treffer als echte Marktpreis-Schaetzung."""
    items = ebay_browse(q, limit=15)
    if not items:
        return None
    preise = [x["preis"] for x in items]
    if not preise:
        return None
    preise.sort()
    n = len(preise)
    return round(preise[n // 2], 2)  # Median statt Mittelwert (robust)
