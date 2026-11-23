import csv
import html
import io
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from urllib.parse import urljoin

REMOTE_CONFIG_PATH = "remote_config.json"
DELTA_UPDATES_PATH = "delta_updates.json"

def fetch_bytes(url, timeout=30):
    cmd = [
        "curl", "-sS", "-L", "--fail",
        "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,text/csv,application/zip,text/plain,*/*;q=0.8",
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

def fetch_text(url, timeout=30):
    return decode_text(fetch_bytes(url, timeout=timeout))

def extract_anchors(page_html, base_url):
    """Estrae tutte le coppie (url_assoluto, testo_pulito) dai link HTML."""
    anchors = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(m.group(1)).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full_url = urljoin(base_url, href)
        text = re.sub(r"<[^>]+>", " ", html.unescape(m.group(2)))
        text = re.sub(r"\s+", " ", text).strip()
        key = (full_url, text)
        if key not in seen:
            seen.add(key)
            anchors.append((full_url, text))
    return anchors

def find_data_files(anchors):
    """Trova link diretti a CSV o ZIP (sia da estensione URL sia da testo link)."""
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
    """Scarica un CSV diretto o estrae tutti i CSV contenuti in uno ZIP."""
    raw = fetch_bytes(url, timeout=35)
    if len(raw) < 200:
        return []
    # Controlla firma ZIP (PK..)
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

def parse_aifa_csv(csv_text):
    """Trova automaticamente la vera riga di intestazione saltando titoli/date iniziali di AIFA."""
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        return []

    header_idx = 0
    best_delim = ";"
    for i, ln in enumerate(lines[:15]):
        u = ln.upper()
        sc = ln.count(";")
        cm = ln.count(",")
        if max(sc, cm) >= 2 and any(tok in u for tok in ("AIC", "MEDICINALE", "DENOMINAZIONE", "PREZZO", "CONFEZIONE", "PRINCIPIO")):
            header_idx = i
            best_delim = ";" if sc >= cm else ","
            break

    body = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(body), delimiter=best_delim)
    rows = []
    for row in reader:
        clean_row = {}
        for k, v in row.items():
            if k is not None:
                clean_row[k.strip().upper()] = (v or "").strip()
        if clean_row:
            rows.append(clean_row)
    return rows

def parse_price(val):
    if not val:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", val).replace(",", ".")
    try:
        num = round(float(cleaned), 2)
        return num if num > 0 else None
    except ValueError:
        return None

