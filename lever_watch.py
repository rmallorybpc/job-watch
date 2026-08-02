"""
Lever job board watcher.

Polls the public Lever Postings API for a list of target companies,
filters by title and location, reports only roles not seen before.

No API key required. The postings endpoint is public.

Endpoint:
    https://api.lever.co/v0/postings/{company}?mode=json

Local usage:
    python lever_watch.py
    python lever_watch.py --no-content   # skip descriptions, faster
    python lever_watch.py --reset        # clear the log, treat all as new

GitHub Actions usage:
    python lever_watch.py --github

Board slugs come from the careers URL:
    jobs.lever.co/lumos  ->  slug is "lumos"
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

from real_fit_score import compute_real_fit, format_real_fit_section

API_BASE = "https://api.lever.co/v0/postings"
GITHUB_API = "https://api.github.com"
ISSUE_LABELS = ["job-lead"]

# Same filter lists as the other watchers. Keep them in sync.
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

# Location filter, exclude-based. Keep in sync with the other watchers.
LOCATION_KEYWORDS = []

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
    "china", "beijing", "shanghai", "shenzhen", "hong kong",
    "malaysia", "kuala lumpur",
    "thailand", "bangkok",
    "philippines", "manila",
    "indonesia", "jakarta",
    "vietnam", "hanoi",
    "korea", "seoul",
    "taiwan", "taipei",
    "poland", "warsaw",
    "portugal", "lisbon",
    "sweden", "stockholm",
    "united arab emirates", "dubai",
    "remote - emea", "remote - apac", "remote - uk",
    "remote emea", "remote apac",
]

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

REQUEST_DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20

DATA_FILE = os.path.join("docs", "data", "jobs_lever.json")
COMPANIES_FILE = "companies_lever.csv"

SEED_COMPANIES = [
    ("Example Co", "example"),
]


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


def is_colorado(location: str) -> bool:
    low = (location or "").lower()
    return any(kw in low for kw in COLORADO_KEYWORDS)


def workplace_allows(job: dict, location: str) -> bool:
    """
    Drop onsite roles that are not in Colorado. Remote and hybrid roles pass
    regardless of city, since Tracey can work those from Colorado.

    Lever exposes workplaceType directly as one of "remote", "hybrid",
    "on-site", or "unspecified". This is a real field, not a text guess.
    """
    wtype = (job.get("workplaceType") or "").lower()
    if wtype in ("remote", "hybrid"):
        return True
    if wtype == "on-site":
        return is_colorado(location)
    # unspecified or missing: fall back to allowing it, so we do not
    # silently drop roles that lack the field. The location filter
    # still applies.
    return True


def extract_location(job: dict) -> str:
    """Lever nests location under categories, plus an allLocations list
    for multi-location postings."""
    categories = job.get("categories") or {}
    parts = []
    primary = categories.get("location")
    if primary:
        parts.append(primary)
    for loc in categories.get("allLocations") or []:
        if loc and loc not in parts:
            parts.append(loc)
    return ", ".join(parts)


def extract_comp(job: dict) -> str:
    """Lever's salaryRange, when a company chooses to post one."""
    sr = job.get("salaryRange") or {}
    lo = sr.get("min")
    hi = sr.get("max")
    currency = sr.get("currency", "")
    if lo is None and hi is None:
        return ""
    if lo is not None and hi is not None:
        return f"{currency} {lo:,.0f} - {hi:,.0f}".strip()
    figure = lo if lo is not None else hi
    return f"{currency} {figure:,.0f}".strip()


def fetch_jobs(slug: str) -> list:
    url = f"{API_BASE}/{slug}"
    try:
        resp = requests.get(url, params={"mode": "json"}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"  ERROR  network failure: {exc}")
        return []

    if resp.status_code == 404:
        print("  ERROR  board slug not found (404). Check the slug.")
        return []
    if resp.status_code != 200:
        print(f"  ERROR  HTTP {resp.status_code}")
        return []

    try:
        data = resp.json()
    except json.JSONDecodeError:
        print("  ERROR  response was not valid JSON")
        return []

    # Lever returns a bare JSON array, not a wrapper object.
    return data if isinstance(data, list) else []


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
            f"**Workplace type:** {record.get('workplace_type') or 'not stated'}",
            f"**Compensation:** {record.get('comp') or 'not stated'}",
            f"**Posted:** {record['posted'] or 'not stated'}",
            f"**Source:** Lever",
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


def create_issue(record: dict, repo: str, token: str) -> str:
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

    lines = ["## Lever watch results", ""]
    if not new_records:
        lines.append(f"No new Lever matches this run. {total} tracked overall.")
    else:
        lines.append(f"**{len(new_records)} new Lever role(s).** {total} tracked overall.")
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

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lever job board watcher")
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

        jobs = fetch_jobs(slug)
        if not jobs:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        hits = 0
        for job in jobs:
            title = job.get("text") or ""
            location = extract_location(job)

            if not title_matches(title) or not location_matches(location):
                continue
            if not workplace_allows(job, location):
                continue

            job_id = job.get("id") or ""
            job_key = f"{slug}:{job_id}"
            if job_key in jobs_log:
                continue

            hits += 1
            description = ""
            if not args.no_content:
                description = strip_html(job.get("description") or job.get("descriptionPlain") or "")

            created_at = job.get("createdAt")
            posted = ""
            if created_at:
                try:
                    posted = datetime.fromtimestamp(int(created_at) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    posted = ""

            record = {
                "company": name,
                "token": slug,
                "title": title,
                "location": location,
                "workplace_type": (job.get("workplaceType") or "").lower() or None,
                "comp": extract_comp(job),
                "posted": posted,
                "url": job.get("hostedUrl") or job.get("applyUrl") or f"https://jobs.lever.co/{slug}",
                "job_id": job_id,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "description": description,
                "issue_url": "",
                "source": "lever",
            }
            record["real_fit"] = compute_real_fit(record)
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
