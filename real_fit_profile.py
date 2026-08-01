"""
real_fit_profile.py

Real-Fit Score profile and weights for Tracey Mallory's search.
Ported from the Zapier job-search-agent instructions. Edit the values
below to retune scoring without touching real_fit_score.py.
"""

# Weighted categories. Must sum to 100.
WEIGHTS = {
    "domain_alignment": 30,
    "seniority_alignment": 20,
    "role_type_fit": 15,
    "compensation_alignment": 10,
    "remote_eligibility": 10,
    "leadership_signals": 10,
    "complexity": 5,
}

# Verdict thresholds, applied to the final 0-100 score.
VERDICT_APPLY_MIN = 75
VERDICT_OPTIONAL_MIN = 50
# Below VERDICT_OPTIONAL_MIN is Skip. Red flags below auto-skip regardless.

COMP_FLOOR = 130_000

# Domain alignment: People-function language that should score high.
DOMAIN_KEYWORDS_STRONG = [
    "people operations", "people & culture", "people and culture",
    "human resources", "hr strategy", "talent management",
    "organizational development", "org development",
]
# Boosts on top of a domain hit, not a replacement for one.
DOMAIN_KEYWORDS_SCALE_BOOST = [
    "multi-market", "multi-state", "enterprise-scale", "global",
]

# Role-type fit: strategic language boosts, transactional language reduces.
ROLE_TYPE_BOOST = [
    "strategic", "organizational design", "culture", "hrbp",
    "business partner", "change management",
]
ROLE_TYPE_REDUCE = [
    "compliance", "transactional", "administrative", "data entry",
]

# Leadership signals.
LEADERSHIP_KEYWORDS = [
    "coaching", "mentoring", "organizational leadership",
    "transformational", "leadership development",
]

# Complexity.
COMPLEXITY_KEYWORDS = [
    "multi-department", "cross-functional", "org change",
    "enterprise-scale", "multi-site",
]

# Seniority. Reuses the same tiers the title filter already keys off of.
SENIORITY_BOOST_TITLES = [
    "chief people", "cpo", "vp of people", "vp of hr",
    "vice president of people", "vice president of human resources",
    "head of people", "head of hr",
]
SENIORITY_PENALIZE_TITLES = [
    "coordinator", "assistant", "analyst", "specialist",
]

# Red flags. A hit here forces an auto-skip regardless of score.
RED_FLAG_PHRASES = [
    "client confidential", "undisclosed client",
    "onsite as needed", "hybrid required",
    "competitive pay",  # only flagged when no range is also present
]
