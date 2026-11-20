#!/usr/bin/env python3
"""
Weekly H-1B sponsor lookup.

Downloads the US Department of Labor's LCA (H-1B) disclosure files for the
latest few quarters, counts certified H-1B filings for every company in
config.json, and writes h1b.json. job_bot.py shows the result as a label on
each alert (it does not change the score).

Matching is by employer name, so a company filing under a very different
legal name can show as "no filings found". Add "h1b_names": ["LEGAL NAME"]
to that company in config.json to fix it.
"""
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import openpyxl

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT / "h1b.json"
PAGE = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
UA = {"User-Agent": "Mozilla/5.0 (personal job alert bot; weekly H-1B lookup)"}
QUARTERS = int(CONFIG.get("h1b_quarters", 4))
DATA_ROLE = re.compile(
    r"data|analyst|analytics|business intelligence|\bBI\b|power ?bi|tableau|\bSQL\b|\bETL\b|reporting",
    re.I)
SUFFIXES = {"INC", "INCORPORATED", "LLC", "L L C", "LP", "LLP", "LTD", "CORP", "CORPORATION", "CO",
            "COMPANY", "COMPANIES", "GROUP", "HOLDINGS", "HOLDING", "SERVICES", "SERVICE", "NA", "N A",
            "NATIONAL ASSOCIATION", "USA", "US", "THE", "PBC", "PLLC", "FSB"}


def norm(name):
    s = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    s = re.sub(r"\b(?:NATIONAL ASSOCIATION|N A|L L C)\b", " ", s)
    words = [w for w in s.split() if w not in SUFFIXES]
    return " ".join(words)


def file_links():
    html = urlopen(Request(PAGE, headers=UA), timeout=60).read().decode("utf-8", "ignore")
    found = {}
    for href in re.findall(r'href="([^"]+LCA_Disclosure_Data_FY(\d{4})_Q(\d)\.xlsx)"', html, re.I):
        url, fy, q = href
        found[(int(fy), int(q))] = urljoin(PAGE, url)
    keys = sorted(found, reverse=True)[:QUARTERS]
    return [(k, found[k]) for k in keys]


def download(url, dest):
    with urlopen(Request(url, headers=UA), timeout=600) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def matchers():
    """Map each tracked company to the normalized employer names that count for it."""
    out = []
    for c in CONFIG["companies"]:
        explicit = bool(c.get("h1b_names"))
        for n in c.get("h1b_names") or [c["name"]]:
            key = norm(re.sub(r"\(.*?\)", " ", n))  # "Judi Health (Capital Rx)" -> "JUDI HEALTH"
            if key:
                out.append((key, c["name"], explicit))
    return out


def company_for(employer, rules, cache):
    if employer in cache:
        return cache[employer]
    e = norm(employer)
    hit = None
    for key, company, explicit in rules:
        # short names (Ro, Hex, Block...) must match exactly unless listed in h1b_names;
        # longer ones may be a prefix ("CAPITAL ONE" matches "CAPITAL ONE SERVICES")
        if e == key or ((explicit or len(key) > 6) and e.startswith(key + " ")):
            hit = company
            break
    cache[employer] = hit
    return hit


def count_file(path, rules, counts, cache):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip().upper() for h in next(rows)]
    col = {name: header.index(name) for name in
           ("CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE", "VISA_CLASS") if name in header}
    missing = {"CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE"} - set(col)
    if missing:
        raise RuntimeError(f"columns not found: {missing}; header starts {header[:12]}")
    n = 0
    for row in rows:
        status = str(row[col["CASE_STATUS"]] or "").strip().upper()
        if status != "CERTIFIED":
            continue
        if "VISA_CLASS" in col and "H-1B" not in str(row[col["VISA_CLASS"]] or "").upper():
            continue
        company = company_for(row[col["EMPLOYER_NAME"]], rules, cache)
        if not company:
            continue
        rec = counts.setdefault(company, {"all_roles": 0, "data_roles": 0})
        rec["all_roles"] += 1
        if DATA_ROLE.search(str(row[col["JOB_TITLE"]] or "")):
            rec["data_roles"] += 1
        n += 1
    wb.close()
    return n


def main():
    links = file_links()
    if not links:
        sys.exit("No LCA disclosure files found on the DOL page; layout may have changed.")
    labels = [f"FY{fy} Q{q}" for (fy, q), _ in links]
    period = f"{labels[-1]} to {labels[0]}" if len(labels) > 1 else labels[0]
    sig = hashlib.sha256(json.dumps([(c["name"], c.get("h1b_names")) for c in CONFIG["companies"]])
                         .encode()).hexdigest()[:12]
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    if prev.get("files") == labels and prev.get("sig") == sig and "--force" not in sys.argv:
        print("H-1B data already up to date:", period)
        return
    rules, cache, counts = matchers(), {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        for (fy, q), url in links:
            dest = Path(tmp) / f"lca_{fy}_{q}.xlsx"
            print("Downloading", url)
            download(url, dest)
            n = count_file(dest, rules, counts, cache)
            print(f"  FY{fy} Q{q}: {n} certified H-1B filings at tracked companies")
            dest.unlink()
    companies = {c["name"]: counts.get(c["name"], {"all_roles": 0, "data_roles": 0})
                 for c in CONFIG["companies"]}
    OUT.write_text(json.dumps({
        "period": period, "files": labels, "sig": sig,
        "updated": datetime.now(timezone.utc).date().isoformat(),
        "companies": companies}, indent=1, sort_keys=True))
    found = sum(1 for v in companies.values() if v["all_roles"])
    print(f"Wrote h1b.json: {found} of {len(companies)} companies have H-1B filings ({period}).")


if __name__ == "__main__":
    main()
