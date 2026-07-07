"""
PDF and text parsing utilities for the Kenya Presidential Opinion Polls Tracker.

The parser is intentionally conservative. It attempts to extract candidate-name /
percentage pairs from official poll reports, but only returns AUTO_ACCEPTED when
minimum confidence thresholds are met. Ambiguous items should be routed to the
review queue rather than published.

Special handling is included for TIFA-style grouped bar charts where percentages
appear before candidate labels in extracted PDF text.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pdfplumber
from dateparser.search import search_dates


TRACKED_CANDIDATES: List[str] = [
    "William Ruto",
    "Kalonzo Musyoka",
    "Fred Matiang'i",
    "Rigathi Gachagua",
    "Edwin Sifuna",
]


CANDIDATE_ALIASES: Dict[str, List[str]] = {
    "William Ruto": [
        "William Ruto",
        "Ruto",
        "President Ruto",
    ],
    "Kalonzo Musyoka": [
        "Kalonzo Musyoka",
        "Kalonzo",
    ],
    "Fred Matiang'i": [
        "Fred Matiang'i",
        "Fred Matiang’i",
        "Matiang'i",
        "Matiang’i",
        "Dr Fred Matiang'i",
        "Dr Fred Matiang’i",
    ],
    "Rigathi Gachagua": [
        "Rigathi Gachagua",
        "Gachagua",
    ],
    "Edwin Sifuna": [
        "Edwin Sifuna",
        "Sifuna",
    ],
}


POLL_TYPE_KEYWORDS: List[Tuple[str, List[str]]] = [
    (
        "preferred_presidential_candidate",
        [
            "preferred presidential candidate",
            "presidential candidate preference",
        ],
    ),
    (
        "preferred_presidential_aspirant",
        [
            "preferred presidential aspirant",
            "presidential aspirant",
            "presidential hopeful",
            "2027 presidential race",
            "presidential race",
            "presidential contest",
            "2027 election prospects",
            "election prospects",
        ],
    ),
    (
        "approval_rating",
        [
            "approval rating",
            "approve",
            "disapprove",
            "performance rating",
        ],
    ),
    (
        "popularity_rating",
        [
            "popularity",
            "popular",
            "most popular",
            "candidate popularity",
            "presidential candidates popularity",
        ],
    ),
    (
        "party_support",
        [
            "party support",
            "political party",
            "party popularity",
        ],
    ),
]


PERCENT_RE = re.compile(
    r"(?P<value>\d{1,2}(?:\.\d+)?|100(?:\.0+)?)\s*(?:%|percent|per\s*cent)\b",
    re.IGNORECASE,
)


SAMPLE_SIZE_RE = re.compile(
    r"(?:sample size|sample|n)\s*[:=]?\s*(?:of\s*)?(?P<n>\d{3,6})",
    re.IGNORECASE,
)


FIELDWORK_RE = re.compile(
    r"(?:fieldwork|data collection|interviews conducted|conducted)\s*"
    r"(?:was\s*)?(?:between|from)?\s*(?P<dates>.{0,100})",
    re.IGNORECASE,
)


QUESTION_RE = re.compile(
    r"(?:question|asked)\s*[:\-]\s*(?P<question>.{20,250}?)(?:\n|$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class ParseResult:
    status: str
    figures: Dict[str, Optional[float]]
    poll_type: str
    poll_date: Optional[str]
    fieldwork_dates: Optional[str]
    sample_size: Optional[int]
    question_text: Optional[str]
    confidence: float
    raw_snippet: str
    reason: str


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes using pdfplumber."""
    text_parts: List[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text)

    return "\n".join(text_parts)


def normalize_text(text: str) -> str:
    """Normalize common punctuation and whitespace inconsistencies."""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def classify_poll_type(text: str) -> Tuple[str, float]:
    """Infer the poll type using keyword signals."""
    lower = text.lower()
    best_type = "unknown"
    best_score = 0.0

    for poll_type, keywords in POLL_TYPE_KEYWORDS:
        matches = sum(1 for keyword in keywords if keyword in lower)
        if matches:
            score = min(1.0, 0.35 + matches * 0.2)
            if score > best_score:
                best_type = poll_type
                best_score = score

    return best_type, best_score


