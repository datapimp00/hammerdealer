"""Headless-Browser-Scraping fuer JS-Portale (markt.de, vinted, willhaben).

Render Free (512 MB RAM) kann Chromium evtl. nicht starten -> graceful Fallback:
kein Playwright / Browser-Crash / Timeout => [] . Service laeuft immer weiter.
EIN Browser wird fuer alle Portale wiederverwendet (spart RAM).
"""
import logging
import re

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    _PW_OK = True
except Exception as e:
    _PW_OK = False
    log.info("playwright nicht verfuegbar: %s", str(e)[:80])

_BROWSER_OK = None


def browser_verfuegbar():
    """True wenn Playwright da ist UND Chromium startet. Gecacht."""
    global _BROWSER_OK
    if not _PW_OK:
        return False
    if _BROWSER_OK is not None:
        return _BROWSER_OK
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            b.close()
        _BROWSER_OK = True
    except Exception as e:
        log.warning("Chromium nicht startfaehig: %s", str(e)[:120])
        _BROWSER_OK = False
    return _BROWSER_OK


def _render_page(page, url, wait_selector=None, timeout=12000):
    """Laedt eine JS-Seite in einer vorhandenen Page. Gibt HTML oder None."""
    page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=timeout)
        except Exception:
            pass
    page.wait_for_timeout(1200)
    return page.content()


# (name, url-builder, wait_selector)
# markt.de getestet: nur 'aehnliche Anzeigen' (Muell) -> NICHT dabei.
# willhaben: parst __NEXT_DATA__ JSON (30 saubere Adverts, kein DOM-Geraten).
# vinted: DOM-basiert (App-Router, kein __NEXT_DATA__) - fragiler.
def _url_vinted(q):
    return "https://www.vinted.de/catalog?search_text=" + q


def _url_willhaben(q):
    return ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=" + q)


_PORTALE = [
    ("willhaben", _url_willhaben,
     "[data-testid='search-result'], div[class*='SearchResultCard']"),
    ("vinted", _url_vinted,
     "div.feed-grid__item-content, [data-testid*='item'], div[class*='feed-grid']"),
]


def headless_scan(q):
    """Alle JS-Portale in EINEM Browser durchgehen. [] wenn Browser nicht geht."""
    if not browser_verfuegbar():
        return []
    import requests
    q_enc = requests.utils.quote(q)
    out = []
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                locale="de-DE")
            page = ctx.new_page()
            for name, url_fn, sel in _PORTALE:
                try:
                    html = _render_page(page, url_fn(q_enc), wait_selector=sel)
                    if not html:
                        continue
                    if name == "willhaben":
                        out += parse_willhaben_json(html)
                    else:
                        out += _parse_vinted(html)
                except Exception as e:
                    log.info("headless %s: %s", name, str(e)[:80])
                    continue
    except Exception as e:
        log.warning("headless browser scan fehlgeschlagen: %s", str(e)[:120])
        return []
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    log.info("headless_scan '%s': %d Artikel", q, len(out))
    return out

def _parse_preis(text):
    """'1.629 €' -> 1629.0 ; '45,90 EUR' -> 45.90"""
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


def _gueltiger_titel(titel):
    """Filtert Muell-Titel raus: zu kurz, reine Preise/Preisart-Fragmente."""
    if not titel:
        return False
    t = titel.strip()
    if len(t) < 8:
        return False
    # reiner Preis / Preis-Fragment ("50,00 €", "inkl.", "50,00 € | 53.2€")
    if re.fullmatch(r"[\d\s.,€EUR|]+", t):
        return False
    # sehr kurz oder nur Sonderzeichen/Zahlen
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4:
        return False
    return True


def _zustand(titel):
    t = (titel or "").lower()
    if any(w in t for w in ("defekt", "kaputt", "schaden", "reparatur")):
        return "defekt"
    if any(w in t for w in ("neu", "versiegelt", "ungeöffnet", "ovp", "fabrikneu")):
        return "neu"
    return "gut"


def _wh_attr(adv, name):
    """Wert eines willhaben-Attributs (erster Eintrag) oder None."""
    for at in adv.get("attributes", {}).get("attribute", []):
        if at.get("name") == name:
            vals = at.get("values") or []
            return vals[0] if vals else None
    return None


