import csv
import io
import json
import os
import re
import subprocess
from datetime import datetime, timezone

REMOTE_CONFIG_PATH = "remote_config.json"
DELTA_UPDATES_PATH = "delta_updates.json"

# Endpoint ufficiali pubblici (Open Data AIFA Drive + Ministero della Salute)
SOURCES = {
    "aifa_classe_a": [
        "https://drive.aifa.gov.it/farmaci/a.csv",
        "https://www.aifa.gov.it/documents/20142/1967301/Lista_trasparenza_AIFA.csv"
    ],
    "aifa_carenze": [
        "https://www.aifa.gov.it/documents/20142/1804926/elenco_medicinali_carenti.csv",
        "https://drive.aifa.gov.it/farmaci/carenze.csv"
    ],
    "salute_veterinari": [
        "https://www.dati.salute.gov.it/imgs/C_17_dataset_177_0_upFile.csv"
    ]
}

def fetch_via_curl(url, timeout=20):
    cmd = [
        "curl", "-sS", "-L", "--fail",
        "--max-time", str(timeout),
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "-H", "Accept: text/csv,text/plain,application/xml,*/*;q=0.8",
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

def fetch_first_working(url_list):
    errors = []
    for url in url_list:
        try:
            text = fetch_via_curl(url)
            if len(text) > 500 and "<html" not in text[:300].lower():
                return text, url
            errors.append("HTML_OR_EMPTY")
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(" | ".join(errors))

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

    # 1. Prezzi Ufficiali Classe A (AIFA Drive)
    try:
        csv_text, used_url = fetch_first_working(SOURCES["aifa_classe_a"])
        delim = ";" if ";" in csv_text[:500] else ","
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=delim)
        updated_prices = {}
        for row in reader:
            row_u = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
            aic = ""
            for key in ("CODICE AIC", "AIC", "MINSAN", "CODICE_AIC"):
                if key in row_u and row_u[key]:
                    aic = re.sub(r"\D", "", row_u[key]).zfill(9)
                    break
            if len(aic) == 9:
                prezzo = None
                for key in ("PREZZO AL PUBBLICO", "PREZZO", "PREZZO_PUBBLICO"):
                    if key in row_u:
                        prezzo = parse_price(row_u[key])
                        if prezzo is not None:
                            break
                prezzo_rif = parse_price(row_u.get("PREZZO DI RIFERIMENTO", ""))
                if prezzo is not None:
                    updated_prices[aic] = {"p": prezzo, "pr": prezzo_rif, "cl": "A"}
        if updated_prices:
            delta["price_updates"] = updated_prices
            delta["source_status"]["aifa_prezzi"] = f"OK ({len(updated_prices)} record da {used_url.split('/')[2]})"
        else:
            delta["source_status"]["aifa_prezzi"] = "PARSED_0_ROWS"
    except Exception as e:
        delta["source_status"]["aifa_prezzi"] = f"ERR ({str(e)[:80]})"

    # 2. Carenze AIFA
    try:
        csv_text, used_url = fetch_first_working(SOURCES["aifa_carenze"])
        delim = ";" if ";" in csv_text[:500] else ","
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=delim)
        shortages = []
        for row in reader:
            row_u = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
            aic = re.sub(r"\D", "", row_u.get("AIC", "") or row_u.get("CODICE AIC", "")).zfill(9)
            nome = row_u.get("MEDICINALE", "") or row_u.get("DENOMINAZIONE", "")
            motivo = row_u.get("MOTIVAZIONE", "") or row_u.get("STATO", "Carente")
            if len(aic) == 9 and aic != "000000000":
                shortages.append({"minsan": aic, "nome": nome[:80], "stato": motivo[:100]})
        if shortages:
            delta["shortages"] = shortages[:3000]
            delta["source_status"]["aifa_carenze"] = f"OK ({len(shortages)} record)"
        else:
            delta["source_status"]["aifa_carenze"] = "PARSED_0_ROWS"
    except Exception as e:
        delta["source_status"]["aifa_carenze"] = f"ERR ({str(e)[:80]})"

    # 3. Test Connettività Ministero Salute (Veterinari)
    try:
        csv_text, used_url = fetch_first_working(SOURCES["salute_veterinari"])
        lines = len(csv_text.splitlines())
        delta["source_status"]["salute_vet"] = f"OK ({lines} righe)"
    except Exception as e:
        delta["source_status"]["salute_vet"] = f"ERR ({str(e)[:80]})"

    config["last_updated"] = now_iso
    config["delta_updates_url"] = "https://raw.githubusercontent.com/herasymcuk/farmadiario-config/refs/heads/main/delta_updates.json"
    delta["last_updated"] = now_iso

    with open(REMOTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(DELTA_UPDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    sync()
