#!/usr/bin/env python3
"""
Job alert bot.

Checks company career feeds (Greenhouse, Lever, Ashby, Workday) and emails you
only NEW jobs that match your titles, skip too-senior roles, and do not say
"no sponsorship". Jobs in your priority domains (banking, insurance,
healthcare, fintech) that also match your skills are flagged PRIORITY and
listed first.

Usage:
  python job_bot.py              normal run (emails new matches)
  python job_bot.py --check      test every company feed, print job counts
  python job_bot.py --dry-run    run filters on every job, print matches, send no email
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

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SEEN_PATH = ROOT / "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job alert bot)", "Accept": "application/json"}
WORKERS = 12

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


def from_greenhouse(c):
    slug = c["slug"]
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"gh:{slug}:{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "posted": j.get("first_published") or j.get("updated_at", ""),
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
        yield {
            "id": f"lv:{slug}:{j['id']}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "posted": ms_to_text(j.get("createdAt")),
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
            "posted": j.get("publishedAt", ""),
            "text": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "pay": comp,
        }


def from_workday(c):
    """Workday career sites. Searches a few role keywords, then loads the full
    description later, only for jobs whose title and location already pass."""
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
            "posted": p.get("postedOn", ""),
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

# ---------- filtering ----------

F = CONFIG["filters"]
TITLE_INCLUDE = re.compile(F["title_include"], re.I)
TITLE_EXCLUDE = re.compile(F["title_exclude"], re.I)
LOCATION_EXCLUDE = re.compile(F["location_exclude"], re.I) if F.get("location_exclude") else None
NO_SPONSOR = re.compile("|".join(F["no_sponsorship_phrases"]), re.I)
DOMAIN = re.compile("|".join(F["domain_keywords"]), re.I)
SKILLS = re.compile(r"\b(?:" + "|".join(F["skill_keywords"]) + r")\b", re.I)
MIN_SKILLS = int(F.get("priority_min_skills", 2))
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*years", re.I)
YEARS_EXP = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*years?\b[^.;\n]{0,60}?\bexperience", re.I)
PAY = re.compile(
    r"\$\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?(?:\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:,\d{3})+(?:\.\d{2})?)?"
)


def min_years(text):
    # Prefer "N years ... experience" phrases and take the highest one,
    # so "3+ yrs Python, 8+ years of experience" counts as 8.
    nums = [int(n) for n in YEARS_EXP.findall(text) if 0 < int(n) <= 15]
    if nums:
        return max(nums)
    nums = [int(n) for n in YEARS.findall(text) if 0 < int(n) <= 15]
    return min(nums) if nums else None


def norm_skill(s):
    s = s.lower().replace(" ", "")
    return {"powerbi": "Power BI", "pyspark": "Spark", "datamodeling": "data modeling",
            "datamodelling": "data modeling"}.get(s, s.upper() if len(s) <= 4 else s.title())


def check_job(job):
    """Return (keep: bool, reason: str)."""
    if not TITLE_INCLUDE.search(job["title"]):
        return False, "title"
    if TITLE_EXCLUDE.search(job["title"]):
        return False, "seniority"
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
    blob = job["title"] + " " + job["text"]
    job["domain_hit"] = sorted({d.group(0).lower() for d in DOMAIN.finditer(job["text"])})[:4]
    job["skills"] = sorted({norm_skill(s.group(0)) for s in SKILLS.finditer(blob)})
    in_domain = job["priority_company"] or bool(job["domain_hit"])
    job["priority"] = in_domain and len(job["skills"]) >= MIN_SKILLS
    return True, "ok"

# ---------- seen-jobs memory ----------

def load_seen():
    if SEEN_PATH.exists():
        return json.loads(SEEN_PATH.read_text())
    return None


def save_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))

# ---------- email ----------

def job_html(j):
    tag = "<span style='color:#b00020'><b>PRIORITY</b></span> " if j.get("priority") else ""
    bits = [f"{tag}<b><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a></b>",
            f"{html.escape(j['company'])} | {html.escape(j['location'] or 'Location not listed')}"]
    extra = []
    if j.get("pay"):
        extra.append(f"Pay: {html.escape(j['pay'])}")
    if j.get("years") is not None:
        extra.append(f"Asks {j['years']}+ yrs")
    if j.get("skills"):
        extra.append("Skills: " + html.escape(", ".join(j["skills"][:8])))
    if j.get("domain_hit"):
        extra.append("Domain: " + ", ".join(j["domain_hit"]))
    if j.get("posted"):
        extra.append(f"Posted: {html.escape(str(j['posted']))}")
    if extra:
        bits.append(" | ".join(extra))
    return "<p>" + "<br>".join(bits) + "</p>"


def send_email(subject, jobs, intro):
    user = os.environ["EMAIL_USER"]
    pwd = os.environ["EMAIL_APP_PASSWORD"]
    to = os.environ.get("EMAIL_TO", user)
    pri = sorted([j for j in jobs if j.get("priority")],
                 key=lambda j: (-len(j.get("skills", [])), j["company"]))
    rest = sorted([j for j in jobs if not j.get("priority")],
                  key=lambda j: (not (j["priority_company"] or j.get("domain_hit")), j["company"]))
    body = f"<p>{intro}</p>"
    if pri:
        body += (f"<h3 style='color:#b00020'>Priority matches ({len(pri)}): your domain "
                 f"and your skills</h3>") + "".join(job_html(j) for j in pri)
    if rest:
        body += f"<h3>Other matching roles ({len(rest)})</h3>" + "".join(job_html(j) for j in rest)
    body += ("<p style='color:#777'>The bot skipped postings that say no sponsorship. "
             "Still confirm sponsorship with the recruiter.</p>")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content("Open this email in HTML view to see the job list.")
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def subject_for(jobs, first_run, n_companies):
    pri = [j for j in jobs if j.get("priority")]
    if first_run:
        return (f"Job bot is live: tracking {n_companies} companies, "
                f"{len(jobs)} roles open now ({len(pri)} priority)")
    names = ", ".join(sorted({j["company"] for j in (pri or jobs)}))[:110]
    if pri:
        return f"PRIORITY: {len(pri)} domain match(es) + {len(jobs) - len(pri)} more new | {names}"
    return f"{len(jobs)} new data role(s): {names}"

# ---------- main ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    companies = CONFIG["companies"]
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

    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    if first_run or mode == "--dry-run":
        to_check = all_jobs
    else:
        to_check = [j for j in all_jobs if j["id"] not in seen]

    with ThreadPoolExecutor(WORKERS) as pool:
        verdicts = list(pool.map(check_job, to_check))
    matches = [j for j, (keep, _) in zip(to_check, verdicts) if keep]
    n_pri = sum(1 for j in matches if j.get("priority"))

    print(f"Fetched {len(all_jobs)} jobs from {len(companies) - len(errors)} companies; "
          f"checked {len(to_check)} unseen; {len(matches)} match ({n_pri} priority).")
    for e in errors:
        print("Feed error:", e)

    if mode == "--dry-run":
        for j in sorted(matches, key=lambda j: not j.get("priority")):
            flag = "PRIORITY " if j.get("priority") else ""
            print(f"- {flag}{j['title']} | {j['company']} | {j['location']} | "
                  f"{', '.join(j.get('skills', []))} | {j['url']}")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    for j in all_jobs:  # record every live job with today's date (old ones get pruned later)
        seen[j["id"]] = today

    if matches:
        intro = ("Here is your baseline of roles already open. From now on you will only get "
                 "emails for brand new postings." if first_run
                 else "New postings found in the last check:")
        send_email(subject_for(matches, first_run, len(companies)), matches, intro)
        print(f"Emailed {len(matches)} job(s), {n_pri} priority.")
    else:
        print("No new matching jobs.")
    save_seen(seen)


if __name__ == "__main__":
    main()
