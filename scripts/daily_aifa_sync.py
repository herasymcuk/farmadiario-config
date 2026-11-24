import csv
import html
import io
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin

REMOTE_CONFIG_PATH = "remote_config.json"
DELTA_UPDATES_PATH = "delta_updates.json"
FEDERFARMA_ALERTS_PATH = "federfarma_alerts.json"
RECALL_URL_HINTS = (
    "ritir", "diviet", "difett", "sospens", "sequestr", "falsificat"
)
RECALL_PHRASES = (
    "divieto di utilizzo", "divieto d'uso", "divieto di vendita",
    "ritiro lott", "ritirati lott", "ritiro di lott", "ritiro di alcuni lott",
    "ritiro cautelativo", "ritiro volontario", "ritiro a scopo precauzionale",
    "ritiro dal commercio", "ritiro dal mercato", "ritiro del medicinale",
    "ritiro dei medicinali", "difetto di qualità", "difetti di qualità",
    "sospensione cautelativa", "sospensione della commercializzazione",
    "farmaci falsificati", "medicinale falsificato", "sequestro"
)
RECALL_EXCLUDE = (
    "importazione", "procurator", "accreditamento", "domicilio digitale",
    "indirizzo pec", "dirigenzial", "manuale utente", "modello segnalazione",
    "allegato", ".xlsx", ".xls", ".pdf", "obblighi per i titolari",
    "conferimento dell'incarico", "ritiri e divieti di utilizzo",
    "archivio ritiri", "normativa", "modulistica"
)


def clean_text(s):
    txt = re.sub(r"\s+", " ", (s or "")).strip()
    return re.sub(r"\s+:", ":", txt)


def fetch_bytes(url, timeout=40, polite_delay=1.2):
    if polite_delay > 0:
        time.sleep(polite_delay)
    cmd = [
        "curl", "-sS", "-L", "--fail",
        "--retry", "2", "--retry-delay", "3",
        "--connect-timeout", "15",
        "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml,text/csv,application/zip,text/plain,*/*;q=0.8",
        "-H", "Accept-Language: it-IT,it;q=0.9",
        url
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"curl_{proc.returncode}: {err[:60]}")
    return proc.stdout


def decode_text(raw):
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_text(url, timeout=40, polite_delay=1.2):
    return decode_text(fetch_bytes(url, timeout=timeout, polite_delay=polite_delay))


def extract_anchors(page_html, base_url):
    anchors = []
    seen = set()
    pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    for m in re.finditer(pattern, page_html, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(m.group(1)).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(base_url, href)
        text = clean_text(re.sub(r"<[^>]+>", " ", html.unescape(m.group(2))))
        key = (full_url, text)
        if key not in seen:
            seen.add(key)
            anchors.append((full_url, text))
    return anchors


def find_data_files(anchors):
    files = []
    for url, text in anchors:
        u_low = url.lower()
        t_low = text.lower()
        is_csv = (".csv" in u_low) or ("/documents/" in u_low and "csv" in t_low)
        is_zip = (".zip" in u_low) or ("/documents/" in u_low and "zip" in t_low)
        if (is_csv or is_zip) and url not in files:
            files.append(url)
    return files


def extract_csv_texts_from_url(url):
    raw = fetch_bytes(url, timeout=45)
    if len(raw) < 200:
        return []
    if raw[:2] == b"PK":
        texts = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    texts.append((name, decode_text(zf.read(name))))
        return texts
    text = decode_text(raw)
    if "<html" in text[:400].lower():
        return []
    return [(url.split("/")[-1].split("?")[0], text)]


def norm_header(h):
    return re.sub(r"\s+", " ", (h or "").replace(".", "").strip().upper())


def parse_aifa_csv(csv_text):
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        return [], ""

    header_idx = 0
    best_delim = ";"
    tokens = (
        "AIC", "MEDICINALE", "DENOMINAZIONE", "PREZZO", "CONFEZIONE",
        "PRINCIPIO", "TITOLARE", "CARENZA", "INIZIO", "TIPOLOGIA"
    )
    for i, ln in enumerate(lines[:25]):
        u = ln.replace('"', "").replace("'", "").replace(".", "").strip().upper()
        if u.startswith(("NB:", "NB ", "NOTA", "AVVISO", "ELENCO", "AGGIORN")):
            continue
        sc = ln.count(";")
        cm = ln.count(",")
        delim = ";" if sc >= cm else ","
        parts = [p.strip().strip('"') for p in ln.split(delim)]
        matched = sum(1 for tok in tokens if tok in u)
        if len(parts) >= 3 and matched >= 2 and len(parts[0]) < 60:
            header_idx = i
            best_delim = delim
            break

    header_preview = lines[header_idx][:90]
    body = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(body), delimiter=best_delim)
    rows = []
    for row in reader:
        clean_row = {}
        for k, v in row.items():
            if k is not None:
                clean_row[norm_header(k)] = (v or "").strip()
        if clean_row:
            rows.append(clean_row)
    return rows, header_preview


