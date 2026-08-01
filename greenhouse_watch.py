"""
Greenhouse job board watcher.

Polls the public Greenhouse Job Board API for a list of target companies,
filters by title and location, and reports only roles not seen before.

No API key required. The Job Board API is public and unauthenticated.

Local usage:
    python greenhouse_watch.py
    python greenhouse_watch.py --no-content   # skip descriptions, faster
    python greenhouse_watch.py --reset        # clear the log, treat all as new

GitHub Actions usage:
    python greenhouse_watch.py --github

    --github opens a GitHub Issue per new role and writes a run summary.
    Requires GITHUB_TOKEN and GITHUB_REPOSITORY in the environment.

State and data:
    docs/data/jobs.json   cumulative log of every role ever matched.
                          Doubles as the data source for the Pages dashboard.
                          Committed back by the workflow after each run.
    companies.csv         target list, edited by hand.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
GITHUB_API = "https://api.github.com"

# Titles to keep. Case-insensitive substring match against the job title.
# Any single hit keeps the role.
TITLE_KEYWORDS = [
    "chief people",
    "cpo",
    "vp of people",
    "vp, people",
    "vice president of people",
    "vp of hr",
    "vp, hr",
    "vp of human resources",
    "vice president of human resources",
    "head of people",
    "head of hr",
    "director of people",
    "people operations",
    "director, people",
    "senior director of people",
    "sr. director of people",
    "vp of talent",
    "head of talent",
]

# Titles to drop even if they matched above. Kills recruiting-only roles
# and IC postings that use senior-sounding language.
TITLE_EXCLUDE = [
    "recruiter",
    "recruiting coordinator",
    "sourcer",
    "intern",
    "coordinator",
    "assistant",
    "analyst",
    "specialist",
    "partner ii",
    "hrbp ii",
]

# Location filter, exclude-based.
# LOCATION_KEYWORDS keeps only roles whose location contains one of these.
# Leave it empty to keep all US-tagged and remote roles.
LOCATION_KEYWORDS = []

# LOCATION_EXCLUDE drops any role whose location names one of these.
# This removes obvious international postings while keeping US-city-tagged
# roles that may be remote-friendly (e.g. "Indianapolis, IN").
LOCATION_EXCLUDE = [
    "amsterdam", "netherlands",
    "barcelona", "madrid", "spain",
    "toronto", "ontario", "canada", ", on",
    "london", "united kingdom", ", uk",
    "berlin", "germany",
    "paris", "france",
    "dublin", "ireland",
    "bengaluru", "bangalore", ", india",
    "singapore",
    "sydney", "australia",
    "tokyo", "japan",
    "mexico city", "mexico",
    "são paulo", "sao paulo", "brazil",
    "kuala lumpur", "malaysia",
    "remote - emea", "remote - apac", "remote - uk",
    "remote emea", "remote apac",
]

# Colorado signal. Used by the onsite-outside-CO rule below.
COLORADO_KEYWORDS = [
    "colorado",
    ", co",
    "denver",
    "boulder",
    "colorado springs",
    "fort collins",
    "aurora, co",
    "westminster, co",
    "lakewood, co",
]

# Words that signal a role is not tied to one office.
REMOTE_KEYWORDS = ["remote"]
HYBRID_KEYWORDS = ["hybrid"]

REQUEST_DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20

DATA_FILE = os.path.join("docs", "data", "jobs.json")
COMPANIES_FILE = "companies.csv"

ISSUE_LABELS = ["job-lead"]

SEED_COMPANIES = [
    ("Example Co", "examplecoinc"),
]


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def strip_html(raw: str) -> str:
    """Crude tag strip. Good enough for keyword scanning and JDR paste."""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def token_from_url(url: str) -> str:
    """Pull a board token out of a Greenhouse careers URL."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else ""


