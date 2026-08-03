"""
filters.py

Shared title, location, and Colorado-detection keyword lists and
matching functions, used by all four watchers.

Ashby keeps its own location_matches, since it uses a US-signal
allowlist rather than the plain exclude-list approach the other three
use, and its own workplace_allows and Lever's workplace_allows, since
both read a real workplaceType-style field instead of guessing from
location text. Everything else lives here, one list per concern
instead of one copy per watcher.

Editing this file changes matching behavior for every watcher that
imports it.
"""

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

# Location filter, exclude-based. Leave LOCATION_KEYWORDS empty to keep
# all US-tagged and remote roles; set it to require an allowlist match.
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

# Colorado signal, used by the onsite-outside-CO rule and by Real-Fit
# Score's remote-eligibility category.
COLORADO_KEYWORDS = [
    "colorado",
    ", co",
    "denver",
    "boulder",
    "broomfield",
    "golden",
    "fort collins",
    "colorado springs",
    "aurora",
    "lakewood",
    "littleton",
    "centennial",
    "arvada",
    "westminster",
]

# Words that signal a role is not tied to one office. Used by the text
# heuristic (infer_workplace_type) for sources with no real field.
REMOTE_KEYWORDS = ["remote"]
HYBRID_KEYWORDS = ["hybrid"]


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


def infer_workplace_type(location: str) -> str:
    """Rough guess at workplace type from the location string, for
    sources with no real remote/hybrid/onsite field (Greenhouse,
    Rippling). Falls back to "onsite" when neither "remote" nor
    "hybrid" appears in the location text. This is a heuristic, not a
    real field, so it can misclassify a remote role that a company
    labeled only with a city name."""
    low = (location or "").lower()
    if not low.strip():
        return "unknown"
    if any(word in low for word in REMOTE_KEYWORDS):
        return "remote"
    if any(word in low for word in HYBRID_KEYWORDS):
        return "hybrid"
    return "onsite"
