import csv
import html
import io
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import urljoin

REMOTE_CONFIG_PATH = "remote_config.json"
DELTA_UPDATES_PATH = "delta_updates.json"

def fetch_via_curl(url, timeout=25):
    cmd = [
        "curl", "-sS", "-L", "--fail",
        "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,text/csv,text/plain,*/*;q=0.8",
        "-H", "Accept-Language: it-IT,it;q=0.9",
        url
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"curl_{proc.returncode}: {err[:60]}")
    raw = proc.stdout
    for enc in ("utf-8-sig", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")

def discover_links(page_url, ext_pattern=r"\.csv"):
    """Scarica una pagina AIFA e trova tutti i link a file CSV/dati pubblicati."""
    page_html = fetch_via_curl(page_url, timeout=25)
    raw_hrefs = re.findall(r'href=["\']([^"\']+)["\']', page_html, flags=re.IGNORECASE)
    found = []
    for h in raw_hrefs:
        clean_h = html.unescape(h).strip()
        if re.search(ext_pattern, clean_h, flags=re.IGNORECASE):
            full_url = urljoin(page_url, clean_h)
            if full_url not in found:
                found.append(full_url)
    return found, page_html

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def parse_price(val):
    if not val:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", val).replace(",", ".")
    try:
        num = round(float(cleaned), 2)
        return num if num > 0 else None
    except ValueError:
        return None

def parse_csv_rows(csv_text):
    sample = csv_text[:2000]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delim)
    rows = []
    for row in reader:
        rows.append({(k or "").strip().upper(): (v or "").strip() for k, v in row.items()})
    return rows

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

    # 1. PREZZI UFFICIALI AIFA (Auto-scoperta CSV da Liste di Trasparenza + Classe A/H)
    try:
        csv_candidates = []
        for index_page in (
            "https://www.aifa.gov.it/liste-di-trasparenza",
            "https://www.aifa.gov.it/elenchi-di-farmaci-di-classe-a-e-h",
            "https://www.aifa.gov.it/open-data"
        ):
            try:
                links, _ = discover_links(index_page, r"\.csv")
                # Dai priorità ai CSV di trasparenza / classe A
                links.sort(key=lambda u: (0 if any(w in u.lower() for w in ("trasparenza", "classe_a", "equivalenti", "a_per")) else 1))
                csv_candidates.extend(links)
            except Exception:
                continue

        updated_prices = {}
        used_urls = []
        for csv_url in csv_candidates[:6]:
            try:
                csv_text = fetch_via_curl(csv_url, timeout=30)
                if len(csv_text) < 500 or "<html" in csv_text[:300].lower():
                    continue
                rows = parse_csv_rows(csv_text)
                count_here = 0
                for r in rows:
                    aic = ""
                    for k in r:
                        if "AIC" in k or "MINSAN" in k:
                            digits = re.sub(r"\D", "", r[k])
                            if 7 <= len(digits) <= 9:
                                aic = digits.zfill(9)
                                break
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
                        count_here += 1
                if count_here > 50:
                    used_urls.append(csv_url.split("/")[-1][:45])
            except Exception:
                continue

        if updated_prices:
            delta["price_updates"] = updated_prices
            delta["source_status"]["aifa_prezzi"] = f"OK ({len(updated_prices)} prezzi da {', '.join(used_urls[:2])})"
        else:
            delta["source_status"]["aifa_prezzi"] = f"NO_CSV_FOUND (candidati: {len(csv_candidates)})"
    except Exception as e:
        delta["source_status"]["aifa_prezzi"] = f"ERR ({str(e)[:80]})"

    # 2. CARENZE E TEMPORANEE INDISPONIBILITÀ AIFA (Auto-scoperta CSV)
    try:
        links, _ = discover_links("https://www.aifa.gov.it/farmaci-carenti", r"\.csv")
        shortages = []
        used_file = ""
        for csv_url in links[:4]:
            try:
                csv_text = fetch_via_curl(csv_url, timeout=30)
                if len(csv_text) < 500 or "<html" in csv_text[:300].lower():
                    continue
                rows = parse_csv_rows(csv_text)
                for r in rows:
                    aic = ""
                    nome = ""
                    motivo = ""
                    for k, v in r.items():
                        if not aic and ("AIC" in k or "MINSAN" in k):
                            digits = re.sub(r"\D", "", v)
                            if 7 <= len(digits) <= 9:
                                aic = digits.zfill(9)
                        elif not nome and ("MEDICINALE" in k or "DENOMINAZIONE" in k or "FARMACO" in k):
                            nome = v
                        elif not motivo and ("MOTIV" in k or "STATO" in k or "NOTE" in k):
                            motivo = v
                    if aic:
                        shortages.append({
                            "minsan": aic,
                            "nome": nome[:80],
                            "stato": (motivo or "Carente AIFA")[:100]
                        })
                if shortages:
                    used_file = csv_url.split("/")[-1][:40]
                    break
            except Exception:
                continue

        if shortages:
            delta["shortages"] = shortages[:4000]
            delta["source_status"]["aifa_carenze"] = f"OK ({len(shortages)} carenze da {used_file})"
        else:
            delta["source_status"]["aifa_carenze"] = f"NO_CSV_FOUND (link trovati: {len(links)})"
    except Exception as e:
        delta["source_status"]["aifa_carenze"] = f"ERR ({str(e)[:80]})"

    # 3. AVVISI RITIRI E DIVIETI DI UTILIZZO AIFA (Lettura diretta pagina istituzionale)
    try:
        notices = []
        for page_url in (
            "https://www.aifa.gov.it/ritiri-e-divieti-di-utilizzo",
            "https://www.aifa.gov.it/web/guest/ritiri-e-divieti-di-utilizzo"
        ):
            try:
                page_html = fetch_via_curl(page_url, timeout=25)
                # Estrae titoli di comunicazioni/avvisi dalla pagina
                raw_titles = re.findall(r'<a[^>]+href=["\'][^"\']*(?:ritir|diviet|revoc|carenz|lotto)[^"\']*["\'][^>]*>(.*?)</a>', page_html, flags=re.IGNORECASE | re.DOTALL)
                for t in raw_titles:
                    clean = re.sub(r"<[^>]+>", "", html.unescape(t))
                    clean = re.sub(r"\s+", " ", clean).strip()
                    if len(clean) >= 20 and clean not in notices:
                        notices.append(clean[:140])
                if notices:
                    break
            except Exception:
                continue

        if notices:
            delta["revocations_notices"] = notices[:50]
            delta["source_status"]["aifa_ritiri"] = f"OK ({len(notices)} avvisi ufficiali)"
        else:
            delta["source_status"]["aifa_ritiri"] = "NO_NOTICES_PARSED"
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
