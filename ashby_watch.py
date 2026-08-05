"""
Ashby job board watcher.

Polls the public Ashby posting API for a list of target companies,
filters by title and location, reports only roles not seen before.

No API key required. The posting API is public.

Endpoint:
    https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Local usage:
    python ashby_watch.py
    python ashby_watch.py --no-content   # skip descriptions, faster
    python ashby_watch.py --reset        # clear the log, treat all as new

GitHub Actions usage:
    python ashby_watch.py --github

Board slugs come from the careers URL:
    jobs.ashbyhq.com/lumos  ->  slug is "lumos"
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

from filters import title_matches, is_colorado, LOCATION_EXCLUDE, LOCATION_KEYWORDS
from real_fit_score import compute_real_fit, format_real_fit_section
from github_issues import create_issue, load_existing_issues

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"

# US indicators. If any appears, the role is US-eligible and we keep it even
# when a foreign location is also listed (e.g. "United States or Canada").
# This is Ashby-specific: an allowlist layered on top of the shared
# LOCATION_EXCLUDE, rather than exclude-only like the other watchers.
US_SIGNALS = [
    "united states", "usa", " us ", ", us", "u.s.", "remote",
    "north america", "anywhere",
    "california", "colorado", "new york", "texas", "washington",
    "massachusetts", "florida", "illinois", "georgia", "michigan",
    "nevada", "north carolina", "virginia", "oregon", "arizona",
    "san francisco", "denver", "boulder", "broomfield", "seattle",
    "boston", "austin", "chicago", "miami", "new jersey", "connecticut",
    ", ca", ", co", ", ny", ", tx", ", wa", ", ma", ", fl", ", il",
    ", ga", ", mi", ", nv", ", nc", ", va", ", or", ", az", ", nj", ", ct",
]

REQUEST_DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20

DATA_FILE = os.path.join("docs", "data", "jobs_ashby.json")
COMPANIES_FILE = "companies_ashby.csv"

SEED_COMPANIES = [
    ("Lumos", "lumos"),
]

FETCH_ERRORS = []


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
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def token_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else ""


def load_companies() -> list:
    if not os.path.exists(COMPANIES_FILE):
        with open(COMPANIES_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company_name", "board_slug"])
            for name, slug in SEED_COMPANIES:
                w.writerow([name, slug])
        print(f"Created {COMPANIES_FILE}. Add your target companies and rerun.")
        sys.exit(0)

    rows = []
    with open(COMPANIES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("company_name") or "").strip()
            slug = (row.get("board_slug") or "").strip()
            if not slug or slug.startswith("#"):
                continue
            if slug.startswith("http"):
                slug = token_from_url(slug)
            rows.append((name or slug, slug))
    return rows


def load_log() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f).get("jobs", {})
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


def location_matches(location: str) -> bool:
    low = (location or "").lower()
    us_signal = any(x in low for x in US_SIGNALS)
    if not us_signal and any(bad in low for bad in LOCATION_EXCLUDE):
        return False
    if not LOCATION_KEYWORDS:
        return True
    return any(loc in low for loc in LOCATION_KEYWORDS)


def workplace_allows(job: dict, location: str) -> bool:
    """
    Drop onsite roles that are not in Colorado. Remote and hybrid roles pass
    regardless of city, since Tracey can work those from Colorado.

    Ashby signals remote via the boolean isRemote and via an employment/
    workplace type string. We treat a role as onsite only when it is clearly
    marked onsite and not remote.
    """
    is_remote = bool(job.get("isRemote"))
    wtype = ""
    for key in ("workplaceType", "locationType", "employmentType"):
        v = job.get(key)
        if isinstance(v, str) and v:
            wtype += " " + v.lower()
    onsite = ("on-site" in wtype or "onsite" in wtype or "in office" in wtype
              or "in-office" in wtype)
    hybrid = "hybrid" in wtype

    if is_remote or hybrid:
        return True
    if onsite:
        return is_colorado(location)
    return True


def extract_location(job: dict) -> str:
    """Ashby returns a flat 'location' string plus optional secondaryLocations."""
    parts = []
    if job.get("location"):
        parts.append(job["location"])
    for sec in job.get("secondaryLocations") or []:
        if isinstance(sec, dict) and sec.get("location"):
            parts.append(sec["location"])
    return ", ".join(parts)


def fetch_jobs(name: str, slug: str) -> list:
    url = f"{API_BASE}/{slug}"
    try:
        resp = requests.get(
            url,
            params={"includeCompensation": "true"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        reason = f"network failure: {exc}"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": slug, "reason": reason})
        return []

    if resp.status_code == 404:
        reason = "board slug not found (404)"
        print(f"  ERROR  {reason}. Check the slug.")
        FETCH_ERRORS.append({"company": name, "token": slug, "reason": reason})
        return []
    if resp.status_code != 200:
        reason = f"HTTP {resp.status_code}"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": slug, "reason": reason})
        return []

    try:
        return resp.json().get("jobs", [])
    except json.JSONDecodeError:
        reason = "response was not valid JSON"
        print(f"  ERROR  {reason}")
        FETCH_ERRORS.append({"company": name, "token": slug, "reason": reason})
        return []


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
            f"**Compensation:** {record.get('comp') or 'not stated'}",
            f"**Posted:** {record['posted'] or 'not stated'}",
            f"**Source:** Ashby",
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

    lines = ["## Ashby watch results", ""]
    if not new_records:
        lines.append(f"No new Ashby matches this run. {total} tracked overall.")
    else:
        lines.append(f"**{len(new_records)} new Ashby role(s).** {total} tracked overall.")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Ashby job board watcher")
    parser.add_argument("--no-content", action="store_true", help="skip descriptions")
    parser.add_argument("--reset", action="store_true", help="clear the jobs log")
    parser.add_argument(
        "--github",
        action="store_true",
        help="open a GitHub Issue per new role and write a run summary",
    )
    args = parser.parse_args()

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

    print(f"Checking {len(companies)} companies...\n")

    for name, slug in companies:
        checked += 1
        print(f"[{checked}/{len(companies)}] {name} ({slug})")

        jobs = fetch_jobs(name, slug)
        if not jobs:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        hits = 0
        for job in jobs:
            title = job.get("title") or ""
            location = extract_location(job)

            if not title_matches(title) or not location_matches(location):
                continue
            if not workplace_allows(job, location):
                continue

            job_id = job.get("id") or job.get("jobId") or ""
            job_key = f"{slug}:{job_id}"
            if job_key in jobs_log:
                continue

            hits += 1
            comp = ""
            comp_obj = job.get("compensation") or {}
            if isinstance(comp_obj, dict):
                comp = comp_obj.get("scrapeableCompensationSalarySummary") or \
                    comp_obj.get("compensationTierSummary") or ""

            workplace = ""
            if job.get("isRemote"):
                workplace = "Remote"
            else:
                for key in ("workplaceType", "locationType", "employmentType"):
                    v = job.get(key)
                    if isinstance(v, str) and v:
                        workplace = v
                        break

            description = ""
            if not args.no_content:
                description = strip_html(job.get("descriptionHtml") or job.get("descriptionPlain") or "")

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
                "token": slug,
                "title": title,
                "location": location,
                "workplace": workplace,
                "comp": comp,
                "posted": (job.get("publishedAt") or job.get("updatedAt") or "")[:10],
                "url": job.get("jobUrl") or job.get("applyUrl") or f"https://jobs.ashbyhq.com/{slug}",
                "job_id": job_id,
                "first_seen": first_seen,
                "description": description,
                "issue_url": issue_url,
                "source": "ashby",
            }
            record["real_fit"] = compute_real_fit(record)
            jobs_log[job_key] = record
            new_records.append(record)

        print(f"  {len(jobs)} posted, {hits} new match{'' if hits == 1 else 'es'}")
        time.sleep(REQUEST_DELAY_SECONDS)

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
