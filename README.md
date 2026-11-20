# Job Alert Bot

Checks company career feeds every 10 minutes and emails you only NEW postings that:

- match your titles (data engineer, data analyst, analytics engineer, BI engineer/analyst)
- are not senior, staff, principal, lead or manager roles
- do not ask for more than 5 years minimum
- do not say "no sponsorship", "without sponsorship", "US citizen required" or "clearance required"
- are not located outside the US

Healthcare, insurance and banking roles are listed first in each email.

## Setup (about 10 minutes)

### 1. Get a Gmail app password
1. Turn on 2-Step Verification on your Google account (required for app passwords).
2. Go to https://myaccount.google.com/apppasswords
3. Create one named "job bot". Copy the 16-character password.

### 2. Create the GitHub repo
1. Sign in at https://github.com and click **New repository**.
2. Name it `job-alert-bot`. Choose **Public** (see note below). Click **Create**.
3. Click **uploading an existing file** and upload `job_bot.py`, `config.json` and `README.md`.
4. For the workflow file, click **Add file > Create new file**, type the name
   `.github/workflows/job-bot.yml`, paste the contents of `job-bot.yml`, and commit.

### 3. Add your email secrets
Repo **Settings > Secrets and variables > Actions > New repository secret**. Add three:

| Name | Value |
|---|---|
| `EMAIL_USER` | your Gmail address |
| `EMAIL_APP_PASSWORD` | the 16-character app password |
| `EMAIL_TO` | where alerts go (can be the same Gmail) |

### 4. Turn it on
1. Go to the **Actions** tab and enable workflows if asked.
2. Click **job-bot > Run workflow** to run it once by hand.
3. You get a "Job bot is live" email with every matching role open right now.
   After that, you only get emails when something new appears.

## Public or private repo?
- **Public:** unlimited free GitHub Actions minutes. Your email and password stay hidden in
  Secrets. Only your company list and job IDs are visible.
- **Private:** free accounts get 2,000 minutes a month. Every 10 minutes uses about 4,300,
  so change the schedule in `job-bot.yml` to `"*/20 * * * *"` (every 20 minutes) to stay free.

## Adding companies
Open `config.json` and add a line to `companies`. Find the slug in the company's job URL:

| Career page URL looks like | ats | slug |
|---|---|---|
| `job-boards.greenhouse.io/definitivehc/...` | `greenhouse` | `definitivehc` |
| `jobs.lever.co/Trend-Health-Partners/...` | `lever` | `Trend-Health-Partners` |
| `jobs.ashbyhq.com/ramp/...` | `ashby` | `ramp` |

To test every slug, run the workflow once and open the log, or run `python job_bot.py --check`
on your computer. Any company marked FAIL has a wrong slug: fix it or remove it.

## Tuning
- Want senior roles too? Remove `senior|sr\.?|` from `title_exclude` in `config.json`.
- Allow more years? Change `max_min_years`.
- Feed errors for one company never stop the others.

## Notes
- GitHub can delay scheduled runs by a few minutes when busy. That is normal.
- GitHub pauses schedules on repos with no activity for 60 days. The bot commits
  `seen.json` regularly, so it stays active.
- The sponsorship filter only catches what the posting says. Always confirm with the recruiter.
