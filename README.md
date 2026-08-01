# job-watch

Polls the public Greenhouse, Rippling, and Ashby job board APIs on a schedule, opens a GitHub Issue for every new matching role with an automatic Real-Fit Score, and publishes a TMG-branded dashboard to GitHub Pages. Runs entirely on GitHub Actions. No third-party automation service.

Live dashboard: `https://rmallorybpc.github.io/job-watch/`

---

## Repo structure

```
job-watch/
├── .github/
│   └── workflows/
│       └── job-watch.yml          Scheduled run, label creation, state commit, Pages deploy
├── docs/                          Published to GitHub Pages via Actions artifact
│   ├── index.html                 TMG-branded dashboard, with a Real-Fit column and filter
│   ├── tmg.css                    Copy from external-hire-premium
│   └── data/
│       ├── jobs.json              Generated. Greenhouse log, dedup state, dashboard data.
│       ├── jobs_rippling.json     Generated. Same, for Rippling.
│       └── jobs_ashby.json        Generated. Same, for Ashby.
├── companies.csv                  Greenhouse target list, edited by hand
├── companies_rippling.csv         Rippling target list, edited by hand
├── companies_ashby.csv            Ashby target list, edited by hand
├── greenhouse_watch.py            Greenhouse watcher
├── rippling_watch.py              Rippling watcher
├── ashby_watch.py                 Ashby watcher
├── real_fit_profile.py            Real-Fit Score weights and keyword profile, edited by hand
├── real_fit_score.py              Shared Real-Fit Score engine, imported by all three watchers
├── TMG-BRAND-GUIDE.md             Copy from external-hire-premium
├── .gitignore
└── README.md
```

Two files are copied in from the `external-hire-premium` repo rather than created here: `TMG-BRAND-GUIDE.md` at the root and `tmg.css` inside `docs/`. The dashboard links `tmg.css` as a sibling file, so it must sit in `docs/`, not the root.

Each source keeps its own data file, its own company CSV, and its own dedup state. They share the filter pattern, the onsite-outside-Colorado rule where the source supports it, and the Real-Fit Score engine, but nothing about one source's data touches another's.

---

## How it works

1. Actions fires on cron, on push to `main`, or manually.
2. The `job-lead` label is created if it does not exist. Idempotent, so it is safe on every run.
3. Each watcher reads its own companies CSV and calls its source's public API.
4. Titles and locations are filtered against the keyword lists in each script. Where the source supports it, an onsite-outside-Colorado rule drops onsite roles outside Colorado; remote and hybrid roles pass regardless of city.
5. Anything not already logged for that source is new.
6. Real-Fit Score is computed for each new match against the weights and keyword profile in `real_fit_profile.py`.
7. Each new role opens a GitHub Issue labeled `job-lead`, with the Real-Fit Score breakdown, a JDR review checklist, and the full description in a collapsed block.
8. The issue URL is written back into the record so the dashboard can link to it.
9. All three data files are committed, then `docs/` is uploaded and deployed to Pages.

---

## Real-Fit Score

Every new match gets an automatic 0–100 score across seven weighted categories (domain alignment, seniority, role-type fit, compensation, remote eligibility, leadership signals, complexity), with a verdict of Apply, Optional, or Skip. A hit on a red-flag phrase forces Skip regardless of score.

This is keyword and phrase matching against the job title and description, not a judgment-based read of the JD. It approximates the Zapier-based Real-Fit rubric well enough to triage at a glance. It is not a substitute for reading the posting, and it is not the same tool as JDR.

**JDR stays separate and manual.** JDR is a deeper, hostile-assessment review Tracey runs herself in a chat against her full resume and credentials, for roles worth a closer look. The issue checklist still has a JDR checkbox for that purpose; job-watch does not compute a JDR score automatically.

**Where the score shows up:**

- **Issue body** — full breakdown, one row per category, with the reasoning behind each score and a note when a category was scored neutral because the source doesn't capture that data.
- **Dashboard** — a compact score-and-verdict badge in the table, plus a Real-Fit filter dropdown. The full breakdown is not repeated here; open the issue for that.