def parse_willhaben_json(html, limit=30):
    """willhaben __NEXT_DATA__ JSON parsen -> saubere Advert-Dicts.
    Enthaelt Titel/Preis/URL/Bild/Standort strukturiert (kein DOM-Geraten)."""
    from bs4 import BeautifulSoup
    import json
    soup = BeautifulSoup(html, "html.parser")
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        log.info("willhaben: kein __NEXT_DATA__")
        return []
    try:
        data = json.loads(nd.string)
    except Exception as e:
        log.info("willhaben __NEXT_DATA__ JSON-Fehler: %s", str(e)[:60])
        return []
    advs = (data.get("props", {}).get("pageProps", {})
            .get("searchResult", {}).get("advertSummaryList", {}).get("advertSummary", []))
    out = []
    for adv in advs[:limit]:
        try:
            titel = adv.get("description", "")
            preis = _parse_preis(_wh_attr(adv, "PRICE/AMOUNT") or "") or _parse_preis(
                _wh_attr(adv, "PRICE_FOR_DISPLAY") or "")
            if preis is None or preis <= 0 or not _gueltiger_titel(titel):
                continue
            # Link: seoSelfLink oder iadShareLink
            link = "https://www.willhaben.at"
            for c in adv.get("contextLinkList", {}).get("contextLink", []):
                if c.get("id") == "iadShareLink":
                    link = c.get("uri", link)
                    break
            seo = _wh_attr(adv, "SEO_URL")
            if seo and not link.startswith("http"):
                link = "https://www.willhaben.at/iad/" + seo
            bild_url = _wh_attr(adv, "ALL_IMAGE_URLS") or ""
            if bild_url and not bild_url.startswith("http"):
                bild_url = "https://cache.willhaben.at/" + bild_url
            out.append({
                "titel": titel[:80], "preis": preis, "link": link,
                "bild": bild_url, "standort": _wh_attr(adv, "LOCATION") or "",
                "zustand": _zustand(titel), "plattform": "willhaben", "beschreibung": "",
            })
        except Exception:
            continue
    log.info("headless willhaben(json): %d Artikel", len(out))
    return out


def _parse_vinted(html, limit=30):
    """vinted DOM-parsen (App-Router, kein __NEXT_DATA__). Fragil: nur saubere
    Titel aus dedizierten Selektoren, kein Container-Text."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    titel_sels = ["h3", "h4", "[data-testid*='item-title']",
                  "[class*='ItemBox__description']",
                  "[class*='new-item-box__description']",
                  "a[href*='/items/']", "p"]
    out = []
    arts = soup.select("div[class*='feed-grid__item'], div[class*='feed-grid'], "
                       "[data-testid*='item'], [class*='ItemBox'], article")
    seen = set()
    for a in arts[: limit * 4]:
        try:
            titel = ""
            for sel in titel_sels:
                el = a.select_one(sel)
                if el:
                    cand = el.get("title") if el.get("title") else el.get_text(strip=True)
                    if cand and _gueltiger_titel(cand):
                        titel = cand
                        break
            if not titel:
                img = a.select_one("img[alt]")
                if img and img.get("alt"):
                    titel = img.get("alt").strip()
            if not _gueltiger_titel(titel):
                continue
            preis = _parse_preis(a.get_text(" ", strip=True))
            if preis is None or preis <= 0:
                continue
            l = a.select_one("a[href]")
            href = l.get("href", "") if l else ""
            if href.startswith("/"):
                href = "https://www.vinted.de" + href
            i = a.select_one("img")
            bild = ((i.get("src") or i.get("data-src") or "") if i else "")
            key = re.sub(r"\s+", " ", titel.lower())[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "titel": titel[:80], "preis": preis, "link": href or "https://www.vinted.de",
                "bild": bild, "standort": "", "zustand": _zustand(titel),
                "plattform": "vinted", "beschreibung": "",
            })
            if len(out) >= limit:
                break
        except Exception:
            continue
    log.info("headless vinted(dom): %d Artikel", len(out))
    return out