def find_candidate_percentages(
    text: str,
    window_chars: int = 170,
) -> Tuple[Dict[str, Optional[float]], str, float]:
    """
    Locate candidate aliases and nearby percentage values.

    This generic parser looks around each candidate alias occurrence. It works
    for ordinary paragraphs and tables where the value is near the name, but it
    can fail on grouped charts where all percentages appear before labels.
    """
    normalized = normalize_text(text)

    figures: Dict[str, Optional[float]] = {
        candidate: None for candidate in TRACKED_CANDIDATES
    }

    snippets: List[str] = []
    evidence_count = 0

    for canonical, aliases in CANDIDATE_ALIASES.items():
        best_value: Optional[float] = None
        best_distance: Optional[int] = None
        best_snippet = ""

        for alias in aliases:
            pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)

            for match in pattern.finditer(normalized):
                start = max(0, match.start() - window_chars)
                end = min(len(normalized), match.end() + window_chars)
                window = normalized[start:end]

                for percent_match in PERCENT_RE.finditer(window):
                    value = float(percent_match.group("value"))

                    if not 0 <= value <= 100:
                        continue

                    alias_mid = match.start() - start + len(alias) // 2
                    pct_mid = percent_match.start() + len(percent_match.group(0)) // 2
                    distance = abs(alias_mid - pct_mid)

                    if best_distance is None or distance < best_distance:
                        best_value = value
                        best_distance = distance
                        best_snippet = window.strip()

        if best_value is not None:
            figures[canonical] = best_value
            evidence_count += 1
            snippets.append(best_snippet[:500])

    confidence = min(0.85, 0.25 + evidence_count * 0.12)
    return figures, "\n---\n".join(snippets[:5]), confidence


def count_positive_candidate_values(figures: Dict[str, Optional[float]]) -> int:
    """
    Count extracted candidate values that are real positive percentages.

    This prevents bad records like all candidates = 0.0% from being
    automatically published to polls_data.json.
    """
    return sum(
        1
        for value in figures.values()
        if isinstance(value, (int, float)) and value > 0 and value <= 100
    )


def has_all_zero_or_empty_values(figures: Dict[str, Optional[float]]) -> bool:
    """
    Return True when no candidate has a positive extracted percentage.

    Examples that should not be auto-published:
    - all values are None
    - all values are 0
    - a mix of None and 0
    """
    return count_positive_candidate_values(figures) == 0


def find_tifa_2027_grouped_chart(
    text: str,
) -> Tuple[Dict[str, Optional[float]], str, float]:
    """
    Parse TIFA-style grouped bar chart text where percentages appear before
    candidate labels.

    Example structure found in extracted PDF text:

    32%
    28%
    25%
    24%
    24%
    21%
    18%
    19%
    ...
    Ruto Kalonzo Matiang'i Sifuna Gachagua ...
    May (2025) August (2025) November (2025) May (2026)

    The first five candidate groups appear in this order:

    - Ruto
    - Kalonzo
    - Matiang'i
    - Sifuna
    - Gachagua

    Each candidate has four values:

    - May 2025
    - August 2025
    - November 2025
    - May 2026

    The latest wave is therefore the fourth value in each candidate group.
    """
    normalized = normalize_text(text)

    required_terms = [
        "Ruto",
        "Kalonzo",
        "Matiang'i",
        "Sifuna",
        "Gachagua",
        "May (2025)",
        "August (2025)",
        "November (2025)",
        "May (2026)",
    ]

    lower = normalized.lower()

    if not all(term.lower() in lower for term in required_terms):
        return (
            {candidate: None for candidate in TRACKED_CANDIDATES},
            "",
            0.0,
        )

    label_match = re.search(
        r"Ruto\s+Kalonzo\s+Matiang'?i\s+Sifuna\s+Gachagua",
        normalized,
        re.IGNORECASE,
    )

    if not label_match:
        return (
            {candidate: None for candidate in TRACKED_CANDIDATES},
            "",
            0.0,
        )

    # Look backwards before the candidate-label row where chart percentages appear.
    chart_start = max(0, label_match.start() - 1200)
    chart_text = normalized[chart_start:label_match.start()]

    values: List[float] = []

    for match in PERCENT_RE.finditer(chart_text):
        value = float(match.group("value"))
        if 0 <= value <= 100:
            values.append(value)

    # Need at least 5 candidates x 4 waves = 20 values.
    if len(values) < 20:
        return (
            {candidate: None for candidate in TRACKED_CANDIDATES},
            chart_text[-700:],
            0.0,
        )

    candidate_order = [
        "William Ruto",
        "Kalonzo Musyoka",
        "Fred Matiang'i",
        "Edwin Sifuna",
        "Rigathi Gachagua",
    ]

    figures: Dict[str, Optional[float]] = {
        candidate: None for candidate in TRACKED_CANDIDATES
    }

    for index, candidate in enumerate(candidate_order):
        group_start = index * 4
        group = values[group_start: group_start + 4]

        if len(group) == 4:
            # Fourth value is May 2026, the latest wave.
            figures[candidate] = group[3]

    snippet_start = max(0, label_match.start() - 900)
    snippet_end = min(len(normalized), label_match.end() + 300)
    snippet = normalized[snippet_start:snippet_end]

    positive_count = count_positive_candidate_values(figures)
    confidence = 0.9 if positive_count >= 5 else 0.0

    return figures, snippet, confidence


