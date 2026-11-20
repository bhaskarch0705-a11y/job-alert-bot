#!/usr/bin/env python3
"""
Job alert bot.

Checks company career feeds (Greenhouse, Lever, Ashby, Workday, Oracle), keeps only
fresh postings that fit your titles and experience and do not say "no
sponsorship", gives each one a fit score out of 100, and sorts it into a tier:

  Tier 1  your domain (banking, insurance, healthcare) + your tools: emailed right away
  Tier 2  finance / operations / revenue / pricing work, or a strong tools match
          in another domain: sent in a digest at the times set in config.json
  Tier 3  weak fit: dropped

Usage:
  python job_bot.py              normal run
  python job_bot.py --check      test every company feed, print job counts
  python job_bot.py --dry-run    score every live job, print tiers, send no email
"""
import html
import json
import os
import random
import threading
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SEEN_PATH = ROOT / "seen.json"
DIGEST_PATH = ROOT / "digest.json"
HEALTH_PATH = ROOT / "health.json"
H1B_PATH = ROOT / "h1b.json"
H1B = json.loads(H1B_PATH.read_text()) if H1B_PATH.exists() else {}
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job alert bot)", "Accept": "application/json"}
WORKERS = 12
WORKDAY_GATE = threading.BoundedSemaphore(4)
NOW = datetime.now(timezone.utc)
LOCAL_TZ = ZoneInfo(CONFIG.get("timezone", "America/Chicago"))

# ---------- fetching ----------