def load_companies() -> list:
    if not os.path.exists(COMPANIES_FILE):
        with open(COMPANIES_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company_name", "board_token"])
            for name, token in SEED_COMPANIES:
                w.writerow([name, token])
        print(f"Created {COMPANIES_FILE}. Add your target companies and rerun.")
        sys.exit(0)

    rows = []
    with open(COMPANIES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("company_name") or "").strip()
            token = (row.get("board_token") or "").strip()
            if not token or token.startswith("#"):
                continue
            if token.startswith("http"):
                token = token_from_url(token)
            rows.append((name or token, token))
    return rows


def load_log() -> dict:
    """Cumulative record of every role ever matched, keyed by token:job_id."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("jobs", {})
    except (json.JSONDecodeError, OSError, AttributeError):
        print(f"Warning: could not read {DATA_FILE}. Starting fresh.")
        return {}


def save_log(jobs: dict, company_count: int) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "companies_watched": company_count,
        "total_roles": len(jobs),
        "jobs": jobs,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def title_matches(title: str) -> bool:
    low = title.lower()
    if any(bad in low for bad in TITLE_EXCLUDE):
        return False
    return any(good in low for good in TITLE_KEYWORDS)


def location_matches(location: str) -> bool:
    low = (location or "").lower()
    if any(bad in low for bad in LOCATION_EXCLUDE):
        return False
    if not LOCATION_KEYWORDS:
        return True
    return any(loc in low for loc in LOCATION_KEYWORDS)


def infer_workplace_type(location: str) -> str:
    """Rough guess at workplace type from the location string.

    Greenhouse does not expose a clean remote/hybrid/onsite field the way
    Ashby does. This looks for common phrasing instead. When neither
    "remote" nor "hybrid" appears, it falls back to "onsite", since
    Greenhouse locations without that language are almost always
    physical office postings. This is a heuristic, not a real field, so
    it can misclassify a remote role that a company labeled only with a
    city name.
    """
    low = (location or "").lower()
    if not low.strip():
        return "unknown"
    if any(word in low for word in REMOTE_KEYWORDS):
        return "remote"
    if any(word in low for word in HYBRID_KEYWORDS):
        return "hybrid"
    return "onsite"


def is_colorado(location: str) -> bool:
    low = (location or "").lower()
    return any(kw in low for kw in COLORADO_KEYWORDS)


def fetch_jobs(token: str, want_content: bool) -> list:
    url = f"{API_BASE}/{token}/jobs"
    params = {"content": "true"} if want_content else {}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"  ERROR  network failure: {exc}")
        return []

    if resp.status_code == 404:
        print("  ERROR  board token not found (404). Check the slug.")
        return []
    if resp.status_code != 200:
        print(f"  ERROR  HTTP {resp.status_code}")
        return []

    try:
        return resp.json().get("jobs", [])
    except json.JSONDecodeError:
        print("  ERROR  response was not valid JSON")
        return []


# ----------------------------------------------------------------------
# GITHUB OUTPUT
# ----------------------------------------------------------------------

def build_issue_body(record: dict) -> str:
    desc = record.get("description", "")
    if len(desc) > 4000:
        desc = desc[:4000] + "\n\n_(truncated, see the posting for the rest)_"

    return "\n".join(
        [
            f"**Company:** {record['company']}",
            f"**Location:** {record['location']}",
            f"**Workplace type (inferred):** {record.get('workplace_type', 'unknown')}",
            f"**Posted:** {record['posted'] or 'not stated'}",
            f"**Apply:** {record['url']}",
            "",
            "---",
            "",
            "### Review checklist",
            "",
            "- [ ] JDR fit score",
            "- [ ] Competitiveness score (only if fit is 60+)",
            "- [ ] Contact type identified",
            "- [ ] Effort band assigned",
            "",
            "---",
            "",
            "<details>",
            "<summary>Job description</summary>",
            "",
            desc or "_No description captured._",
            "",
            "</details>",
        ]
    )


def create_issue(record: dict, repo: str, token: str) -> str:
    """Returns the issue html_url on success, empty string on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": f"{record['company']}: {record['title']}",
        "body": build_issue_body(record),
        "labels": ISSUE_LABELS,
    }
    try:
        resp = requests.post(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=headers,
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        print(f"  ERROR  could not create issue: {exc}")
        return ""

    if resp.status_code == 201:
        return resp.json().get("html_url", "")
    print(f"  ERROR  issue creation returned HTTP {resp.status_code}: {resp.text[:200]}")
    return ""


def write_step_summary(new_records: list, total: int) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lines = ["## Job watch results", ""]
    if not new_records:
        lines.append(f"No new matches this run. {total} role(s) tracked overall.")
    else:
        lines.append(f"**{len(new_records)} new role(s).** {total} tracked overall.")
        lines.append("")
        lines.append("| Company | Title | Location | Link |")
        lines.append("|---|---|---|---|")
        for r in new_records:
            title = r["title"].replace("|", "\\|")
            lines.append(
                f"| {r['company']} | {title} | {r['location']} | [Apply]({r['url']}) |"
            )

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Greenhouse job board watcher")
    parser.add_argument(
        "--no-content",
        action="store_true",
        help="skip full job descriptions",
    )
    parser.add_argument("--reset", action="store_true", help="clear the jobs log")
    parser.add_argument(
        "--github",
        action="store_true",
        help="open a GitHub Issue per new role and write a run summary",
    )
    args = parser.parse_args()

    want_content = not args.no_content

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")

    if args.github and not (gh_token and gh_repo):
        print("ERROR  --github requires GITHUB_TOKEN and GITHUB_REPOSITORY.")
        sys.exit(1)

    if args.reset and os.path.exists(DATA_FILE):
        os.remove(DATA_FILE)
        print("Cleared jobs log.\n")

    companies = load_companies()
    if not companies:
        print(f"No companies found in {COMPANIES_FILE}.")
        return

    jobs_log = load_log()
    new_records = []
    checked = 0

    print(f"Checking {len(companies)} companies...\n")

    for name, token in companies:
        checked += 1
        print(f"[{checked}/{len(companies)}] {name} ({token})")

        jobs = fetch_jobs(token, want_content)
        if not jobs:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        hits = 0
        for job in jobs:
            title = job.get("title", "")
            location = (job.get("location") or {}).get("name", "")

            if not title_matches(title) or not location_matches(location):
                continue

            workplace_type = infer_workplace_type(location)
            if workplace_type == "onsite" and not is_colorado(location):
                continue

            job_key = f"{token}:{job.get('id')}"
            if job_key in jobs_log:
                continue

            hits += 1
            record = {
                "company": name,
                "token": token,
                "title": title,
                "location": location,
                "workplace_type": workplace_type,
                "posted": (job.get("updated_at") or "")[:10],
                "url": job.get("absolute_url", ""),
                "job_id": job.get("id", ""),
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "description": strip_html(job.get("content", "")),
                "issue_url": "",
            }
            jobs_log[job_key] = record
            new_records.append(record)

        print(f"  {len(jobs)} posted, {hits} new match{'' if hits == 1 else 'es'}")
        time.sleep(REQUEST_DELAY_SECONDS)

    if args.github:
        for record in new_records:
            issue_url = create_issue(record, gh_repo, gh_token)
            if issue_url:
                record["issue_url"] = issue_url
                print(f"  issue opened: {record['company']} - {record['title']}")

    save_log(jobs_log, len(companies))

    if args.github:
        write_step_summary(new_records, len(jobs_log))

    if not new_records:
        print(f"\nNo new matches. {len(jobs_log)} role(s) tracked overall.")
        return

    print(f"\n{len(new_records)} new role(s). {len(jobs_log)} tracked overall.\n")
    for record in new_records:
        print(f"  {record['company']}: {record['title']}")
        print(f"    {record['location']}  |  {record['url']}\n")


if __name__ == "__main__":
    main()
