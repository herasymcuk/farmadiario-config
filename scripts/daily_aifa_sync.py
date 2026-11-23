import csv
import io
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

REMOTE_CONFIG_PATH = "remote_config.json"
DELTA_UPDATES_PATH = "delta_updates.json"

# Fonti pubbliche ufficiali AIFA (Open Data)
AIFA_SOURCES = {
    "trasparenza_a": "https://www.aifa.gov.it/documents/20142/1967301/Lista_trasparenza_AIFA.csv",
    "carenze": "https://www.aifa.gov.it/documents/20142/1804926/elenco_medicinali_carenti.csv",
    "ritiri_rss": "https://www.aifa.gov.it/web/guest/ritiri-e-divieti-di-utilizzo/-/asset_publisher/rss"
}

def fetch_text(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "FarmaDiario-SyncBot/4.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        for enc in ("utf-8-sig", "latin-1", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")

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
        return round(float(cleaned), 2)
    except ValueError:
        return None

def sync():
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = load_json(REMOTE_CONFIG_PATH, {
        "config_version": 1,
        "last_updated": now_iso,
        "recalled_lots": [],
        "critical_interactions": [],
        "search_sources": []
    })
    delta = load_json(DELTA_UPDATES_PATH, {
        "schema_version": 1,
        "last_updated": now_iso,
        "source_status": {},
        "price_updates": {},
        "shortages": [],
        "revocations_notices": []
    })

    # 1. Aggiornamento Prezzi Ufficiali & Quota Riferimento AIFA (Classe A)
    try:
        csv_text = fetch_text(AIFA_SOURCES["trasparenza_a"])
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
        updated_prices = {}
        for row in reader:
            row_u = { (k or "").strip().upper(): (v or "").strip() for k, v in row.items() }
            aic = re.sub(r"\D", "", row_u.get("CODICE AIC", "") or row_u.get("AIC", ""))
            if len(aic) == 9:
                prezzo = parse_price(row_u.get("PREZZO AL PUBBLICO", "") or row_u.get("PREZZO", ""))
                prezzo_rif = parse_price(row_u.get("PREZZO DI RIFERIMENTO", ""))
                if prezzo is not None:
                    updated_prices[aic] = {
                        "p": prezzo,
                        "pr": prezzo_rif,
                        "cl": "A"
                    }
        if updated_prices:
            delta["price_updates"] = updated_prices
            delta["source_status"]["aifa_prezzi"] = f"OK ({len(updated_prices)} record)"
    except Exception as e:
        delta["source_status"]["aifa_prezzi"] = f"CACHE ({type(e).__name__})"

    # 2. Aggiornamento Carenze / Revoche / Sospensioni temporanee AIFA
    try:
        csv_text = fetch_text(AIFA_SOURCES["carenze"])
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
        shortages = []
        for row in reader:
            row_u = { (k or "").strip().upper(): (v or "").strip() for k, v in row.items() }
            aic = re.sub(r"\D", "", row_u.get("AIC", "") or row_u.get("CODICE AIC", ""))
            nome = row_u.get("MEDICINALE", "") or row_u.get("DENOMINAZIONE", "")
            motivo = row_u.get("MOTIVAZIONE", "") or row_u.get("STATO", "Carente / Sospeso")
            if len(aic) == 9:
                shortages.append({"minsan": aic, "nome": nome[:80], "stato": motivo[:100]})
        if shortages:
            delta["shortages"] = shortages[:3000]
            delta["source_status"]["aifa_carenze"] = f"OK ({len(shortages)} record)"
    except Exception as e:
        delta["source_status"]["aifa_carenze"] = f"CACHE ({type(e).__name__})"

    # 3. Controllo RSS Ritiri e Divieti di Utilizzo AIFA
    try:
        rss_text = fetch_text(AIFA_SOURCES["ritiri_rss"])
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", rss_text)
        notices = [t[0] or t[1] for t in titles if "ritir" in (t[0] or t[1]).lower() or "diviet" in (t[0] or t[1]).lower() or "revoc" in (t[0] or t[1]).lower()]
        if notices:
            delta["revocations_notices"] = notices[:50]
            delta["source_status"]["aifa_ritiri"] = f"OK ({len(notices)} avvisi)"
    except Exception as e:
        delta["source_status"]["aifa_ritiri"] = f"CACHE ({type(e).__name__})"

    config["last_updated"] = now_iso
    config["delta_updates_url"] = "https://raw.githubusercontent.com/herasymcuk/farmadiario-config/refs/heads/main/delta_updates.json"
    delta["last_updated"] = now_iso

    with open(REMOTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(DELTA_UPDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    sync()
