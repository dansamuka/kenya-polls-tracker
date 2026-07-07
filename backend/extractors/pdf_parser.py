"""
PDF and text parsing utilities for the Kenya Presidential Opinion Polls Tracker.

The parser is intentionally conservative. It attempts to extract candidate-name /
percentage pairs from official poll reports, but only returns AUTO_ACCEPTED when
minimum confidence thresholds are met. Ambiguous items should be routed to the
review queue rather than published.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

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
    "William Ruto": ["William Ruto", "Ruto", "President Ruto"],
    "Kalonzo Musyoka": ["Kalonzo Musyoka", "Kalonzo"],
    "Fred Matiang'i": [
        "Fred Matiang'i",
        "Fred Matiang’i",
        "Matiang'i",
        "Matiang’i",
        "Dr Fred Matiang'i",
        "Dr Fred Matiang’i",
    ],
    "Rigathi Gachagua": ["Rigathi Gachagua", "Gachagua"],
    "Edwin Sifuna": ["Edwin Sifuna", "Sifuna"],
}

POLL_TYPE_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("preferred_presidential_candidate", ["preferred presidential candidate", "presidential candidate preference"]),
    ("preferred_presidential_aspirant", ["preferred presidential aspirant", "presidential aspirant", "presidential hopeful"]),
    ("approval_rating", ["approval rating", "approve", "disapprove", "performance rating"]),
    ("popularity_rating", ["popularity", "popular", "most popular"]),
    ("party_support", ["party support", "political party", "party popularity"]),
]

PERCENT_RE = re.compile(r"(?P<value>\d{1,2}(?:\.\d+)?|100(?:\.0+)?)\s*(?:%|percent|per\s*cent)\b", re.IGNORECASE)
SAMPLE_SIZE_RE = re.compile(r"(?:sample size|sample|n)\s*[:=]?\s*(?:of\s*)?(?P<n>\d{3,6})", re.IGNORECASE)
FIELDWORK_RE = re.compile(
    r"(?:fieldwork|data collection|interviews conducted|conducted)\s*(?:was\s*)?(?:between|from)?\s*(?P<dates>.{0,100})",
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
        matches = sum(1 for kw in keywords if kw in lower)
        if matches:
            score = min(1.0, 0.35 + matches * 0.2)
            if score > best_score:
                best_type = poll_type
                best_score = score
    return best_type, best_score


def find_candidate_percentages(text: str, window_chars: int = 170) -> Tuple[Dict[str, Optional[float]], str, float]:
    """
    Locate candidate aliases and nearby percentage values.

    The parser looks around each alias occurrence rather than assuming tables are
    perfectly structured. This works for many text-extracted PDFs but remains
    intentionally conservative.
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
                for pct in PERCENT_RE.finditer(window):
                    value = float(pct.group("value"))
                    if not 0 <= value <= 100:
                        continue
                    alias_mid = match.start() - start + len(alias) // 2
                    pct_mid = pct.start() + len(pct.group(0)) // 2
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
        valid_dates = [dt for _, dt in found if 2000 <= dt.year <= datetime.utcnow().year + 1]
        if valid_dates:
            poll_date = valid_dates[0].date().isoformat()

    fieldwork_dates = None
    m = FIELDWORK_RE.search(normalized)
    if m:
        fieldwork_dates = re.sub(r"\s+", " ", m.group("dates")).strip(" .;:-")[:140]

    return poll_date, fieldwork_dates


def extract_sample_size(text: str) -> Optional[int]:
    """Extract a sample-size value when it appears in common report language."""
    m = SAMPLE_SIZE_RE.search(normalize_text(text))
    if not m:
        return None
    try:
        n = int(m.group("n"))
        return n if 100 <= n <= 200000 else None
    except ValueError:
        return None


def extract_question_text(text: str) -> Optional[str]:
    """Extract a likely question phrase if the report labels it explicitly."""
    m = QUESTION_RE.search(normalize_text(text))
    if not m:
        return None
    question = re.sub(r"\s+", " ", m.group("question")).strip()
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

    figures, snippet, figure_confidence = find_candidate_percentages(normalized)
    poll_type, type_confidence = classify_poll_type(normalized)
    poll_date, fieldwork_dates = extract_dates(normalized)
    poll_date = poll_date or fallback_date
    sample_size = extract_sample_size(normalized)
    question_text = extract_question_text(normalized)

    found_values = {k: v for k, v in figures.items() if isinstance(v, (int, float))}
    found_count = len(found_values)

    confidence = round(min(0.98, figure_confidence + type_confidence * 0.25 + (0.08 if poll_date else 0)), 2)

    if found_count >= 2 and poll_type != "unknown" and poll_date:
        status = "AUTO_ACCEPTED"
        reason = "Candidate percentages, poll type, and date were identified."
    elif found_count > 0:
        status = "NEEDS_REVIEW"
        missing = []
        if poll_type == "unknown":
            missing.append("poll type")
        if not poll_date:
            missing.append("poll date")
        if found_count < 2:
            missing.append("at least two tracked candidate values")
        reason = "Candidate values found but review is needed for: " + ", ".join(missing)
    else:
        status = "REJECTED"
        reason = "No tracked candidate percentages were found."

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


def parse_pdf_bytes(pdf_bytes: bytes, fallback_date: Optional[str] = None) -> ParseResult:
    """Extract and parse poll data from PDF bytes."""
    try:
        text = extract_text_from_pdf_bytes(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 - surface parser failure in review/reject flow
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
