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

API_BASE = "https://ats.rippling.com/api/v2/board"
PAGE_SIZE = 50

# Same filter lists as the Greenhouse watcher. Keep them in sync.
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

# Location filter. Empty list disables it, matching the Greenhouse setup.
LOCATION_KEYWORDS = []

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
    if not LOCATION_KEYWORDS:
        return True
    low = (location or "").lower()
    return any(loc in low for loc in LOCATION_KEYWORDS)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Rippling ATS board watcher")
    parser.add_argument("--no-content", action="store_true", help="skip descriptions")
    parser.add_argument("--reset", action="store_true", help="clear the jobs log")
    args = parser.parse_args()

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
                "posted": (job.get("createdOn") or "")[:10],
                "url": url,
                "job_id": job_id,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "description": description,
                "issue_url": "",
                "source": "rippling",
            }
            jobs_log[job_key] = record
            new_records.append(record)

        print(f"  {len(listings)} posted, {hits} new match{'' if hits == 1 else 'es'}")
        time.sleep(REQUEST_DELAY_SECONDS)

    save_log(jobs_log, len(companies))

    if not new_records:
        print(f"\nNo new matches. {len(jobs_log)} role(s) tracked overall.")
        return

    print(f"\n{len(new_records)} new role(s). {len(jobs_log)} tracked overall.\n")
    for record in new_records:
        print(f"  {record['company']}: {record['title']}")
        print(f"    {record['location']}  |  {record['url']}\n")


if __name__ == "__main__":
    main()