def extract_aic(row):
    for k, v in row.items():
        if ("AIC" in k or "MINSAN" in k) and "DATA" not in k and "TITOLARE" not in k and "DITTA" not in k:
            # Rifiuta stringhe che contengono barre di data es. 20/11/2026
            if "/" in v:
                continue
            digits = re.sub(r"\D", "", v)
            if 7 <= len(digits) <= 9:
                return digits.zfill(9)
    return ""

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
        "price_updates": prev_delta.get("price_updates", {}),
        "shortages": prev_delta.get("shortages", []),
        "revocations_notices": prev_delta.get("revocations_notices", [])
    }

    # 1. PREZZI UFFICIALI AIFA (Scoperta 2 livelli: pagina indice -> ultimo aggiornamento -> CSV/ZIP)
    try:
        index_pages = [
            "https://www.aifa.gov.it/liste-di-trasparenza",
            "https://www.aifa.gov.it/elenchi-di-farmaci-di-classe-a-e-h"
        ]
        file_candidates = []
        subpage_candidates = []

        for idx_url in index_pages:
            try:
                page_html = fetch_text(idx_url)
                anchors = extract_anchors(page_html, idx_url)
                file_candidates.extend(find_data_files(anchors))
                for u, t in anchors:
                    tl = t.lower()
                    ul = u.lower()
                    if "aifa.gov.it" in ul and u not in index_pages and u not in subpage_candidates:
                        if any(w in tl or w in ul for w in ("trasparenza", "classe-a", "classe a", "equivalenti", "aggiornat", "2026", "2025")):
                            if not any(ext in ul for ext in (".pdf", ".xls", ".xlsx", ".doc")):
                                subpage_candidates.append(u)
            except Exception:
                continue

        # Scendi di 1 livello nelle prime 4 sotto-pagine più recenti per pescare CSV/ZIP
        for sub_url in subpage_candidates[:4]:
            try:
                sub_html = fetch_text(sub_url)
                sub_anchors = extract_anchors(sub_html, sub_url)
                for f_url in find_data_files(sub_anchors):
                    if f_url not in file_candidates:
                        file_candidates.append(f_url)
            except Exception:
                continue

        # Dai priorità ai CSV di trasparenza e classe A
        file_candidates.sort(key=lambda u: (0 if any(w in u.lower() for w in ("trasparenza", "classe_a", "classe-a", "a_per", "equivalenti")) else 1))

        updated_prices = {}
        used_files = []
        for f_url in file_candidates[:6]:
            try:
                for fname, csv_text in extract_csv_texts_from_url(f_url):
                    rows = parse_aifa_csv(csv_text)
                    added = 0
                    for r in rows:
                        aic = extract_aic(r)
                        if not aic:
                            continue
                        prezzo = None
                        prezzo_rif = None
                        for k, v in r.items():
                            if "RIFERIMENTO" in k and prezzo_rif is None:
                                prezzo_rif = parse_price(v)
                            elif "PREZZO" in k and prezzo is None:
                                prezzo = parse_price(v)
                        if prezzo is not None:
                            updated_prices[aic] = {"p": prezzo, "pr": prezzo_rif, "cl": "A"}
                            added += 1
                    if added > 20:
                        used_files.append(f"{fname[:30]}({added})")
            except Exception:
                continue

        if updated_prices:
            delta["price_updates"] = updated_prices
            delta["source_status"]["aifa_prezzi"] = f"OK ({len(updated_prices)} prezzi: {', '.join(used_files[:3])})"
        else:
            delta["source_status"]["aifa_prezzi"] = f"NO_PRICES (file:{len(file_candidates)}, sub:{len(subpage_candidates)})"
    except Exception as e:
        delta["source_status"]["aifa_prezzi"] = f"ERR ({str(e)[:80]})"

    # 2. CARENZE AIFA (Con fix intestazione multi-riga)
    try:
        c_html = fetch_text("https://www.aifa.gov.it/farmaci-carenti")
        c_files = find_data_files(extract_anchors(c_html, "https://www.aifa.gov.it/farmaci-carenti"))
        shortages = []
        used_file = ""
        for f_url in c_files[:3]:
            try:
                for fname, csv_text in extract_csv_texts_from_url(f_url):
                    rows = parse_aifa_csv(csv_text)
                    for r in rows:
                        aic = extract_aic(r)
                        if not aic:
                            continue
                        nome = ""
                        motivo = ""
                        for k, v in r.items():
                            if not nome and any(w in k for w in ("MEDICINALE", "DENOMINAZIONE", "FARMACO", "CONFEZIONE")):
                                nome = v
                            elif not motivo and any(w in k for w in ("MOTIV", "STATO", "NOTE", "CAUSA")):
                                motivo = v
                        shortages.append({
                            "minsan": aic,
                            "nome": nome[:80],
                            "stato": (motivo or "Carente AIFA")[:100]
                        })
                    if shortages:
                        used_file = fname[:40]
                        break
            except Exception:
                continue
            if shortages:
                break

        if shortages:
            delta["shortages"] = shortages[:4000]
            delta["source_status"]["aifa_carenze"] = f"OK ({len(shortages)} carenze da {used_file})"
        else:
            delta["source_status"]["aifa_carenze"] = f"NO_ROWS (file:{len(c_files)})"
    except Exception as e:
        delta["source_status"]["aifa_carenze"] = f"ERR ({str(e)[:80]})"

    # 3. AVVISI RITIRI E DIVIETI DI UTILIZZO AIFA (Match sul testo dei titoli)
    try:
        notices = []
        for page_url in (
            "https://www.aifa.gov.it/ritiri-e-divieti-di-utilizzo",
            "https://www.aifa.gov.it/notizie"
        ):
            try:
                p_html = fetch_text(page_url)
                for u, t in extract_anchors(p_html, page_url):
                    tl = t.lower()
                    if len(t) >= 25 and any(w in tl for w in ("ritir", "diviet", "revoc", "lotto", "lotti", "sospension")):
                        if t not in notices:
                            notices.append(t[:150])
                if notices:
                    break
            except Exception:
                continue

        if notices:
            delta["revocations_notices"] = notices[:50]
            delta["source_status"]["aifa_ritiri"] = f"OK ({len(notices)} avvisi ufficiali)"
        else:
            delta["source_status"]["aifa_ritiri"] = "NO_NOTICES"
    except Exception as e:
        delta["source_status"]["aifa_ritiri"] = f"ERR ({str(e)[:80]})"

    config["last_updated"] = now_iso
    config["delta_updates_url"] = "https://raw.githubusercontent.com/herasymcuk/farmadiario-config/refs/heads/main/delta_updates.json"

    with open(REMOTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(DELTA_UPDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    sync()
