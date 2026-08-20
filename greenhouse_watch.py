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
    Also loads existing job-lead issues first, to avoid opening a
    duplicate for a role already logged and to recover its true
    first-seen date.

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

from filters import title_matches, location_matches, infer_workplace_type, is_colorado
from real_fit_score import compute_real_fit, format_real_fit_section
from github_issues import create_issue, load_existing_issues

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

API_BASE = "https://boards-api.greenhouse.io/v1/boards"

REQUEST_DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20

DATA_FILE = os.path.join("docs", "data", "jobs.json")
COMPANIES_FILE = "companies.csv"

SEED_COMPANIES = [
    ("Example Co", "examplecoinc"),
]

FETCH_ERRORS = []


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def strip_html(raw: str) -> str:
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
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("jobs", {})
    except (json.JSONDecodeError, OSError, AttributeError):
        print(f"Warning: could not read {DATA_FILE}. Starting fresh.")
        return {}


def save_log(jobs: dict, company_count: int, fetch_errors: list) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "companies_watched": company_count,
        "total_roles": len(jobs),
        "fetch_errors": fetch_errors,
        "jobs": jobs,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def fetch_jobs(name: str, token: str, want_content: bool) -> list:
    url = f"{API_BASE}/{token}/jobs"
    params = {"content": "true"} if want_content else {}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        reason = f"network failure: {exc}"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": token, "reason": reason})
        return []

    if resp.status_code == 404:
        reason = "board token not found (404)"
        print(f"  ERROR  {reason}. Check the slug.")
        FETCH_ERRORS.append({"company": name, "token": token, "reason": reason})
        return []
    if resp.status_code != 200:
        reason = f"HTTP {resp.status_code}"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": token, "reason": reason})
        return []

    try:
        return resp.json().get("jobs", [])
    except json.JSONDecodeError:
        reason = "response was not valid JSON"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": token, "reason": reason})
        return []


# ----------------------------------------------------------------------
# GITHUB OUTPUT
# ----------------------------------------------------------------------

def build_issue_body(record: dict) -> str:
    desc = record.get("description", "")
    if len(desc) > 4000:
        desc = desc[:4000] + "\n\n_(truncated, see the posting for the rest)_"

    real_fit = record.get("real_fit")
    real_fit_section = format_real_fit_section(real_fit) if real_fit else "_Real-Fit Score not computed._"

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
            real_fit_section,
            "",
            "---",
            "",
            "### Review checklist",
            "",
            "- [ ] JDR fit score",
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