**Data availability by source.** Compensation scoring needs a salary figure; only Ashby captures one. Remote-eligibility scoring needs a workplace-type signal; Greenhouse and Rippling infer it from location text since neither exposes a real field, Ashby has a cleaner signal. Wherever a source doesn't have the input a category needs, that category scores neutral rather than being penalized, and the issue notes which categories were affected.

**Tuning it.** Edit `real_fit_profile.py`, not `real_fit_score.py`. Weights, the compensation floor, and every keyword list live there so retuning never touches the scoring logic itself.

---

## Setup

**1. Create the repo.** Public gives unlimited Actions minutes and a Pages URL under `rmallorybpc.github.io`. Private works but caps Actions at 2,000 minutes per month, which is far more than this uses.

**2. Add the files** in the structure above. `real_fit_profile.py` and `real_fit_score.py` must exist before any watcher's first run, since all three import `real_fit_score`, which imports `real_fit_profile`. Add both before committing the watcher files, or the run will fail on import.

**3. Copy the brand files.** From `external-hire-premium`, copy `TMG-BRAND-GUIDE.md` to the root and `tmg.css` to `docs/`.

**4. Enable write permissions.** Settings, Actions, General, Workflow permissions, select "Read and write permissions". Without this both the state commit and the label creation fail.

**5. Set Pages source to Actions.** Settings, Pages, Build and deployment, Source, select **GitHub Actions**. Do not select "Deploy from a branch". The workflow uploads `docs/` as an artifact and deploys it directly, so no branch or root folder is involved.

**6. Fill in the three company CSVs.**

`companies.csv` (Greenhouse):

```csv
company_name,board_token
Acme Corp,acmecorp
Widget Inc,widgetinc
```

`companies_rippling.csv` (Rippling):

```csv
company_name,board_id
Arine,arine
```

`companies_ashby.csv` (Ashby):

```csv
company_name,board_slug
Lumos,lumos
```

Each script also accepts a full careers URL in place of the token/ID/slug and parses it out.

**7. Run it.** Actions tab, Job watch, Run workflow. Check the run summary for the results table, then open the Pages URL.

The `job-lead` label is created automatically on the first run. No manual step needed.

---

## Finding board identifiers

- **Greenhouse.** Careers page URL, `job-boards.greenhouse.io/acme` or the older `boards.greenhouse.io/acme`. The token is `acme`.
- **Rippling.** Careers page URL, `ats.rippling.com/arine/jobs`. The board ID is `arine`.
- **Ashby.** Careers page URL, `jobs.ashbyhq.com/lumos`. The slug is `lumos`.
- **Google.** `site:job-boards.greenhouse.io "head of people"` finds the company and the token together; the same pattern works against `ats.rippling.com` and `jobs.ashbyhq.com`.
- **Embedded boards.** View source on the careers page and search for the board's API base URL.

There is no discovery endpoint for any of the three. Each offers no way to enumerate its customers, so every company list is built by hand. That constraint is fine here, since this is a target-company watchlist rather than a broad job search.

---

## Tuning the filters

`TITLE_KEYWORDS`, `TITLE_EXCLUDE`, and `LOCATION_KEYWORDS`/`LOCATION_EXCLUDE` appear near the top of all three watcher scripts and are meant to be kept in sync across them. `COLORADO_KEYWORDS` (or `CO_SIGNALS` in Ashby) drives the onsite-outside-Colorado rule.

Start broad and tighten after a week of watching what comes through. A filter that is too narrow fails silently and looks identical to no new postings.

Note that `specialist` sits in the exclude list, which drops "People Operations Specialist" while keeping "Director, People Operations". Adjust if that is wrong for the search.

Real-Fit Score weights and keyword lists are tuned separately, in `real_fit_profile.py`. See the Real-Fit Score section above.

---

## Local runs

```
pip install requests
python greenhouse_watch.py
python rippling_watch.py
python ashby_watch.py
```

Each runs without `--github`, so no issues are opened. Still writes that source's data file, which will show as a diff on the next commit. Add `--no-content` to skip descriptions and run faster.

---

## Notifications

GitHub sends issue notifications natively. Watch the repo and set notifications to All Activity. Email arrives with no extra service. The mobile app gives push instead.

---

## Schedule

The cron is `0 13,21 * * 1-5`, which is 07:00 and 15:00 Mountain on weekdays during daylight time. Cron runs on UTC and does not adjust for daylight saving, so these shift an hour in winter.

