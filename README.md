# job-watch

Polls the public Greenhouse Job Board API on a schedule, opens a GitHub Issue for every new matching role, and publishes a TMG-branded dashboard to GitHub Pages. Runs entirely on GitHub Actions. No third-party automation service.

Live dashboard: `https://rmallorybpc.github.io/job-watch/`

---

## Repo structure

```
job-watch/
├── .github/
│   └── workflows/
│       └── job-watch.yml        Scheduled run, label creation, state commit, Pages deploy
├── docs/                        Published to GitHub Pages via Actions artifact
│   ├── index.html               TMG-branded dashboard
│   ├── tmg.css                  Copy from external-hire-premium
│   └── data/
│       └── jobs.json            Generated. Cumulative log and dashboard data source.
├── companies.csv                Target list, edited by hand
├── greenhouse_watch.py          The watcher
├── TMG-BRAND-GUIDE.md           Copy from external-hire-premium
├── .gitignore
└── README.md
```

Two files are copied in from the `external-hire-premium` repo rather than created here: `TMG-BRAND-GUIDE.md` at the root and `tmg.css` inside `docs/`. The dashboard links `tmg.css` as a sibling file, so it must sit in `docs/`, not the root.

`docs/data/jobs.json` is both the dedup state and the dashboard data. One file, one commit, no sync risk between them.

---

## How it works

1. Actions fires on cron, on push to `main`, or manually.
2. The `job-lead` label is created if it does not exist. Idempotent, so it is safe on every run.
3. The script reads `companies.csv` and calls the Greenhouse Job Board API per board token.
4. Titles and locations are filtered against the keyword lists in the script.
5. Anything not already in `docs/data/jobs.json` is new.
6. Each new role opens a GitHub Issue labeled `job-lead`, with a JDR review checklist and the full description in a collapsed block.
7. The issue URL is written back into the record so the dashboard can link to it.
8. `docs/data/jobs.json` is committed, then `docs/` is uploaded and deployed to Pages.

---

## Setup

**1. Create the repo.** Public gives unlimited Actions minutes and a Pages URL under `rmallorybpc.github.io`. Private works but caps Actions at 2,000 minutes per month, which is far more than this uses.

**2. Add the files** in the structure above.

**3. Copy the brand files.** From `external-hire-premium`, copy `TMG-BRAND-GUIDE.md` to the root and `tmg.css` to `docs/`.

**4. Enable write permissions.** Settings, Actions, General, Workflow permissions, select "Read and write permissions". Without this both the state commit and the label creation fail.

**5. Set Pages source to Actions.** Settings, Pages, Build and deployment, Source, select **GitHub Actions**. Do not select "Deploy from a branch". The workflow uploads `docs/` as an artifact and deploys it directly, so no branch or root folder is involved.

**6. Fill in `companies.csv`.**

```csv
company_name,board_token
Acme Corp,acmecorp
Widget Inc,widgetinc
```

The token is the slug in the company's Greenhouse careers URL. `job-boards.greenhouse.io/acmecorp` means the token is `acmecorp`. The script also accepts a full URL in the token column and parses the slug out.

**7. Run it.** Actions tab, Job watch, Run workflow. Check the run summary for the results table, then open the Pages URL.

The `job-lead` label is created automatically on the first run. No manual step needed.

---

## Finding board tokens

- **Careers page URL.** `job-boards.greenhouse.io/acme` or the older `boards.greenhouse.io/acme`.
- **Google.** `site:job-boards.greenhouse.io "head of people"` finds the company and the token together.
- **Embedded boards.** View source on the careers page and search for `for=`. The value after it is the token.

There is no discovery endpoint. Greenhouse offers no way to enumerate its customers, so the company list is built by hand. That constraint is fine here, since this is a target-company watchlist rather than a broad job search.

---

## Tuning the filters

Three lists near the top of `greenhouse_watch.py`:

- `TITLE_KEYWORDS` — any single match keeps the role
- `TITLE_EXCLUDE` — any match drops it, even if a keyword hit
- `LOCATION_KEYWORDS` — set to `[]` to disable location filtering

Start broad and tighten after a week of watching what comes through. A filter that is too narrow fails silently and looks identical to no new postings.

Note that `specialist` sits in the exclude list, which drops "People Operations Specialist" while keeping "Director, People Operations". Adjust if that is wrong for the search.

---

## Local runs

```
pip install requests
python greenhouse_watch.py
```

Runs without `--github`, so no issues are opened. Still writes `docs/data/jobs.json`, which will show as a diff on the next commit. Add `--no-content` to skip descriptions and run faster.

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

Actions, Job watch, Run workflow, toggle reset to true. Clears the jobs log and treats every current match as new. Useful after widening the filters. It will reopen issues for roles already seen, so close the old ones first.

---

## Limits worth knowing

- **Greenhouse only.** Lever, Ashby, and Workable each have their own API and schema. Adding one means a new fetch and parse function, not a config change.
- **No discovery.** The company list is manual.
- **Poll only.** No webhooks. New roles surface at the next scheduled run.
- **Ghost postings.** These exist across every job source. A role appearing here does not mean it is real.

---

## Copilot Agent prompt

Paste this into Copilot Agent mode in VS Code to scaffold the repo.

```
Create a new repository named job-watch with this exact structure and no other files. Root files: greenhouse_watch.py, companies.csv, README.md, .gitignore, TMG-BRAND-GUIDE.md. Directory .github/workflows containing job-watch.yml. Directory docs containing index.html and tmg.css. Directory docs/data containing a placeholder jobs.json holding the single JSON object {"generated": "", "companies_watched": 0, "total_roles": 0, "jobs": {}}. Write .gitignore to ignore __pycache__/, *.pyc, .venv/, venv/, .DS_Store, and .vscode/. Write companies.csv with the header row company_name,board_token and one example data row. Copy TMG-BRAND-GUIDE.md and tmg.css from the external-hire-premium repository, placing TMG-BRAND-GUIDE.md at the repo root and tmg.css inside the docs directory, not the root, because docs/index.html links tmg.css as a sibling file. Do not modify greenhouse_watch.py, docs/index.html, or .github/workflows/job-watch.yml if those files are already present in the workspace, they are authored and validated already. After creating the structure, initialize git, commit everything with the message "feat: initial job watch scaffold", and create the remote repository under the account rmallorybpc as a public repo, then push to the main branch. Then print a plain text checklist of the four manual settings steps that cannot be done from the CLI: enable Read and write permissions under Settings Actions General Workflow permissions, set Settings Pages Build and deployment Source to GitHub Actions rather than Deploy from a branch, populate companies.csv with real company names and Greenhouse board tokens, and trigger the first run from the Actions tab using Run workflow. Do not create a job-lead label, the workflow creates it automatically on first run. Do not add a CNAME file, a Jekyll config, or a .nojekyll file. Do not create any workflow other than job-watch.yml.
```
