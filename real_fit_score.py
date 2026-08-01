"""
real_fit_score.py

Computes the Real-Fit Score for a matched job record, using the weights
and keyword lists in real_fit_profile.py. Shared by all three watchers.

This is keyword and phrase matching, not a judgment-based read of the
JD. It approximates the Zapier Real-Fit rubric well enough to triage,
it does not replace a human read, and it does not replace JDR.

Where a source does not capture a needed field (compensation on
Greenhouse and Rippling, workplace type where a source lacks a clean
field), the matching category is scored neutral rather than penalized,
and the result flags that the data was missing so it is visible on
the issue.
"""

import re

from real_fit_profile import (
    WEIGHTS,
    VERDICT_APPLY_MIN,
    VERDICT_OPTIONAL_MIN,
    COMP_FLOOR,
    DOMAIN_KEYWORDS_STRONG,
    DOMAIN_KEYWORDS_SCALE_BOOST,
    ROLE_TYPE_BOOST,
    ROLE_TYPE_REDUCE,
    LEADERSHIP_KEYWORDS,
    COMPLEXITY_KEYWORDS,
    SENIORITY_BOOST_TITLES,
    SENIORITY_PENALIZE_TITLES,
    RED_FLAG_PHRASES,
)


def _to_number(token: str) -> float:
    token = token.strip().replace(",", "")
    if token.lower().endswith("k"):
        return float(token[:-1]) * 1000
    return float(token)


def parse_comp_floor(comp_text: str):
    """Best-effort lowest dollar figure found in a compensation string.
    Handles "$150,000 - $200,000" and "$150K - $200K" shapes. Returns
    None if nothing that looks like money is found."""
    if not comp_text:
        return None
    raw = re.findall(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?[kK])", comp_text)
    if not raw:
        return None
    return min(_to_number(m) for m in raw)


def score_domain(text: str) -> tuple:
    weight = WEIGHTS["domain_alignment"]
    hits = [k for k in DOMAIN_KEYWORDS_STRONG if k in text]
    if not hits:
        return weight * 0.3, "no explicit domain language found; scored low on title match alone"
    points = weight * 0.8
    if any(b in text for b in DOMAIN_KEYWORDS_SCALE_BOOST):
        points = weight
    return points, f"domain language: {', '.join(hits[:3])}"


def score_seniority(title: str) -> tuple:
    weight = WEIGHTS["seniority_alignment"]
    low = title.lower()
    if any(t in low for t in SENIORITY_PENALIZE_TITLES):
        return 0, "title reads junior or IC despite matching the title filter"
    if any(t in low for t in SENIORITY_BOOST_TITLES):
        return weight, "title matches a top-tier seniority keyword"
    return weight * 0.6, "title passed the filter but is not a top-tier seniority keyword"


def score_role_type(text: str) -> tuple:
    weight = WEIGHTS["role_type_fit"]
    boost = [k for k in ROLE_TYPE_BOOST if k in text]
    reduce = [k for k in ROLE_TYPE_REDUCE if k in text]
    if boost:
        return weight, f"strategic language: {', '.join(boost[:3])}"
    if reduce:
        return weight * 0.2, f"transactional language: {', '.join(reduce[:2])}"
    return weight * 0.5, "no strong role-type signal either way"


def score_compensation(comp_text: str) -> tuple:
    weight = WEIGHTS["compensation_alignment"]
    if not comp_text:
        return weight * 0.5, "no compensation data captured for this source; scored neutral", False
    floor = parse_comp_floor(comp_text)
    if floor is None:
        return weight * 0.5, "compensation text present but no figure could be parsed; scored neutral", True
    if floor >= COMP_FLOOR:
        return weight, f"comp floor ~${floor:,.0f} meets the ${COMP_FLOOR:,.0f} target", True
    return 0, f"comp floor ~${floor:,.0f} is below the ${COMP_FLOOR:,.0f} target", True


def score_remote(workplace_value: str) -> tuple:
    weight = WEIGHTS["remote_eligibility"]
    if not workplace_value:
        return weight * 0.5, "no workplace-type data captured for this source; scored neutral", False
    return weight, f"workplace type: {workplace_value} (already passed the onsite-outside-CO filter upstream)", True


