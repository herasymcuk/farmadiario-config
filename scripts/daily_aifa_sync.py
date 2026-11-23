def parse_aifa_csv(csv_text):
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        return [], ""

    header_idx = 0
    best_delim = ";"
    tokens = ("AIC", "MEDICINALE", "DENOMINAZIONE", "PREZZO", "CONFEZIONE", "PRINCIPIO", "TITOLARE", "CARENZA", "INIZIO")
    for i, ln in enumerate(lines[:25]):
        u = ln.replace(".", "").strip().upper()
        if u.startswith(("NB:", "NB ", "NOTA", "AVVISO", "ELENCO", "AGGIORN")):
            continue
        sc = ln.count(";")
        cm = ln.count(",")
        delim = ";" if sc >= cm else ","
        parts = [p.strip() for p in ln.split(delim)]
        matched = sum(1 for tok in tokens if tok in u)
        if len(parts) >= 3 and matched >= 2 and len(parts[0]) < 50:
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