def parse_price(val):
    if not val:
        return None
    v = val.replace("€", "").strip()
    if re.search(r"[a-zA-Z]", v):
        return None
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    else:
        v = v.replace(",", ".")
    cleaned = re.sub(r"[^\d\.]", "", v)
    try:
        num = round(float(cleaned), 2)
        return num if 0 < num < 9999999 else None
    except ValueError:
        return None


def sanitize_prices(price_map):
    """Garantisce che pr (prezzo di riferimento) sia sempre <= p (prezzo al pubblico)."""
    cleaned = {}
    for aic, item in price_map.items():
        if not isinstance(item, dict):
            continue
        p = item.get("p")
        pr = item.get("pr")
        if p is None or p <= 0:
            continue
        if pr is not None and (pr <= 0 or pr > p):
            pr = None
        cleaned[aic] = {"p": p, "pr": pr, "cl": item.get("cl", "A")}
    return cleaned


def extract_aic(row):
    bad_cols = ("DATA", "INIZIO", "FINE", "SCADENZ", "TITOLARE", "DITTA", "RAGIONE")
    for k, v in row.items():
        if ("AIC" in k or "MINSAN" in k) and not any(bad in k for bad in bad_cols):
            if "/" in v or "-" in v:
                continue
            digits = re.sub(r"\D", "", v)
            if 6 <= len(digits) <= 9:
                return digits.zfill(9)
    return ""


def parse_date_iso(raw_date):
    raw_date = clean_text(raw_date)
    if not raw_date:
        return ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw_date)
    if m:
        return m.group(1)
    try:
        dt = parsedate_to_datetime(raw_date)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def is_valid_recall_title(title, date_iso=""):
    t = clean_text(title)
    tl = t.lower().replace("\u2019", "'")
    if len(t) < 22 or len(t) > 240:
        return False
    if any(bad in tl for bad in RECALL_EXCLUDE):
        return False
    if date_iso:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=540)).strftime("%Y-%m-%d")
        if date_iso < cutoff:
            return False
    return any(phrase in tl for phrase in RECALL_PHRASES)


def format_notice(title, date_iso=""):
    t = clean_text(html.unescape(title))
    suffix = r"\s*[-\u2013|]\s*(AIFA|Agenzia Italiana del Farmaco|aifa\.gov\.it).*$"
    t = re.sub(suffix, "", t, flags=re.IGNORECASE).strip()
    if date_iso:
        return f"[{date_iso}] {t}"[:180]
    return t[:180]


def parse_rss_items(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="ignore"))
    except Exception:
        return items

    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag in ("item", "entry"):
            title = ""
            pub = ""
            for child in elem:
                ctag = child.tag.split("}")[-1].lower()
                if ctag == "title" and not title:
                    title = "".join(child.itertext())
                elif ctag in ("pubdate", "published", "updated", "date") and not pub:
                    pub = "".join(child.itertext())
            if title:
                items.append((parse_date_iso(pub), clean_text(html.unescape(title))))
    return items