def score_leadership(text: str) -> tuple:
    weight = WEIGHTS["leadership_signals"]
    hits = [k for k in LEADERSHIP_KEYWORDS if k in text]
    if hits:
        return weight, f"leadership language: {', '.join(hits[:3])}"
    return weight * 0.3, "no explicit leadership or coaching language"


def score_complexity(text: str) -> tuple:
    weight = WEIGHTS["complexity"]
    hits = [k for k in COMPLEXITY_KEYWORDS if k in text]
    if hits:
        return weight, f"complexity signals: {', '.join(hits[:2])}"
    return weight * 0.3, "no explicit complexity signal"


def check_red_flags(text_low: str, text_raw: str) -> list:
    hits = []
    for phrase in RED_FLAG_PHRASES:
        if phrase not in text_low:
            continue
        if phrase == "competitive pay" and re.search(r"\$\s?\d", text_raw):
            # a real range is stated elsewhere, so "competitive" alone is not the red flag
            continue
        hits.append(phrase)
    return hits


def compute_real_fit(record: dict) -> dict:
    title = record.get("title", "") or ""
    description = record.get("description", "") or ""
    comp_text = record.get("comp", "") or ""
    workplace_value = record.get("workplace_type") or record.get("workplace") or ""

    text_raw = f"{title} {description}"
    text_low = text_raw.lower()

    domain_pts, domain_note = score_domain(text_low)
    seniority_pts, seniority_note = score_seniority(title)
    role_pts, role_note = score_role_type(text_low)
    comp_pts, comp_note, comp_available = score_compensation(comp_text)
    remote_pts, remote_note, workplace_available = score_remote(workplace_value)
    leadership_pts, leadership_note = score_leadership(text_low)
    complexity_pts, complexity_note = score_complexity(text_low)

    breakdown = {
        "domain_alignment": {"points": round(domain_pts, 1), "of": WEIGHTS["domain_alignment"], "note": domain_note},
        "seniority_alignment": {"points": round(seniority_pts, 1), "of": WEIGHTS["seniority_alignment"], "note": seniority_note},
        "role_type_fit": {"points": round(role_pts, 1), "of": WEIGHTS["role_type_fit"], "note": role_note},
        "compensation_alignment": {"points": round(comp_pts, 1), "of": WEIGHTS["compensation_alignment"], "note": comp_note},
        "remote_eligibility": {"points": round(remote_pts, 1), "of": WEIGHTS["remote_eligibility"], "note": remote_note},
        "leadership_signals": {"points": round(leadership_pts, 1), "of": WEIGHTS["leadership_signals"], "note": leadership_note},
        "complexity": {"points": round(complexity_pts, 1), "of": WEIGHTS["complexity"], "note": complexity_note},
    }

    total = sum(v["points"] for v in breakdown.values())
    red_flags = check_red_flags(text_low, text_raw)

    if red_flags:
        verdict = "Skip"
    elif total >= VERDICT_APPLY_MIN:
        verdict = "Apply"
    elif total >= VERDICT_OPTIONAL_MIN:
        verdict = "Optional"
    else:
        verdict = "Skip"

    return {
        "score": round(total, 1),
        "verdict": verdict,
        "red_flags": red_flags,
        "breakdown": breakdown,
        "compensation_data_available": comp_available,
        "workplace_data_available": workplace_available,
    }


def format_real_fit_section(result: dict) -> str:
    lines = [f"### Real-Fit Score: {result['score']} / 100 — {result['verdict']}", ""]
    if result["red_flags"]:
        lines.append(f"**Red flag(s) detected:** {', '.join(result['red_flags'])}. Verdict forced to Skip.")
        lines.append("")
    lines.append("| Category | Points | Note |")
    lines.append("|---|---|---|")
    for key in WEIGHTS:
        b = result["breakdown"][key]
        label = key.replace("_", " ").title()
        lines.append(f"| {label} | {b['points']} / {b['of']} | {b['note']} |")
    notes = []
    if not result["compensation_data_available"]:
        notes.append("compensation is neutral, this source does not capture salary")
    if not result["workplace_data_available"]:
        notes.append("remote eligibility is neutral, this source does not capture workplace type")
    if notes:
        lines.append("")
        lines.append("_" + "; ".join(notes) + "._")
    return "\n".join(lines)
