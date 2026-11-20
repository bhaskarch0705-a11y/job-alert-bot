#!/usr/bin/env python3
"""
Job alert bot.

Checks company career feeds (Greenhouse, Lever, Ashby, Workday), keeps only
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
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SEEN_PATH = ROOT / "seen.json"
DIGEST_PATH = ROOT / "digest.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job alert bot)", "Accept": "application/json"}
WORKERS = 12
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
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


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


SOURCES = {
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
    if job["tier"] == 3:
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


def job_html(j):
    color = "#b00020" if j["tier"] == 1 else "#555"
    bits = [f"<span style='color:{color}'><b>[{j['score']}]</b></span> "
            f"<b><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a></b>",
            f"{html.escape(j['company'])} | {html.escape(j['location'] or 'Location not listed')}"
            + (f" | Posted {age_text(j.get('age'))}" if j.get("age") is not None else "")
            + (f" | Pay: {html.escape(j['pay'])}" if j.get("pay") else ""),
            f"<span style='color:#777'>{html.escape(j['why'])}</span>"]
    return "<p>" + "<br>".join(bits) + "</p>"


def send_email(subject, jobs, intro, extra_note=""):
    user = os.environ["EMAIL_USER"]
    pwd = os.environ["EMAIL_APP_PASSWORD"]
    to = os.environ.get("EMAIL_TO", user)
    body = f"<p>{intro}</p>" + "".join(job_html(j) for j in jobs)
    if extra_note:
        body += f"<p><i>{extra_note}</i></p>"
    body += ("<p style='color:#777'>Score = domain (40 core, 20 finance/ops/revenue/pricing) + "
             "tools (5 each, max 30) + title (20 core, 10 other) + experience fit (10, or 5 if "
             "not stated). Postings saying no sponsorship were skipped; still confirm with the "
             "recruiter.</p>")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content("Open this email in HTML view to see the job list.")
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def slim(jobs):
    """Drop the long description before saving jobs that wait for the digest."""
    return [{k: v for k, v in j.items() if k not in ("text", "detail_url")} for j in jobs]


def top(jobs, n):
    jobs = sorted(jobs, key=lambda j: (-j["score"], j["company"]))
    return jobs[:n], jobs[n:]


def short_names(jobs):
    return ", ".join(dict.fromkeys(j["company"] for j in jobs))[:100]

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
    for c, jobs, err in results:
        label = c.get("slug") or c.get("site", "")
        if err is None:
            all_jobs.extend(jobs)
            if mode == "--check":
                print(f"OK   {c['name']:<32} {c['ats']:<10} {len(jobs)} jobs")
        else:
            errors.append(f"{c['name']} ({c['ats']}:{label}): {err}")
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
    t1 = [j for j in matches if j["tier"] == 1]
    t2 = [j for j in matches if j["tier"] == 2]
    dropped = sum(1 for _, why in verdicts if why.startswith("low fit"))

    print(f"Fetched {len(all_jobs)} jobs from {len(companies) - len(errors)} companies; "
          f"checked {len(to_check)} unseen; tier 1: {len(t1)}, tier 2: {len(t2)}, "
          f"dropped as low fit: {dropped}.")
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

    digest = load_digest()
    if t1:
        send_now, overflow = top(t1, cap)
        send_email(f"PRIORITY {len(t1)} new | top: {send_now[0]['company']} "
                   f"[{send_now[0]['score']}] | {short_names(send_now)}",
                   send_now, "New Tier 1 postings (your domain and your tools), best fit first:")
        digest["pending"].extend(slim(overflow))
        print(f"Emailed {len(send_now)} tier 1 job(s); {len(overflow)} overflow to digest.")
    digest["pending"].extend(slim(t2))

    slot = current_slot()
    if not digest.get("last_slot"):
        digest["last_slot"] = slot  # first run: start the digest clock without sending
    elif digest["pending"] and digest["last_slot"] != slot:
        send_now, rest = top(digest["pending"], cap)
        note = f"+{len(rest)} lower-scored role(s) not shown." if rest else ""
        send_email(f"Digest: {len(digest['pending'])} more data role(s) | {short_names(send_now)}",
                   send_now,
                   "Tier 2 since the last digest (finance, operations, revenue or pricing "
                   "roles, and strong tool matches in other domains), best fit first:", note)
        print(f"Sent digest with {len(send_now)} job(s).")
        digest = {"pending": [], "last_slot": slot}
    save_digest(digest)
    save_seen(seen)


if __name__ == "__main__":
    main()