def extract_dates(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract a plausible poll/publication date and fieldwork date phrase."""
    normalized = normalize_text(text)

    found = search_dates(
        normalized[:6000],
        settings={
            "PREFER_DATES_FROM": "past",
            "RETURN_AS_TIMEZONE_AWARE": False,
            "DATE_ORDER": "DMY",
        },
    )

    poll_date = None

    if found:
        valid_dates = [
            dt
            for _, dt in found
            if 2000 <= dt.year <= datetime.utcnow().year + 1
        ]

        if valid_dates:
            poll_date = valid_dates[0].date().isoformat()

    fieldwork_dates = None
    fieldwork_match = FIELDWORK_RE.search(normalized)

    if fieldwork_match:
        fieldwork_dates = re.sub(
            r"\s+",
            " ",
            fieldwork_match.group("dates"),
        ).strip(" .;:-")[:140]

    return poll_date, fieldwork_dates


def extract_sample_size(text: str) -> Optional[int]:
    """Extract a sample-size value when it appears in common report language."""
    match = SAMPLE_SIZE_RE.search(normalize_text(text))

    if not match:
        return None

    try:
        sample_size = int(match.group("n"))
    except ValueError:
        return None

    if 100 <= sample_size <= 200000:
        return sample_size

    return None


def extract_question_text(text: str) -> Optional[str]:
    """Extract a likely question phrase if the report labels it explicitly."""
    match = QUESTION_RE.search(normalize_text(text))

    if not match:
        return None

    question = re.sub(r"\s+", " ", match.group("question")).strip()
    return question[:300]


def parse_poll_text(text: str, fallback_date: Optional[str] = None) -> ParseResult:
    """Parse normalized poll data from extracted PDF or webpage text."""
    normalized = normalize_text(text)

    if not normalized:
        return ParseResult(
            status="REJECTED",
            figures={candidate: None for candidate in TRACKED_CANDIDATES},
            poll_type="unknown",
            poll_date=fallback_date,
            fieldwork_dates=None,
            sample_size=None,
            question_text=None,
            confidence=0.0,
            raw_snippet="",
            reason="No extractable text found.",
        )

    # First try the TIFA grouped-chart parser.
    figures, snippet, figure_confidence = find_tifa_2027_grouped_chart(normalized)

    # If that special parser does not apply, fall back to generic nearby-value parsing.
    if figure_confidence == 0.0:
        figures, snippet, figure_confidence = find_candidate_percentages(normalized)

    poll_type, type_confidence = classify_poll_type(normalized)
    poll_date, fieldwork_dates = extract_dates(normalized)
    poll_date = poll_date or fallback_date
    sample_size = extract_sample_size(normalized)
    question_text = extract_question_text(normalized)

    positive_count = count_positive_candidate_values(figures)

    has_any_extracted_value = any(
        isinstance(value, (int, float))
        for value in figures.values()
    )

    confidence = round(
        min(
            0.98,
            figure_confidence
            + type_confidence * 0.25
            + (0.08 if poll_date else 0),
        ),
        2,
    )

    if positive_count >= 2 and poll_type != "unknown" and poll_date:
        status = "AUTO_ACCEPTED"
        reason = "Candidate percentages, poll type, and date were identified."

    elif has_all_zero_or_empty_values(figures) and has_any_extracted_value:
        status = "NEEDS_REVIEW"
        reason = (
            "Candidate percentage values were extracted, but all extracted values "
            "are zero. This is likely a parsing error and must be reviewed before "
            "publication."
        )

    elif positive_count > 0:
        status = "NEEDS_REVIEW"
        missing = []

        if poll_type == "unknown":
            missing.append("poll type")

        if not poll_date:
            missing.append("poll date")

        if positive_count < 2:
            missing.append("at least two positive tracked candidate values")

        reason = "Candidate values found but review is needed for: " + ", ".join(missing)

    else:
        status = "REJECTED"
        reason = "No positive tracked candidate percentages were found."

    return ParseResult(
        status=status,
        figures=figures,
        poll_type=poll_type,
        poll_date=poll_date,
        fieldwork_dates=fieldwork_dates,
        sample_size=sample_size,
        question_text=question_text,
        confidence=confidence,
        raw_snippet=snippet or normalized[:700],
        reason=reason,
    )


def parse_pdf_bytes(
    pdf_bytes: bytes,
    fallback_date: Optional[str] = None,
) -> ParseResult:
    """Extract and parse poll data from PDF bytes."""
    try:
        text = extract_text_from_pdf_bytes(pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        return ParseResult(
            status="REJECTED",
            figures={candidate: None for candidate in TRACKED_CANDIDATES},
            poll_type="unknown",
            poll_date=fallback_date,
            fieldwork_dates=None,
            sample_size=None,
            question_text=None,
            confidence=0.0,
            raw_snippet="",
            reason=f"PDF extraction failed: {exc}",
        )

    return parse_poll_text(text, fallback_date=fallback_date)