def write_step_summary(new_records: list, total: int, fetch_errors: list, reused_count: int) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lines = ["## Job watch results", ""]
    if not new_records:
        lines.append(f"No new matches this run. {total} role(s) tracked overall.")
    else:
        lines.append(f"**{len(new_records)} new role(s).** {total} tracked overall.")
        if reused_count:
            lines.append(f"{reused_count} matched an existing issue and did not open a duplicate.")
        lines.append("")
        lines.append("| Company | Title | Real-Fit | Location | Link |")
        lines.append("|---|---|---|---|---|")
        for r in new_records:
            title = r["title"].replace("|", "\\|")
            rf = r.get("real_fit") or {}
            rf_label = f"{rf.get('score', '-')} ({rf.get('verdict', '-')})" if rf else "-"
            lines.append(
                f"| {r['company']} | {title} | {rf_label} | {r['location']} | [Apply]({r['url']}) |"
            )

    if fetch_errors:
        lines.append("")
        lines.append("### Fetch failures")
        lines.append("")
        lines.append("| Company | Reason |")
        lines.append("|---|---|")
        for e in fetch_errors:
            lines.append(f"| {e['company']} | {e['reason']} |")

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

    existing_issues = {}
    if args.github:
        print("Loading existing job-lead issues...")
        existing_issues = load_existing_issues(gh_repo, gh_token)
        print(f"  found {len(existing_issues)} existing issue(s)\n")

    companies = load_companies()
    if not companies:
        print(f"No companies found in {COMPANIES_FILE}.")
        return

    jobs_log = load_log()
    new_records = []
    checked = 0
    # For closed-role reconciliation: the set of company tokens that
    # fetched successfully with a non-empty result this run, and the set
    # of job_keys actually seen live in those fetches. A logged role is
    # marked closed only if its company is in scanned_ok AND its job_key
    # is not in seen_keys — never on a failed or empty fetch.
    scanned_ok = set()
    seen_keys = set()

    print(f"Checking {len(companies)} companies...\n")

    for name, token in companies:
        checked += 1
        print(f"[{checked}/{len(companies)}] {name} ({token})")

        jobs = fetch_jobs(name, token, want_content)
        if not jobs:
            # Distinguish a genuinely empty/unreadable board from a real
            # fetch error. A fetch error already logged to FETCH_ERRORS
            # and printed an ERROR line above; this prints a line so a
            # board that returned zero postings is visible rather than
            # silently skipped.
            already_errored = any(e["token"] == token for e in FETCH_ERRORS)
            if not already_errored:
                print("  0 posted (board returned no jobs)")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        # This company fetched successfully with real postings, so its
        # log entries can be safely reconciled against what is live now.
        scanned_ok.add(token)
        for job in jobs:
            seen_keys.add(f"{token}:{job.get('id')}")

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
            issue_title = f"{name}: {title}"
            existing = existing_issues.get(issue_title)
            if existing:
                first_seen = (existing["created_at"] or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                issue_url = existing["url"]
            else:
                first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                issue_url = ""

            record = {
                "company": name,
                "token": token,
                "title": title,
                "location": location,
                "workplace_type": workplace_type,
                "posted": (job.get("updated_at") or "")[:10],
                "url": job.get("absolute_url", ""),
                "job_id": job.get("id", ""),
                "first_seen": first_seen,
                "description": strip_html(job.get("content", "")),
                "issue_url": issue_url,
                "closed_date": "",
            }
            record["real_fit"] = compute_real_fit(record)
            jobs_log[job_key] = record
            new_records.append(record)

        print(f"  {len(jobs)} posted, {hits} new match{'' if hits == 1 else 'es'}")
        time.sleep(REQUEST_DELAY_SECONDS)

    # ------------------------------------------------------------------
    # Closed-role reconciliation.
    # A role in the log is marked closed when its company fetched
    # successfully this run (token in scanned_ok) but the role's job_key
    # was not among the live postings (not in seen_keys). This never
    # fires for a company that 404'd, errored, or returned an empty
    # board, since those tokens are absent from scanned_ok — a failed
    # fetch must not be read as "every role closed". A role already
    # marked closed keeps its original closed_date and is not re-stamped.
    # ------------------------------------------------------------------
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    newly_closed = 0
    for job_key, record in jobs_log.items():
        rec_token = record.get("token")
        if rec_token not in scanned_ok:
            continue
        if job_key in seen_keys:
            # Role is live again; clear any stale closed stamp.
            if record.get("closed_date"):
                record["closed_date"] = ""
            continue
        if not record.get("closed_date"):
            record["closed_date"] = today
            newly_closed += 1
    if newly_closed:
        print(f"\n{newly_closed} role(s) no longer posted; marked closed as of {today}.")

    reused_count = 0
    if args.github:
        for record in new_records:
            if record["issue_url"]:
                reused_count += 1
                print(f"  reused existing issue: {record['company']} - {record['title']}")
                continue
            issue_url = create_issue(record, gh_repo, gh_token, build_issue_body)
            if issue_url:
                record["issue_url"] = issue_url
                print(f"  issue opened: {record['company']} - {record['title']}")

    save_log(jobs_log, len(companies), FETCH_ERRORS)

    if args.github:
        write_step_summary(new_records, len(jobs_log), FETCH_ERRORS, reused_count)

    if FETCH_ERRORS:
        print(f"\n{len(FETCH_ERRORS)} compan{'y' if len(FETCH_ERRORS) == 1 else 'ies'} failed to fetch:")
        for e in FETCH_ERRORS:
            print(f"  {e['company']}: {e['reason']}")

    if not new_records:
        print(f"\nNo new matches. {len(jobs_log)} role(s) tracked overall.")
        return

    print(f"\n{len(new_records)} new role(s). {len(jobs_log)} tracked overall.\n")
    for record in new_records:
        print(f"  {record['company']}: {record['title']}")
        print(f"    {record['location']}  |  {record['url']}\n")


if __name__ == "__main__":
    main()
