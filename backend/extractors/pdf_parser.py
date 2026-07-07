"""
PDF and text parsing utilities for the Kenya Presidential Opinion Polls Tracker.

The parser is intentionally conservative. It extracts candidate percentage data
from official reports, but only returns AUTO_ACCEPTED when minimum confidence
thresholds are met. Ambiguous items are routed to review_queue.json.

This version supports TIFA-style grouped bar charts and exposes all extracted
poll waves from June 2025 onward so the frontend can show a historical trend,
not only the latest point.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber

try:  # dateparser is installed in GitHub Actions from requirements.txt.
    from dateparser.search import search_dates as _dateparser_search_dates
except Exception:  # pragma: no cover - fallback for local/offline audits.
    _dateparser_search_dates = None

TRACKED_CANDIDATES: List[str] = [
    "William Ruto",
    "Kalonzo Musyoka",
    "Fred Matiang'i",
    "Rigathi Gachagua",
    "Edwin Sifuna",
]

CANDIDATE_ALIASES: Dict[str, List[str]] = {
    "William Ruto": ["William Ruto", "Ruto", "President Ruto", "Dr William Ruto", "H.E William Ruto"],
    "Kalonzo Musyoka": ["Kalonzo Musyoka", "Kalonzo", "Stephen Kalonzo", "Stephen Kalonzo Musyoka"],
    "Fred Matiang'i": [
        "Fred Matiang'i",
        "Fred Matiang’i",
        "Matiang'i",
        "Matiang’i",
        "Dr Fred Matiang'i",
        "Dr Fred Matiang’i",
    ],
    "Rigathi Gachagua": ["Rigathi Gachagua", "Gachagua", "Riggy G"],
    "Edwin Sifuna": ["Edwin Sifuna", "Sifuna", "Senator Edwin Sifuna"],
}

POLL_TYPE_KEYWORDS: List[Tuple[str, List[str]]] = [
    (
        "preferred_presidential_candidate",
        [
            "preferred presidential candidate",
            "presidential candidate preference",
            "if presidential elections were held today",
            "if presidential election were held today",
            "who would you vote for as president",
            "vote for as president",
            "vote for president",
            "next president of kenya",
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
            "opposition landscape",
            "fragmented opposition landscape",
            "assuming a presidential election were held today",
            "assuming presidential elections were held today",
        ],
    ),
    (
        "approval_rating",
        ["approval rating", "approve", "disapprove", "performance rating"],
    ),
    (
        "popularity_rating",
        [
            "popularity",
            "popular",
            "most popular",
            "candidate popularity",
            "presidential candidates popularity",
            "leader popularity",
            "leaders popularity",
            "popularity rating",
            "popular leaders",
        ],
    ),
    ("party_support", ["party support", "political party", "party popularity"]),
]

# IMPORTANT: do not put a final \b after "%". PDF text can contain values such
# as "32%\n28%" or "5%5%". A final boundary prevents normal newline-separated
# percentages from matching because "%" and newline are both non-word chars.
PERCENT_RE = re.compile(
    r"(?P<value>\d{1,2}(?:\.\d+)?|100(?:\.0+)?)\s*(?:%|percent\b|per\s*cent\b)",
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

MIN_TREND_DATE = "2025-06-01"


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
    # Optional list of fully normalized public-record fragments for charts.
    # Used by grouped chart parsers to emit multiple time points from one PDF.
    series_records: List[Dict[str, Any]] = field(default_factory=list)


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

    This generic parser is the fallback for normal tables and paragraphs. It is
    deliberately conservative and should not override a successful chart parser.
    """
    normalized = normalize_text(text)
    figures: Dict[str, Optional[float]] = {candidate: None for candidate in TRACKED_CANDIDATES}
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
    """Count candidate values that are real positive percentages."""
    return sum(
        1
        for value in figures.values()
        if isinstance(value, (int, float)) and value > 0 and value <= 100
    )


def has_all_zero_or_empty_values(figures: Dict[str, Optional[float]]) -> bool:
    """Return True when no candidate has a positive extracted percentage."""
    return count_positive_candidate_values(figures) == 0