Scheduled workflows can be delayed during periods of high load, sometimes by 10 to 30 minutes. Not a problem for a job watcher.

Repos with no activity for 60 days have scheduled workflows disabled automatically. The state commit counts as activity, so this stays alive as long as roles keep appearing. If the search goes quiet for two months, re-enable it manually.

---

## Manual reset

Actions, Job watch, Run workflow, toggle reset to true. Clears all three sources' logs and treats every current match as new. Useful after widening the filters, adding country exclusions, or backporting a rule to a source that didn't have it yet. It will reopen issues for roles already seen, so close the old ones first.

Roles logged before Real-Fit Score existed have no `real_fit` field and show a blank badge on the dashboard permanently unless a reset run recomputes them from scratch.

---

## Limits worth knowing

- **Three sources, not universal.** Lever, Workable, and any other ATS each have their own API and schema. Adding one means a new fetch and parse function, not a config change.
- **No discovery.** Every company list is manual.
- **Poll only.** No webhooks. New roles surface at the next scheduled run.
- **Ghost postings.** These exist across every job source. A role appearing here does not mean it is real.
- **Compensation data is source-limited.** Only Ashby captures a salary field. Real-Fit Score's compensation category is neutral, not penalized, for Greenhouse and Rippling matches.
- **Workplace type is inferred, not read, for two of three sources.** Greenhouse and Rippling have no clean remote/hybrid/onsite field; the onsite-outside-Colorado rule and Real-Fit Score's remote-eligibility category both fall back to reading the location text, which can misclassify a role a company didn't label clearly.
- **Real-Fit Score is triage, not judgment.** It is keyword and phrase matching, meant to sort roles at a glance. It does not replace reading the posting, and it is a different tool from JDR, which stays a manual, deeper review run separately.

---

## Copilot Agent prompt

Paste this into Copilot Agent mode in VS Code to scaffold the repo from scratch. Note it reflects the full current structure, including all three watchers and the Real-Fit Score module; do not use it against a repo that already has these files, since it will not touch anything already present.

```
Create a new repository named job-watch with this exact structure and no other files. Root files: greenhouse_watch.py, rippling_watch.py, ashby_watch.py, real_fit_profile.py, real_fit_score.py, companies.csv, companies_rippling.csv, companies_ashby.csv, README.md, .gitignore, TMG-BRAND-GUIDE.md. Directory .github/workflows containing job-watch.yml. Directory docs containing index.html and tmg.css. Directory docs/data containing three placeholder files, jobs.json, jobs_rippling.json, and jobs_ashby.json, each holding the single JSON object {"generated": "", "companies_watched": 0, "total_roles": 0, "jobs": {}}. Write .gitignore to ignore __pycache__/, *.pyc, .venv/, venv/, .DS_Store, and .vscode/. Write companies.csv with the header row company_name,board_token and one example data row. Write companies_rippling.csv with the header row company_name,board_id and one example data row. Write companies_ashby.csv with the header row company_name,board_slug and one example data row. Copy TMG-BRAND-GUIDE.md and tmg.css from the external-hire-premium repository, placing TMG-BRAND-GUIDE.md at the repo root and tmg.css inside the docs directory, not the root, because docs/index.html links tmg.css as a sibling file. Do not modify greenhouse_watch.py, rippling_watch.py, ashby_watch.py, real_fit_profile.py, real_fit_score.py, docs/index.html, or .github/workflows/job-watch.yml if those files are already present in the workspace, they are authored and validated already. After creating the structure, initialize git, commit everything with the message "feat: initial job watch scaffold", and create the remote repository under the account rmallorybpc as a public repo, then push to the main branch. Then print a plain text checklist of the four manual settings steps that cannot be done from the CLI: enable Read and write permissions under Settings Actions General Workflow permissions, set Settings Pages Build and deployment Source to GitHub Actions rather than Deploy from a branch, populate all three companies CSVs with real company names and board identifiers, and trigger the first run from the Actions tab using Run workflow. Do not create a job-lead label, the workflow creates it automatically on first run. Do not add a CNAME file, a Jekyll config, or a .nojekyll file. Do not create any workflow other than job-watch.yml.
```