def fetch_json(url, payload=None):
    data = None
    headers = dict(HEADERS)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers)
    # Workday rate-limits (HTTP 429) when many tenants on one cluster are hit at once,
    # so Workday calls share a small pool and any 429/5xx is retried with backoff.
    gate = WORKDAY_GATE if "myworkdayjobs.com" in url else nullcontext()
    for attempt in range(5):
        try:
            with gate:
                with urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        time.sleep(3 * 2 ** attempt + random.random() * 3)


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def ms_to_text(ms):
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def iso_age(s):
    """Days since an ISO timestamp, or None if it cannot be read."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (NOW - dt).total_seconds() / 86400


def workday_age(s):
    """Workday shows 'Posted Today', 'Posted Yesterday', 'Posted 3 Days Ago', 'Posted 30+ Days Ago'."""
    s = (s or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", s)
    return int(m.group(1)) if m else None


def from_greenhouse(c):
    slug = c["slug"]
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"gh:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "age": iso_age(j.get("first_published") or j.get("updated_at")),
            "text": strip_html(j.get("content", "")),
            "pay": "",
        }


def from_lever(c):
    slug = c["slug"]
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    for j in data:
        parts = [j.get("descriptionPlain", ""), j.get("additionalPlain", "")]
        for block in j.get("lists", []):
            parts.append(block.get("text", ""))
            parts.append(strip_html(block.get("content", "")))
        pay = ""
        sr = j.get("salaryRange")
        if sr and sr.get("min"):
            pay = f"${sr['min']:,} - ${sr.get('max', sr['min']):,}"
        created = j.get("createdAt")
        yield {
            "id": f"lv:{slug}:{j['id']}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "age": (time.time() * 1000 - created) / 86400000 if created else None,
            "text": " ".join(p for p in parts if p),
            "pay": pay,
        }


def from_ashby(c):
    slug = c["slug"]
    data = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )
    for j in data.get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        yield {
            "id": f"ab:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "location": j.get("location", ""),
            "age": iso_age(j.get("publishedAt")),
            "text": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "pay": comp,
        }


def from_workday(c):
    """Workday career sites. Searches a few role keywords, then loads the full
    description later, only for jobs that already pass the cheap checks."""
    host, tenant, site = c["host"], c["tenant"], c["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    terms = CONFIG.get("workday_search_terms", ["data analyst", "data engineer"])
    pages = int(CONFIG.get("workday_pages_per_term", 3))
    found = {}
    for term in terms:
        for page in range(pages):
            data = fetch_json(f"{base}/jobs", {
                "appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term,
            })
            postings = data.get("jobPostings") or []
            for p in postings:
                path = p.get("externalPath")
                if path and path not in found:
                    found[path] = p
            if len(postings) < 20:
                break
    for path, p in found.items():
        req_id = (p.get("bulletFields") or [path])[0]
        yield {
            "id": f"wd:{tenant}:{req_id}",
            "title": p.get("title", ""),
            "url": f"https://{host}/{site}{path}",
            "location": p.get("locationsText", ""),
            "age": workday_age(p.get("postedOn")),
            "text": "",
            "pay": "",
            "detail_url": f"{base}{path}",
        }


def from_oracle(c):
    """Oracle Recruiting Cloud career sites (JPMorgan Chase and others). Results come
    newest first, so paging stops once postings get older than max_age_days."""
    host, site = c["host"], c["site"]
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    terms = CONFIG.get("oracle_search_terms") or CONFIG.get("workday_search_terms", ["data analyst"])
    pages = int(CONFIG.get("oracle_pages_per_term", 4))
    max_age = float(CONFIG["filters"].get("max_age_days", 2))
    found = {}
    for term in terms:
        for page in range(pages):
            url = (f"{base}?onlyData=true&expand=requisitionList.secondaryLocations"
                   f"&finder=findReqs;siteNumber={site},limit=25,offset={page * 25},"
                   f"keyword={quote(chr(34) + term + chr(34))},sortBy=POSTING_DATES_DESC")
            items = fetch_json(url).get("items") or [{}]
            reqs = items[0].get("requisitionList") or []
            for r in reqs:
                found.setdefault(r["Id"], r)
            ages = [iso_age(r.get("PostedDate")) for r in reqs]
            if len(reqs) < 25 or all(a is not None and a > max_age for a in ages):
                break
    for rid, r in found.items():
        if r.get("PrimaryLocationCountry") not in (None, "", "US"):
            continue
        text = " ".join(strip_html(r.get(k) or "") for k in
                        ("ShortDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
        yield {
            "id": f"or:{host.split('.')[0]}:{rid}",
            "title": r.get("Title", ""),
            "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
            "location": r.get("PrimaryLocation", "") + (" (" + str(r["WorkplaceType"]) + ")" if r.get("WorkplaceType") else ""),
            "age": iso_age(r.get("PostedDate")),
            "text": text,
            "pay": "",
        }


SOURCES = {
    "oracle": from_oracle,
    "greenhouse": from_greenhouse,
    "lever": from_lever,
    "ashby": from_ashby,
    "workday": from_workday,
}


def fetch_company(company):
    jobs = []
    for job in SOURCES[company["ats"]](company):
        job["company"] = company["name"]
        job["priority_company"] = bool(company.get("priority"))
        job["gem"] = bool(company.get("gem"))
        jobs.append(job)
    return jobs


def load_detail(job):
    """Fill in the description for feeds (Workday) that list jobs without it."""
    if job.get("text") or not job.get("detail_url"):
        return
    try:
        info = fetch_json(job["detail_url"]).get("jobPostingInfo") or {}
        job["text"] = strip_html(info.get("jobDescription", ""))
        if info.get("location"):
            job["location"] = info["location"]
        if info.get("externalUrl"):
            job["url"] = info["externalUrl"]
    except Exception:
        job["text"] = ""

# ---------- filtering and scoring ----------

F = CONFIG["filters"]
S = CONFIG["scoring"]
TITLE_INCLUDE = re.compile(F["title_include"], re.I)
TITLE_EXCLUDE = re.compile(F["title_exclude"], re.I)
LOCATION_EXCLUDE = re.compile(F["location_exclude"], re.I) if F.get("location_exclude") else None
NO_SPONSOR = re.compile("|".join(F["no_sponsorship_phrases"]), re.I)
MAX_AGE = float(F.get("max_age_days", 2))
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*years", re.I)
YEARS_EXP = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*years?\b[^.;\n]{0,60}?\bexperience", re.I)
PAY = re.compile(
    r"\$\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?(?:\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?)?"
)


def kw_regex(words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


CORE_DOMAIN = kw_regex(S["core_domain_keywords"])
CORE_TITLE = kw_regex(S["core_title_only_keywords"])
SECOND_DOMAIN = kw_regex(S["second_domain_keywords"])
SECOND_TITLE = kw_regex(S["second_title_only_keywords"])
TOOLS = kw_regex(S["tools"])
TITLE_CORE = re.compile(S["title_core"], re.I)
TOOL_NAMES = {"powerbi": "Power BI", "pyspark": "Spark", "elt": "ETL", "datamodeling": "Data modeling",
              "datamodelling": "Data modeling", "bigquery": "BigQuery", "dbt": "dbt"}


def min_years(text):
    # Prefer "N years ... experience" phrases and take the highest one,
    # so "3+ yrs Python, 8+ years of experience" counts as 8.
    nums = [int(n) for n in YEARS_EXP.findall(text) if 0 < int(n) <= 15]
    if nums:
        return max(nums)
    nums = [int(n) for n in YEARS.findall(text) if 0 < int(n) <= 15]
    return min(nums) if nums else None


def tool_name(s):
    k = s.lower().replace(" ", "")
    return TOOL_NAMES.get(k, k.upper() if len(k) <= 4 else k.title())


def distinct(regex, text):
    return sorted({m.group(0).lower() for m in regex.finditer(text)})


EXCLUDE_WORDS = re.compile(r"\b(exclud\w*|not (?:able|eligible|open)|unable|cannot|can't|do not|does not|"
                           r"not currently|must not|may not|except|exception|outside of|restrict\w*|prohibit\w*)", re.I)
ONLY_STATES = re.compile(r"\b(?:only|currently)\b[^.]{0,40}\b(?:hire|hiring|employ|accept\w*|consider\w*|"
                         r"located|reside|residents?)\b[^.]{0,80}\bstates?\b", re.I)
STATE_CODE = re.compile(r"\b[A-Z]{2}\b")
MISSOURI = re.compile(r"\bMissouri\b|\bMO\b")
TEXAS = re.compile(r"\bTexas\b|\bTX\b")


def state_note(job):
    """For remote jobs: warn when Missouri is excluded, and say whether Texas works."""
    if job.get("mode") != "Remote":
        return ""
    mo_out = tx_out = False
    for s in re.split(r"(?<=[.!?;])\s+", job["text"]):
        listed = len(STATE_CODE.findall(s)) >= 3 or re.search(r"\b(?:Alabama|Arizona|Colorado|Texas|"
                                                               r"Florida|Missouri|Kansas)\b", s)
        if not listed:
            continue
        if EXCLUDE_WORDS.search(s) and MISSOURI.search(s):
            mo_out = True
            tx_out = tx_out or bool(TEXAS.search(s))
        elif ONLY_STATES.search(s) and not MISSOURI.search(s):
            mo_out = True
            tx_out = tx_out or not TEXAS.search(s)
    if not mo_out:
        return ""
    if tx_out:
        return "Remote, but Missouri and Texas look excluded: check the state list"
    return "Remote, but Missouri looks excluded: apply with your Texas address"


def h1b_label(company):
    info = (H1B.get("companies") or {}).get(company)
    if info is None:
        return ""
    period = H1B.get("period", "")
    if not info.get("data_roles"):
        return f"H-1B: no data-role filings found ({period})"
    return f"H-1B: {info['data_roles']} data-role filings, {info['all_roles']} total ({period})"


def title_key(job):
    t = re.sub(r"[^a-z0-9 ]", " ", job["title"].lower())
    t = re.sub(r"\b(?:req|r|id)\s*\d+\b|\b\d{4,}\b", " ", t)
    return "t:" + job["company"].lower() + "|" + " ".join(t.split())


REMOTE_TEXT = re.compile(r"\b(fully remote|100% remote|remote[- ]first|remote position|remote role|"
                         r"work from home|work from anywhere)\b", re.I)


def work_mode(job):
    where = (job.get("location") or "") + " " + job["title"]
    if re.search(r"\bhybrid\b", where, re.I):
        return "Hybrid"
    if re.search(r"\bremote\b", where, re.I):
        return "Remote"
    if re.search(r"\bhybrid\b", job["text"], re.I):
        return "Hybrid"
    if REMOTE_TEXT.search(job["text"]):
        return "Remote"
    return "On-site"


def score_job(job):
    """Fill job['score'], job['tier'] and job['why'] (a short explanation)."""
    title, text = job["title"], job["text"]
    why = []

    # Words like "insurance" or "healthcare" show up in almost every benefits section,
    # so they only count in the title; the job text needs 2+ specific domain terms.
    core_title = distinct(CORE_DOMAIN, title) or distinct(CORE_TITLE, title)
    core_text = distinct(CORE_DOMAIN, text)
    second_text = distinct(SECOND_DOMAIN, text)
    second = (distinct(SECOND_DOMAIN, title) or distinct(SECOND_TITLE, title)
              or (second_text if len(second_text) >= 2 else []))
    if job["priority_company"] or core_title or len(core_text) >= 2:
        domain, dpts = "core", S["points"]["core_domain"]
        label = "domain company" if job["priority_company"] else ", ".join((core_title or core_text)[:3])
        why.append(f"Domain +{dpts} ({label})")
    elif second:
        domain, dpts = "second", S["points"]["second_domain"]
        why.append(f"Fin/ops/revenue +{dpts} ({', '.join(second[:3])})")
    else:
        domain, dpts = "other", 0

    tools = sorted({tool_name(t) for t in distinct(TOOLS, title + " " + text)})
    tpts = min(len(tools) * S["points"]["per_tool"], S["points"]["tools_max"])
    if tools:
        why.append(f"Tools +{tpts} ({', '.join(tools[:6])})")

    tl = S["points"]["title_core"] if TITLE_CORE.search(title) else S["points"]["title_other"]
    why.append(f"Title +{tl}")

    yrs = job.get("years")
    if yrs is None:
        epts = S["points"]["exp_unstated"]
        why.append(f"Exp +{epts} (not stated)")
    else:
        epts = S["points"]["exp_fit"]
        why.append(f"Exp +{epts} (asks {yrs} yrs)")

    score = dpts + tpts + tl + epts
    if job.get("gem"):
        score += S["points"].get("hidden_gem", 10)
        why.append(f"Hidden gem +{S['points'].get('hidden_gem', 10)}")
    job["mode"] = work_mode(job)
    job["note"] = state_note(job)
    job["h1b"] = h1b_label(job["company"])
    if domain == "core" and tpts >= S["tier1_min_tool_points"] and score >= S["tier1_min_score"]:
        tier = 1
    elif score >= S["tier2_min_score"]:
        tier = 2
    else:
        tier = 3
    job.update(score=score, tier=tier, domain=domain, tools=tools, why=" | ".join(why))


def check_job(job):
    """Return (keep: bool, reason: str). Cheap checks first, description last."""
    if not TITLE_INCLUDE.search(job["title"]):
        return False, "title"
    if TITLE_EXCLUDE.search(job["title"]):
        return False, "seniority"
    if job.get("age") is not None and job["age"] > MAX_AGE:
        return False, "old posting"
    if LOCATION_EXCLUDE and LOCATION_EXCLUDE.search(job["location"] or ""):
        return False, "location"
    load_detail(job)
    if LOCATION_EXCLUDE and LOCATION_EXCLUDE.search(job["location"] or ""):
        return False, "location"
    m = NO_SPONSOR.search(job["text"])
    if m:
        return False, f"no sponsorship: '{m.group(0)}'"
    yrs = min_years(job["text"])
    job["years"] = yrs
    if yrs is not None and yrs > F["max_min_years"]:
        return False, f"asks {yrs}+ years"
    if not job["pay"]:
        p = PAY.search(job["text"])
        job["pay"] = p.group(0) if p else ""
    score_job(job)
    if job["tier"] == 3 and job["score"] < S.get("lower_fit_min", 30):
        return False, f"low fit ({job['score']})"
    return True, "ok"

# ---------- state ----------

def load_seen():
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text())
    return None


def save_seen(seen):
    cutoff = (NOW - timedelta(days=120)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))


def load_digest():
    if DIGEST_PATH.exists():
        return json.loads(DIGEST_PATH.read_text())
    return {"pending": [], "last_slot": ""}


def save_digest(d):
    DIGEST_PATH.write_text(json.dumps(d, indent=0))


def current_slot():
    """Latest digest time (local) that has already passed, as 'YYYY-MM-DD-HH'."""
    local = NOW.astimezone(LOCAL_TZ)
    hours = sorted(int(h) for h in CONFIG.get("digest_hours_local", [8, 16]))
    past = [h for h in hours if h <= local.hour]
    if past:
        return f"{local.date().isoformat()}-{past[-1]:02d}"
    return f"{(local.date() - timedelta(days=1)).isoformat()}-{hours[-1]:02d}"

# ---------- email ----------

def age_text(a):
    if a is None:
        return ""
    return "within 24h" if a < 1 else f"{int(a)} day(s) ago"


BANDS = [(90, "90+"), (80, "80-89"), (70, "70-79"), (60, "60-69"), (50, "50-59"), (0, "under 50")]


def band(score):
    return next(label for low, label in BANDS if score >= low)


def job_html(j):
    color = "#b00020" if j["tier"] == 1 else "#555"
    gem = " <b style='color:#00796b'>HIDDEN GEM</b>" if j.get("gem") else ""
    bits = [f"<span style='color:{color}'><b>[{j['score']}]</b></span> "
            f"<b><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a></b>{gem}",
            f"{html.escape(j['company'])} | {html.escape(j['location'] or 'Location not listed')}"
            + f" | {j.get('mode', 'On-site')}"
            + (f" | Posted {age_text(j.get('age'))}" if j.get("age") is not None else "")
            + (f" | Pay: {html.escape(j['pay'])}" if j.get("pay") else ""),
            f"<span style='color:#777'>{html.escape(j['why'])}</span>"]
    if j.get("h1b"):
        bits.append(f"<span style='color:#1565c0'>{html.escape(j['h1b'])}</span>")
    if j.get("note"):
        bits.append(f"<b style='color:#e65100'>{html.escape(j['note'])}</b>")
    return "<p>" + "<br>".join(bits) + "</p>"


def send_email(subject, jobs, intro, extra_note="", low_jobs=None):
    body = f"<p>{intro}</p>"
    for _, label in BANDS:
        group = [j for j in jobs if band(j["score"]) == label]
        if group:
            body += f"<h3>Score {label} ({len(group)})</h3>" + "".join(job_html(j) for j in group)
    if extra_note:
        body += f"<p><i>{extra_note}</i></p>"
    if low_jobs:
        body += ("<h3 style='color:#777'>Lower fit: good title, fewer tools or outside your "
                 "domains</h3>") + "".join(job_html(j) for j in low_jobs)
    body += ("<p style='color:#777'>Score = domain (40 core, 20 finance/ops/revenue/pricing) + "
             "tools (5 each, max 30) + title (20 core, 10 other) + experience fit (10, or 5 if "
             "not stated), +10 for a hidden gem. Postings saying no sponsorship were skipped; "
             "still confirm with the "
             "recruiter.</p>")
    best = max(j["score"] for j in (jobs or low_jobs))
    deliver(f"[{band(best)}] {subject}", body)


def deliver(subject, body):
    user = os.environ["EMAIL_USER"]
    pwd = os.environ["EMAIL_APP_PASSWORD"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("EMAIL_TO", user)
    msg.set_content("Open this email in HTML view.")
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def slim(jobs):
    """Drop the long description before saving jobs that wait for the digest."""
    return [{k: v for k, v in j.items() if k not in ("text", "detail_url")} for j in jobs]


def top(jobs, n):
    """Best n jobs, at most max_per_company_per_email from any one company, so one big
    employer (CVS, Centene...) cannot fill the whole email. The rest is returned."""
    per = int(CONFIG.get("max_per_company_per_email", 4))
    picked, rest, count = [], [], {}
    for j in sorted(jobs, key=lambda j: (-j["score"], j["company"])):
        if len(picked) < n and count.get(j["company"], 0) < per:
            picked.append(j)
            count[j["company"]] = count.get(j["company"], 0) + 1
        else:
            rest.append(j)
    return picked, rest


def short_names(jobs):
    return ", ".join(dict.fromkeys(j["company"] for j in jobs))[:100]

# ---------- weekly health report ----------

def week_key():
    y, w, _ = NOW.astimezone(LOCAL_TZ).isocalendar()
    return f"{y}-W{w:02d}"


def new_health():
    return {"week": week_key(), "runs": 0, "errors": {}, "empty": {}, "alerts": {},
            "tier1": 0, "tier2": 0, "lower": 0, "reposts": 0}


def load_health():
    if HEALTH_PATH.exists():
        return json.loads(HEALTH_PATH.read_text())
    return new_health()


def bump(d, k, n=1):
    d[k] = d.get(k, 0) + n


def health_report(h, n_companies):
    runs = max(h["runs"], 1)
    top_cos = sorted(h["alerts"].items(), key=lambda kv: -kv[1])[:15]
    broken = sorted(h["errors"].items(), key=lambda kv: -kv[1])
    empty = sorted(c for c, n in h["empty"].items() if n >= runs)
    total = h["tier1"] + h["tier2"] + h["lower"]

    def li(rows):
        return "<ul>" + "".join(f"<li>{html.escape(str(r))}</li>" for r in rows) + "</ul>"

    body = (f"<h3>Week {h['week']}</h3><p>{h['runs']} runs across {n_companies} companies. "
            f"{total} matches: {h['tier1']} Tier 1, {h['tier2']} Tier 2, {h['lower']} lower fit. "
            f"{h['reposts']} reposts skipped.</p>")
    body += "<h4>Most matches by company</h4>"
    body += li(f"{c}: {n}" for c, n in top_cos) if top_cos else "<p>None.</p>"
    body += "<h4>Feeds that failed</h4>"
    body += (li(f"{c}: failed {n} of {h['runs']} runs" for c, n in broken) if broken
             else "<p>None. Every feed worked on every run.</p>")
    if empty:
        body += ("<h4>Feeds that returned 0 jobs all week</h4><p>Could be a hiring freeze or a "
                 "changed job board.</p>" + li(empty))
    if H1B.get("period"):
        body += (f"<p style='color:#777'>H-1B data: {html.escape(H1B['period'])}, "
                 f"updated {html.escape(H1B.get('updated', ''))}.</p>")
    deliver(f"[Weekly] Job bot health: {total} matches, {len(broken)} feed problem(s)", body)


# ---------- main ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    companies = CONFIG["companies"]
    cap = int(CONFIG.get("max_jobs_per_email", 15))
    all_jobs, errors = [], []

    def grab(c):
        try:
            return c, fetch_company(c), None
        except Exception as e:  # one broken feed must not stop the rest
            return c, [], e

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(grab, companies))
    health = load_health()
    for c, jobs, err in results:
        label = c.get("slug") or c.get("site", "")
        if err is None:
            all_jobs.extend(jobs)
            if not jobs:
                bump(health["empty"], c["name"])
            if mode == "--check":
                print(f"OK   {c['name']:<32} {c['ats']:<10} {len(jobs)} jobs")
        else:
            errors.append(f"{c['name']} ({c['ats']}:{label}): {err}")
            bump(health["errors"], c["name"])
            if mode == "--check":
                print(f"FAIL {c['name']:<32} {c['ats']:<10} {err} (wrong slug or site down?)")
    if mode == "--check":
        return

    seen = load_seen() or {}
    if mode == "--dry-run":
        to_check = all_jobs
    else:
        to_check = [j for j in all_jobs if j["id"] not in seen]

    with ThreadPoolExecutor(WORKERS) as pool:
        verdicts = list(pool.map(check_job, to_check))
    matches = [j for j, (keep, _) in zip(to_check, verdicts) if keep]
    reposts = 0
    if mode != "--dry-run":
        # Same company + same title seen live in the last 30 days = a repost, not a new job.
        cutoff = (NOW - timedelta(days=int(CONFIG.get("repost_days", 30)))).date().isoformat()
        kept, this_run = [], set()
        for j in sorted(matches, key=lambda j: -j["score"]):
            k = title_key(j)
            if seen.get(k, "") >= cutoff or k in this_run:
                reposts += 1
                continue
            this_run.add(k)
            kept.append(j)
        matches = kept
    t1 = [j for j in matches if j["tier"] == 1]
    t2 = [j for j in matches if j["tier"] == 2]
    t3 = [j for j in matches if j["tier"] == 3]
    dropped = sum(1 for _, why in verdicts if why.startswith("low fit"))

    print(f"Fetched {len(all_jobs)} jobs from {len(companies) - len(errors)} companies; "
          f"checked {len(to_check)} unseen; tier 1: {len(t1)}, tier 2: {len(t2)}, lower fit: {len(t3)}, "
          f"dropped as low fit: {dropped}, reposts skipped: {reposts}.")
    for e in errors:
        print("Feed error:", e)

    if mode == "--dry-run":
        for j in sorted(matches, key=lambda j: -j["score"]):
            print(f"- T{j['tier']} [{j['score']}] {j['title']} | {j['company']} | "
                  f"{j['location']} | {j['why']} | {j['url']}")
        return

    today = NOW.date().isoformat()
    for j in all_jobs:  # record every live job with today's date (old ones get pruned later)
        seen[j["id"]] = today
        seen[title_key(j)] = today

    digest = load_digest()
    if t1:
        send_now, overflow = top(t1, cap)
        send_email(f"PRIORITY {len(t1)} new | top: {send_now[0]['company']} "
                   f"[{send_now[0]['score']}] | {short_names(send_now)}",
                   send_now, "New Tier 1 postings (your domain and your tools), best fit first:")
        digest["pending"].extend(slim(overflow))
        print(f"Emailed {len(send_now)} tier 1 job(s); {len(overflow)} overflow to digest.")
    digest["pending"].extend(slim(t2))
    digest.setdefault("pending_low", []).extend(slim(t3))

    slot = current_slot()
    if not digest.get("last_slot"):
        digest["last_slot"] = slot  # first run: start the digest clock without sending
    elif (digest["pending"] or digest["pending_low"]) and digest["last_slot"] != slot:
        send_now, rest = top(digest["pending"], cap)
        low_now, low_rest = top(digest["pending_low"], int(CONFIG.get("max_lower_fit_per_digest", 10)))
        note = f"+{len(rest)} more role(s) held for the next digest." if rest else ""
        send_email(f"Digest: {len(digest['pending'])} role(s) + {len(digest['pending_low'])} lower fit"
                   f" | {short_names(send_now or low_now)}",
                   send_now,
                   "Tier 2 since the last digest (finance, operations, revenue or pricing "
                   "roles, and strong tool matches in other domains), best fit first:", note, low_now)
        print(f"Sent digest with {len(send_now)} job(s) + {len(low_now)} lower fit.")
        digest = {"pending": rest[:int(CONFIG.get("digest_carryover", 40))], "pending_low": [], "last_slot": slot}
    save_digest(digest)
    save_seen(seen)

    health["runs"] += 1
    health["tier1"] += len(t1)
    health["tier2"] += len(t2)
    health["lower"] += len(t3)
    health["reposts"] += reposts
    for j in matches:
        bump(health["alerts"], j["company"])
    local = NOW.astimezone(LOCAL_TZ)
    if health["week"] != week_key() and local.weekday() == 0 and local.hour >= 7:
        health_report(health, len(companies))
        print("Sent weekly health report.")
        health = new_health()
    HEALTH_PATH.write_text(json.dumps(health, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