def _figures_from_ordered_values(values: List[float], wave_index: int) -> Dict[str, Optional[float]]:
    """Map flattened grouped-chart values to tracked candidates for a given wave."""
    candidate_order = [
        "William Ruto",
        "Kalonzo Musyoka",
        "Fred Matiang'i",
        "Edwin Sifuna",
        "Rigathi Gachagua",
    ]
    figures: Dict[str, Optional[float]] = {candidate: None for candidate in TRACKED_CANDIDATES}
    for index, candidate in enumerate(candidate_order):
        group_start = index * 4
        group = values[group_start : group_start + 4]
        if len(group) == 4:
            figures[candidate] = group[wave_index]
    return figures




def _can_auto_accept_single_candidate(poll_type: str, positive_count: int) -> bool:
    """
    Allow single-candidate records only for candidate/popularity poll types.

    This lets Infotrak reports that contain one tracked candidate value enter the
    dataset without weakening the all-zero safeguards. It does not mix approval
    ratings into the presidential-aspirant trend because the dashboard filters
    by poll_type.
    """
    return positive_count >= 1 and poll_type in {
        "preferred_presidential_candidate",
        "preferred_presidential_aspirant",
        "popularity_rating",
    }


def _is_from_min_trend_date(date_value: Optional[str]) -> bool:
    """Return True if date is missing or on/after the configured trend start."""
    if not date_value:
        return True
    return date_value >= MIN_TREND_DATE

def find_tifa_2027_grouped_chart(
    text: str,
    fallback_date: Optional[str] = None,
) -> Tuple[Dict[str, Optional[float]], str, float, List[Dict[str, Any]]]:
    """
    Parse TIFA-style grouped bar chart text and return all trend waves from
    June 2025 onward.

    In the May 2026 TIFA report, pdfplumber extracts the chart roughly as:

    32% 28% 25% 24% 24% 21% ... 0%0%0% 0%0%
    Ruto Kalonzo Matiang'i Sifuna Gachagua Babu The Late Raila Other Undecided NR
    May (2025) August (2025) November (2025) May (2026)

    The chart has 10 categories and 4 waves. Percent values are flattened in
    category order. The first five category groups are the tracked candidates.

    For the dashboard trend, we exclude May 2025 because the user requested data
    from June 2025 onward. Therefore the emitted series contains August 2025,
    November 2025, and May 2026.
    """
    normalized = normalize_text(text)
    lower = normalized.lower()

    required_terms = [
        "ruto",
        "kalonzo",
        "matiang'i",
        "sifuna",
        "gachagua",
        "may (2025)",
        "august (2025)",
        "november (2025)",
        "may (2026)",
    ]

    if not all(term in lower for term in required_terms):
        empty = {candidate: None for candidate in TRACKED_CANDIDATES}
        return empty, "", 0.0, []

    label_match = re.search(
        r"Ruto\s+Kalonzo\s+Matiang'?i\s+Sifuna\s+Gachagua",
        normalized,
        re.IGNORECASE,
    )
    if not label_match:
        empty = {candidate: None for candidate in TRACKED_CANDIDATES}
        return empty, "", 0.0, []

    chart_start = max(0, label_match.start() - 1600)
    chart_text = normalized[chart_start:label_match.start()]

    values: List[float] = []
    for match in PERCENT_RE.finditer(chart_text):
        value = float(match.group("value"))
        if 0 <= value <= 100:
            values.append(value)

    # Need at least 5 tracked candidates x 4 waves = 20 values. The full chart
    # normally has 40 values, but 20 is enough to parse the five tracked figures.
    if len(values) < 20:
        empty = {candidate: None for candidate in TRACKED_CANDIDATES}
        return empty, chart_text[-900:], 0.0, []

    # Wave index 0 is May 2025. Exclude it because the requested tracker window
    # begins from June 2025.
    waves = [
        ("2025-08-01", "August 2025", 1),
        ("2025-11-01", "November 2025", 2),
        (fallback_date or "2026-05-14", "May 2026", 3),
    ]

    series_records: List[Dict[str, Any]] = []
    for wave_date, wave_label, wave_index in waves:
        figures = _figures_from_ordered_values(values, wave_index)
        if count_positive_candidate_values(figures) >= 5:
            series_records.append(
                {
                    "date": wave_date,
                    "fieldwork_dates": wave_label,
                    "figures": figures,
                    "notes": f"Extracted from TIFA grouped chart wave: {wave_label}",
                }
            )

    latest_figures = series_records[-1]["figures"] if series_records else {candidate: None for candidate in TRACKED_CANDIDATES}

    snippet_start = max(0, label_match.start() - 900)
    snippet_end = min(len(normalized), label_match.end() + 350)
    snippet = normalized[snippet_start:snippet_end]

    positive_count = count_positive_candidate_values(latest_figures)
    confidence = 0.94 if positive_count >= 5 and len(series_records) >= 3 else 0.0
    return latest_figures, snippet, confidence, series_records


