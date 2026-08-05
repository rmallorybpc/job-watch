"""
github_issues.py

Shared GitHub Issues API helpers used by all four watchers: opening a
new job-lead issue, and loading every existing job-lead issue (open or
closed) so a watcher can avoid opening a duplicate for a role it has
already logged before, and can recover the role's true first-seen date
from the issue's original creation date instead of stamping "now"
every time a reset or a rerun sees the role again.
"""

import json

import requests

GITHUB_API = "https://api.github.com"
ISSUE_LABELS = ["job-lead"]
TIMEOUT_SECONDS = 20
MAX_ISSUE_PAGES = 10  # safety cap; 100 issues/page = 1000 issues max


def create_issue(record: dict, repo: str, token: str, build_issue_body) -> str:
    """Returns the issue html_url on success, empty string on failure.
    build_issue_body is the watcher-specific function that formats the
    issue body, since that differs slightly per source."""
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


def load_existing_issues(repo: str, token: str) -> dict:
    """Map of exact issue title -> {"url":..., "created_at":...} for
    every job-lead issue ever opened, open or closed. Used so a reset
    or a rerun does not open a duplicate issue for a role already
    logged, and so first_seen can be recovered from the issue's real
    creation date instead of the date of whichever run last saw it.

    Matching is by exact title string ("Company: Title"). If a company
    edits a posting's title, this will not match the old issue and a
    new one opens with a fresh first_seen. That is a known limitation,
    not a bug."""
    issues = {}
    if not (repo and token):
        return issues

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    page = 1
    while page <= MAX_ISSUE_PAGES:
        try:
            resp = requests.get(
                f"{GITHUB_API}/repos/{repo}/issues",
                headers=headers,
                params={"state": "all", "labels": "job-lead", "per_page": 100, "page": page},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            print(f"  WARN  could not load existing issues: {exc}")
            return issues

        if resp.status_code != 200:
            print(f"  WARN  could not load existing issues: HTTP {resp.status_code}")
            return issues

        try:
            batch = resp.json()
        except json.JSONDecodeError:
            print("  WARN  existing issues response was not valid JSON")
            return issues

        if not batch:
            break

        for issue in batch:
            # Pull requests share this endpoint; skip them.
            if "pull_request" in issue:
                continue
            title = issue.get("title", "")
            created_at = issue.get("created_at", "")
            url = issue.get("html_url", "")
            if not title:
                continue
            existing = issues.get(title)
            if existing is None or created_at < existing["created_at"]:
                issues[title] = {"url": url, "created_at": created_at}

        if len(batch) < 100:
            break
        page += 1

    return issues
