#!/usr/bin/env python3
"""
Job alert bot.

Checks company career feeds (Greenhouse, Lever, Ashby) and emails you
only NEW jobs that match your titles, skip senior roles, and do not
say "no sponsorship".

Usage:
  python job_bot.py            normal run (emails new matches)
  python job_bot.py --check    test every company slug, print job counts
  python job_bot.py --dry-run  run filters, print matches, send no email
"""
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
SEEN_PATH = ROOT / "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job alert bot)"}


# ---------- fetching ----------

def fetch_json(url):
    req = Request(url, headers=HEADERS)
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


def from_greenhouse(slug):
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


def from_lever(slug):
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


def from_ashby(slug):
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


SOURCES = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby}


def fetch_company(company):
    fn = SOURCES[company["ats"]]
    jobs = []
    for job in fn(company["slug"]):
        job["company"] = company["name"]
        jobs.append(job)
    return jobs


# ---------- filtering ----------

F = CONFIG["filters"]
TITLE_INCLUDE = re.compile(F["title_include"], re.I)
TITLE_EXCLUDE = re.compile(F["title_exclude"], re.I)
LOCATION_EXCLUDE = re.compile(F["location_exclude"], re.I) if F.get("location_exclude") else None
NO_SPONSOR = re.compile("|".join(F["no_sponsorship_phrases"]), re.I)
DOMAIN = re.compile("|".join(F["domain_keywords"]), re.I)
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


def check_job(job):
    """Return (keep: bool, reason: str)."""
    if not TITLE_INCLUDE.search(job["title"]):
        return False, "title"
    if TITLE_EXCLUDE.search(job["title"]):
        return False, "seniority"
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
    job["domain_hit"] = sorted({d.group(0).lower() for d in DOMAIN.finditer(job["text"])})[:4]
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
    bits = [f"<b><a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a></b>",
            f"{html.escape(j['company'])} | {html.escape(j['location'] or 'Location not listed')}"]
    extra = []
    if j.get("pay"):
        extra.append(f"Pay: {html.escape(j['pay'])}")
    if j.get("years") is not None:
        extra.append(f"Asks {j['years']}+ yrs")
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
    jobs = sorted(jobs, key=lambda j: (not j.get("domain_hit"), j["company"]))
    body = f"<p>{intro}</p>" + "".join(job_html(j) for j in jobs)
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


# ---------- main ----------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    companies = CONFIG["companies"]
    all_jobs, errors = [], []
    for c in companies:
        try:
            jobs = fetch_company(c)
            all_jobs.extend(jobs)
            if mode == "--check":
                print(f"OK   {c['name']:<32} {c['ats']:<10} {len(jobs)} jobs")
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as e:
            errors.append(f"{c['name']} ({c['ats']}:{c['slug']}): {e}")
            if mode == "--check":
                print(f"FAIL {c['name']:<32} {c['ats']:<10} {e}  (wrong slug?)")
    if mode == "--check":
        return

    matches = []
    for j in all_jobs:
        keep, why = check_job(j)
        if keep:
            matches.append(j)
    print(f"Fetched {len(all_jobs)} jobs from {len(companies) - len(errors)} companies; "
          f"{len(matches)} match your filters.")
    for e in errors:
        print("Feed error:", e)

    if mode == "--dry-run":
        for j in matches:
            print(f"- {j['title']} | {j['company']} | {j['location']} | {j['pay']} | {j['url']}")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    new = [j for j in matches if j["id"] not in seen]
    for j in all_jobs:  # record every live job with today's date (old ones get pruned later)
        seen[j["id"]] = today

    if first_run:
        send_email(
            f"Job bot is live: tracking {len(companies)} companies, {len(matches)} roles open now",
            matches,
            "Here is your baseline of roles already open. From now on you will only get "
            "emails for brand new postings.",
        )
    elif new:
        names = ", ".join(sorted({j["company"] for j in new}))[:120]
        send_email(f"{len(new)} new data role(s): {names}", new,
                   "New postings found in the last check:")
        print(f"Emailed {len(new)} new job(s).")
    else:
        print("No new matching jobs.")
    save_seen(seen)


if __name__ == "__main__":
    main()