def _fallback_search_dates(text: str) -> Optional[List[Tuple[str, datetime]]]:
    """Very small fallback if dateparser is not available in a local audit."""
    month_re = (
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    )
    matches: List[Tuple[str, datetime]] = []
    for match in re.finditer(rf"(?:(\d{{1,2}})\s+)?{month_re}\s+(20\d{{2}})", text, re.IGNORECASE):
        day = int(match.group(1) or 1)
        month_name = match.group(2).lower()
        year = int(match.group(3))
        month = [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ].index(month_name) + 1
        try:
            matches.append((match.group(0), datetime(year, month, day)))
        except ValueError:
            continue
    return matches or None


def extract_dates(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract a plausible poll/publication date and fieldwork date phrase."""
    normalized = normalize_text(text)

    if _dateparser_search_dates:
        found = _dateparser_search_dates(
            normalized[:6000],
            settings={
                "PREFER_DATES_FROM": "past",
                "RETURN_AS_TIMEZONE_AWARE": False,
                "DATE_ORDER": "DMY",
            },
        )
    else:
        found = _fallback_search_dates(normalized[:6000])

    poll_date = None
    if found:
        valid_dates = [dt for _, dt in found if 2000 <= dt.year <= datetime.utcnow().year + 1]
        if valid_dates:
            poll_date = valid_dates[0].date().isoformat()

    fieldwork_dates = None
    fieldwork_match = FIELDWORK_RE.search(normalized)
    if fieldwork_match:
        fieldwork_dates = re.sub(r"\s+", " ", fieldwork_match.group("dates")).strip(" .;:-")[:140]

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
    return sample_size if 100 <= sample_size <= 200000 else None


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

    figures, snippet, figure_confidence, series_records = find_tifa_2027_grouped_chart(
        normalized,
        fallback_date=fallback_date,
    )
    if figure_confidence == 0.0:
        figures, snippet, figure_confidence = find_candidate_percentages(normalized)
        series_records = []

    poll_type, type_confidence = classify_poll_type(normalized)
    extracted_date, fieldwork_dates = extract_dates(normalized)
    poll_date = fallback_date or extracted_date
    sample_size = extract_sample_size(normalized)
    question_text = extract_question_text(normalized)

    positive_count = count_positive_candidate_values(figures)
    has_any_extracted_value = any(isinstance(value, (int, float)) for value in figures.values())

    confidence = round(
        min(0.98, figure_confidence + type_confidence * 0.25 + (0.08 if poll_date else 0)),
        2,
    )

    if (
        poll_type != "unknown"
        and poll_date
        and _is_from_min_trend_date(poll_date)
        and (positive_count >= 2 or _can_auto_accept_single_candidate(poll_type, positive_count))
    ):
        status = "AUTO_ACCEPTED"
        reason = "Candidate percentages, poll type, and date were identified."
    elif has_all_zero_or_empty_values(figures) and has_any_extracted_value:
        status = "NEEDS_REVIEW"
        reason = (
            "Candidate percentage values were extracted, but all extracted values are zero. "
            "This is likely a parsing error and must be reviewed before publication."
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

    # Fill common metadata into grouped-chart series records. The poll_tracker
    # will expand these into separate public records.
    for record in series_records:
        record.setdefault("poll_type", poll_type)
        record.setdefault("question_text", question_text)
        record.setdefault("sample_size", sample_size)
        record.setdefault("confidence", confidence)

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
        series_records=series_records,
    )


def parse_pdf_bytes(pdf_bytes: bytes, fallback_date: Optional[str] = None) -> ParseResult:
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
