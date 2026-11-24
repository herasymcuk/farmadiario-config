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
    m = re.search(r"(\d{4}-\d{2}-\d{
