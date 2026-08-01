"""
Rippling ATS board watcher.

Polls the public Rippling ATS board API for a list of target companies,
filters by title and location, reports only roles not seen before.

No API key required. The board listings endpoint is public.

Listings:  https://ats.rippling.com/api/v2/board/{board_id}/jobs
Detail:    https://ats.rippling.com/api/v2/board/{board_id}/jobs/{job_id}

Local usage:
    python rippling_watch.py
    python rippling_watch.py --no-content   # skip descriptions, faster
    python rippling_watch.py --reset        # clear the log, treat all as new

GitHub Actions usage:
    python rippling_watch.py --github

Board IDs come from the careers URL:
    ats.rippling.com/arine/jobs  ->  board_id is "arine"
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

API_BASE = "https://ats.rippling.com/api/v2/board"
GITHUB_API = "https://api.github.com"
PAGE_SIZE = 50
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

# Words that signal a role is not tied to one office. Rippling has no
# clean workplace-type field, same situation as Greenhouse, so this is a
# text heuristic against the location string, not a real field read.
REMOTE_KEYWORDS = ["remote"]
HYBRID_KEYWORDS = ["hybrid"]

REQUEST_DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20

DATA_FILE = os.path.join("docs", "data", "jobs_rippling.json")
COMPANIES_FILE = "companies_rippling.csv"

SEED_COMPANIES = [
    ("Arine", "arine"),
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
            w.writerow(["company_name", "board_id"])
            for name, bid in SEED_COMPANIES:
                w.writerow([name, bid])
        print(f"Created {COMPANIES_FILE}. Add your target companies and rerun.")
        sys.exit(0)

    rows = []
    with open(COMPANIES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("company_name") or "").strip()
            bid = (row.get("board_id") or "").strip()
            if not bid or bid.startswith("#"):
                continue
            if bid.startswith("http"):
                bid = token_from_url(bid)
            rows.append((name or bid, bid))
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


def infer_workplace_type(location: str) -> str:
    """Rough guess at workplace type from the location string. Rippling
    does not expose a clean remote/hybrid/onsite field, same situation
    as Greenhouse. Falls back to "onsite" when neither "remote" nor
    "hybrid" appears in the location text."""
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


def extract_location(job: dict) -> str:
    """Rippling location shape varies. Try the common keys."""
    locs = job.get("workLocations") or job.get("locations") or []
    names = []
    for loc in locs:
        if isinstance(loc, dict):
            name = loc.get("label") or loc.get("name") or loc.get("city") or ""
            if name:
                names.append(name)
        elif isinstance(loc, str):
            names.append(loc)
    single = job.get("location")
    if isinstance(single, dict):
        single = single.get("label") or single.get("name") or ""
    if single and single not in names:
        names.append(single)
    return ", ".join(names)


def fetch_listings(board_id: str) -> list:
    """Paginate the listings endpoint. Returns raw job dicts."""
    all_jobs = []
    page = 0
    while True:
        url = f"{API_BASE}/{board_id}/jobs"
        try:
            resp = requests.get(
                url,
                params={"page": page, "pageSize": PAGE_SIZE},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            print(f"  ERROR  network failure: {exc}")
            return all_jobs

        if resp.status_code == 404:
            print("  ERROR  board id not found (404). Check the slug.")
            return all_jobs
        if resp.status_code != 200:
            print(f"  ERROR  HTTP {resp.status_code}")
            return all_jobs

        try:
            data = resp.json()
        except json.JSONDecodeError:
            print("  ERROR  response was not valid JSON")
            return all_jobs

        items = data.get("items", [])
        all_jobs.extend(items)

        total_pages = data.get("totalPages", 1)
        page += 1
        if page >= total_pages or not items:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    return all_jobs


def fetch_detail(board_id: str, job_id: str) -> str:
    """Fetch one job's full description HTML. Best-effort."""
    url = f"{API_BASE}/{board_id}/jobs/{job_id}"
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return ""
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return ""

    desc = data.get("description") or data.get("jobDescription") or ""
    if isinstance(desc, dict):
        desc = "\n\n".join(str(v) for v in desc.values())
    return strip_html(desc)


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
            f"**Source:** Rippling",
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

    lines = ["## Rippling watch results", ""]
    if not new_records:
        lines.append(f"No new Rippling matches this run. {total} tracked overall.")
    else:
        lines.append(f"**{len(new_records)} new Rippling role(s).** {total} tracked overall.")
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
    parser = argparse.ArgumentParser(description="Rippling ATS board watcher")
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

    for name, bid in companies:
        checked += 1
        print(f"[{checked}/{len(companies)}] {name} ({bid})")

        listings = fetch_listings(bid)
        if not listings:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        hits = 0
        for job in listings:
            title = job.get("name") or job.get("title") or ""
            location = extract_location(job)

            if not title_matches(title) or not location_matches(location):
                continue

            workplace_type = infer_workplace_type(location)
            if workplace_type == "onsite" and not is_colorado(location):
                continue

            job_id = job.get("uuid") or job.get("id") or ""
            job_key = f"{bid}:{job_id}"
            if job_key in jobs_log:
                continue

            hits += 1
            description = ""
            if not args.no_content and job_id:
                description = fetch_detail(bid, job_id)
                time.sleep(REQUEST_DELAY_SECONDS)

            url = f"https://ats.rippling.com/{bid}/jobs/{job_id}"
            record = {
                "company": name,
                "token": bid,
                "title": title,
                "location": location,
                "workplace_type": workplace_type,
                "posted": (job.get("createdOn") or "")[:10],
                "url": url,
                "job_id": job_id,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "description": description,
                "issue_url": "",
                "source": "rippling",
            }
            record["real_fit"] = compute_real_fit(record)
            jobs_log[job_key] = record
            new_records.append(record)

        print(f"  {len(listings)} posted, {hits} new match{'' if hits == 1 else 'es'}")
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