def fetch_aifa_recalls():
    """Raccoglie solo avvisi clinici reali di ritiro/divieto AIFA degli ultimi 18 mesi."""
    found = []
    seen_keys = set()

    def add_notice(date_iso, title):
        if not is_valid_recall_title(title, date_iso):
            return
        formatted = format_notice(title, date_iso)
        dedup_key = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", formatted).lower()[:80]
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            found.append((date_iso or "0000-00-00", formatted))

    # Strategia 1: Auto-discovery RSS + HTML su pagine ufficiali AIFA
    rss_urls = []
    aifa_pages = (
        "https://www.aifa.gov.it/rss",
        "https://www.aifa.gov.it/qualita-dei-medicinali",
        "https://www.aifa.gov.it/difetti-di-qualita",
        "https://www.aifa.gov.it/news",
    )
    rss_link_re = r'<link[^>]+type=["\']application/rss\+xml["\'][^>]+href=["\']([^"\']+)["\']'
    for page_url in aifa_pages:
        try:
            p_html = fetch_text(page_url, timeout=25, polite_delay=1.0)
            for m in re.finditer(rss_link_re, p_html, flags=re.IGNORECASE):
                r_url = urljoin(page_url, html.unescape(m.group(1)).strip())
                if r_url not in rss_urls:
                    rss_urls.append(r_url)
            for u, t in extract_anchors(p_html, page_url):
                u_low = u.lower()
                if ("rss" in u_low or "feed" in u_low) and "aifa.gov.it" in u_low:
                    if u not in rss_urls:
                        rss_urls.append(u)
                elif "/w/" in u_low or "/-/" in u_low:
                    add_notice("", t)
        except Exception:
            continue

    for r_url in rss_urls[:5]:
        try:
            xml_txt = fetch_text(r_url, timeout=25, polite_delay=1.0)
            for d_iso, title in parse_rss_items(xml_txt):
                add_notice(d_iso, title)
        except Exception:
            continue

    if found:
        found.sort(key=lambda x: x[0], reverse=True)
        return [txt for _, txt in found[:40]], "AIFA diretto"

    # Strategia 2: Sitemap ufficiale AIFA
    try:
        sm_xml = fetch_text("https://www.aifa.gov.it/sitemap.xml", timeout=30, polite_delay=1.0)
        child_maps = re.findall(r"<loc>\s*(https?://[^<]+sitemap[^<]*)\s*</loc>", sm_xml, flags=re.IGNORECASE)
        xml_sources = [sm_xml]
        for cm in child_maps[:3]:
            try:
                xml_sources.append(fetch_text(html.unescape(cm), timeout=25, polite_delay=1.0))
            except Exception:
                continue

        for src in xml_sources:
            for block in re.findall(r"<url>(.*?)</url>", src, flags=re.IGNORECASE | re.DOTALL):
                loc_m = re.search(r"<loc>\s*([^<]+)\s*</loc>", block, flags=re.IGNORECASE)
                if not loc_m:
                    continue
                loc = html.unescape(loc_m.group(1)).strip()
                loc_low = loc.lower()
                if not any(kw in loc_low for kw in RECALL_URL_HINTS):
                    continue
                if "/w/" not in loc_low and "/-/" not in loc_low:
                    continue
                lm_m = re.search(r"<lastmod>\s*([^<]+)\s*</lastmod>", block, flags=re.IGNORECASE)
                d_iso = parse_date_iso(lm_m.group(1)) if lm_m else ""
                slug = unquote(loc.rstrip("/").split("/")[-1]).replace("-", " ").strip()
                if slug:
                    add_notice(d_iso, slug[0].upper() + slug[1:])
        if found:
            found.sort(key=lambda x: x[0], reverse=True)
            return [txt for _, txt in found[:40]], "AIFA sitemap"
    except Exception:
        pass

    # Strategia 3: Feed RSS indicizzato filtrato su site:aifa.gov.it (ultimi 18 mesi)
    try:
        since_date = (datetime.now(timezone.utc) - timedelta(days=540)).strftime("%Y-%m-%d")
        query = (
            "site:aifa.gov.it+"
            "(%22divieto+di+utilizzo%22+OR+%22ritiro+lotti%22+OR+"
            "%22ritiro+cautelativo%22+OR+%22ritiro+dal+commercio%22+OR+"
            "%22ritiro+dal+mercato%22+OR+%22difetto+di+qualit%C3%A0%22)"
            f"+after:{since_date}"
        )
        rss_url = f"https://news.google.com/rss/search?q={query}&hl=it&gl=IT&ceid=IT:it"
        xml_txt = fetch_text(rss_url, timeout=25, polite_delay=0.5)
        for d_iso, title in parse_rss_items(xml_txt):
            add_notice(d_iso, title)
        if found:
            found.sort(key=lambda x: x[0], reverse=True)
            return [txt for _, txt in found[:40]], "AIFA RSS"
    except Exception:
        pass

    return [], ""


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def sync():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = load_json(REMOTE_CONFIG_PATH, {
        "config_version": 1,
        # 4. INTEGRAZIONE AUTOMATICA PROVVEDIMENTI AIFA DA PEC FEDERFARMA
    ff = load_json(FEDERFARMA_ALERTS_PATH, {})
    ff_revoked_aics = ff.get("revoked_aics", [])
    ff_prefixes6 = ff.get("revoked_prefixes6", [])
    ff_lots = ff.get("recalled_lots", [])
    ff_new_aics = ff.get("new_aics", [])
    ff_notices = ff.get("notices", [])

    delta["revoked_aics"] = [x.get("aic") for x in ff_revoked_aics if x.get("aic")]
    delta["revoked_prefixes6"] = ff_prefixes6
    delta["recalled_lots"] = ff_lots
    delta["new_aics"] = ff_new_aics
    config["recalled_lots"] = ff_lots

    # Rende subito effettive le revoche AIC/AIP come FUORI_COMMERCIO anche sulle build attuali
    if ff_revoked_aics:
        existing_shortages = {s["minsan"]: s for s in delta.get("shortages", []) if s.get("minsan")}
        for item in ff_revoked_aics:
            aic = item.get("aic", "")
            if aic:
                tipo = item.get("type", "REVOCA_AIC")
                nota = f"Cessata commercializzazione definitiva ({tipo} AIFA - {item.get('date', '')})"
                existing_shortages[aic] = {
                    "minsan": aic,
                    "nome": clean_text(item.get("name", ""))[:90],
                    "stato": nota[:220]
                }
        delta["shortages"] = list(existing_shortages.values())[:5500]

    if ff_notices:
        merged_notices = list(ff_notices)
        for n in delta.get("revocations_notices", []):
            if n not in merged_notices:
                merged_notices.append(n)
        delta["revocations_notices"] = merged_notices[:50]

    delta["source_status"]["federfarma_pec"] = (
        f"OK ({len(ff_revoked_aics)} AIC revocati, {len(ff_prefixes6)} famiglie 6-cifre, "
        f"{len(ff_lots)} lotti, {len(ff_new_aics)} nuovi)"
    )
        "last_updated": now_iso,
        "recalled_lots": [],
        "critical_interactions": [],
        "search_sources": []
    })
    prev_delta = load_json(DELTA_UPDATES_PATH, {})
    delta = {
        "schema_version": 1,
        "last_updated": now_iso,
        "source_status": {},
        "price_updates": sanitize_prices(prev_delta.get("price_updates", {})),
        "shortages": prev_delta.get("shortages", []),
        "revocations_notices": prev_delta.get("revocations_notices", [])
    }

    # 1. PREZZI UFFICIALI AIFA (Liste di Trasparenza + Classe A e H)
    try:
        index_pages = [
            "https://www.aifa.gov.it/liste-di-trasparenza",
            "https://www.aifa.gov.it/elenchi-di-farmaci-di-classe-a-e-h"
        ]
        file_candidates = []
        subpage_candidates = []
        page_hints = ("trasparenza", "classe-a", "classe a", "equivalenti", "aggiornat", "2026", "2025")

        for idx_url in index_pages:
            try:
                page_html = fetch_text(idx_url)
                anchors = extract_anchors(page_html, idx_url)
                file_candidates.extend(find_data_files(anchors))
                for u, t in anchors:
                    tl, ul = t.lower(), u.lower()
                    if "aifa.gov.it" in ul and u not in index_pages and u not in subpage_candidates:
                        if any(w in tl or w in ul for w in page_hints):
                            if not any(ext in ul for ext in (".pdf", ".xls", ".xlsx", ".doc")):
                                subpage_candidates.append(u)
            except Exception:
                continue

        for sub_url in subpage_candidates[:3]:
            try:
                sub_html = fetch_text(sub_url)
                for f_url in find_data_files(extract_anchors(sub_html, sub_url)):
                    if f_url not in file_candidates:
                        file_candidates.append(f_url)
            except Exception:
                continue

        priority = ("trasparenza", "equivalenti", "classe-a", "classe_a")
        file_candidates.sort(key=lambda u: (0 if any(w in u.lower() for w in priority) else 1))

        updated_prices = {}
        used_files = []
        for f_url in file_candidates[:5]:
            try:
                for fname, csv_text in extract_csv_texts_from_url(f_url):
                    rows, _ = parse_aifa_csv(csv_text)
                    added = 0
                    for r in rows:
                        aic = extract_aic(r)
                        if not aic:
                            continue
                        prezzo = None
                        prezzo_rif = None
                        for k, v in r.items():
                            if "PREZZO" in k and ("RIFERIMENTO" in k or "RIMBORSO" in k or "MASSIMO" in k):
                                if prezzo_rif is None:
                                    prezzo_rif = parse_price(v)
                            elif "PREZZO" in k and prezzo is None:
                                prezzo = parse_price(v)
                        if prezzo is not None:
                            existing = updated_prices.get(aic)
                            if prezzo_rif is None and existing:
                                prezzo_rif = existing.get("pr")
                            if prezzo_rif is not None and prezzo_rif > prezzo:
                                prezzo_rif = None
                            updated_prices[aic] = {"p": prezzo, "pr": prezzo_rif, "cl": "A"}
                            added += 1
                    if added > 20:
                        used_files.append(f"{fname[:30]}({added})")
            except Exception:
                continue

        if updated_prices:
            delta["price_updates"] = sanitize_prices(updated_prices)
            delta["source_status"]["aifa_prezzi"] = f"OK ({len(delta['price_updates'])} prezzi: {', '.join(used_files[:3])})"
        else:
            delta["source_status"]["aifa_prezzi"] = f"CACHE ({len(delta['price_updates'])} prezzi mantenuti; file:{len(file_candidates)})"
    except Exception as e:
        delta["source_status"]["aifa_prezzi"] = f"CACHE ({len(delta['price_updates'])} prezzi; err:{str(e)[:50]})"

    # 2. CARENZE AIFA (pulite da \n, spazi doppi e MINSAN duplicati)
    try:
        c_html = fetch_text("https://www.aifa.gov.it/farmaci-carenti")
        c_files = find_data_files(extract_anchors(c_html, "https://www.aifa.gov.it/farmaci-carenti"))
        c_files.sort(key=lambda u: (0 if "carenti" in u.lower() else 1))
        shortages_map = {}
        used_file = ""
        last_header = ""
        for f_url in c_files[:4]:
            try:
                for fname, csv_text in extract_csv_texts_from_url(f_url):
                    rows, hdr = parse_aifa_csv(csv_text)
                    last_header = hdr
                    for r in rows:
                        aic = extract_aic(r)
                        if not aic:
                            continue
                        nome = ""
                        motivo = ""
                        for k, v in r.items():
                            if not nome and any(w in k for w in ("MEDICINALE", "DENOMINAZIONE", "FARMACO", "CONFEZIONE")):
                                nome = v
                            elif not motivo and any(w in k for w in ("MOTIV", "STATO", "NOTE", "CAUSA", "TIPOLOGIA")):
                                motivo = v
                        nome_clean = clean_text(nome)[:90]
                        stato_clean = clean_text(motivo or "Carente AIFA")[:220]
                        prev = shortages_map.get(aic)
                        if prev is None or len(stato_clean) > len(prev["stato"]):
                            shortages_map[aic] = {
                                "minsan": aic,
                                "nome": nome_clean,
                                "stato": stato_clean
                            }
                    if shortages_map:
                        used_file = fname[:40]
                        break
            except Exception:
                continue
            if shortages_map:
                break

        if shortages_map:
            shortages = list(shortages_map.values())
            delta["shortages"] = shortages[:5000]
            delta["source_status"]["aifa_carenze"] = f"OK ({len(shortages)} carenze uniche da {used_file})"
        else:
            delta["source_status"]["aifa_carenze"] = f"NO_ROWS (hdr: {last_header[:55]})"
    except Exception as e:
        delta["source_status"]["aifa_carenze"] = f"CACHE ({len(delta['shortages'])} carenze; err:{str(e)[:50]})"

    # 3. AVVISI RITIRI E DIVIETI DI UTILIZZO AIFA (solo avvisi clinici recenti)
    try:
        notices, source_label = fetch_aifa_recalls()
        if notices:
            delta["revocations_notices"] = notices
            delta["source_status"]["aifa_ritiri"] = f"OK ({len(notices)} avvisi da {source_label})"
        else:
            delta["source_status"]["aifa_ritiri"] = f"CACHE ({len(delta['revocations_notices'])} avvisi)"
    except Exception as e:
        delta["source_status"]["aifa_ritiri"] = f"CACHE ({len(delta['revocations_notices'])}; err:{str(e)[:50]})"

    config["last_updated"] = now_iso
    config["delta_updates_url"] = (
        "https://raw.githubusercontent.com/herasymcuk/farmadiario-config/"
        "refs/heads/main/delta_updates.json"
    )

    with open(REMOTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(DELTA_UPDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    sync()
