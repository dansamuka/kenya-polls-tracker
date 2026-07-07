"""
Kenya Presidential Opinion Polls Tracker backend.

Run locally:
    python backend/poll_tracker.py

This script discovers official pollster releases, downloads official pages/PDFs,
extracts candidate percentages, and writes clean records to data/polls_data.json.
Ambiguous records are routed to data/review_queue.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateparser import parse as parse_date

# Allow running from repository root or backend directory.
CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from extractors import infotrak, tifa  # noqa: E402
from extractors.pdf_parser import TRACKED_CANDIDATES, parse_pdf_bytes, parse_poll_text  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
POLLS_FILE = DATA_DIR / "polls_data.json"
REVIEW_FILE = DATA_DIR / "review_queue.json"
SOURCES_FILE = DATA_DIR / "sources_registry.json"
DOWNLOAD_DIR = REPO_ROOT / ".cache" / "downloads"

USER_AGENT = os.getenv(
    "KENYA_POLLS_USER_AGENT",
    "KenyaPollsTracker/1.0 (+https://github.com/your-org/kenya-polls-tracker)",
)
REQUEST_TIMEOUT = int(os.getenv("KENYA_POLLS_TIMEOUT", "30"))
ENABLE_TWITTER_DISCOVERY = os.getenv("ENABLE_TWITTER_DISCOVERY", "false").lower() == "true"

OFFICIAL_DOMAINS = ["tifaresearch.com", "www.tifaresearch.com", "infotrakresearch.com", "www.infotrakresearch.com"]


class PollTrackerError(Exception):
    """Base error for tracker operations."""


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_data_files() -> None:
    """Create required directories and empty JSON files if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for path in [POLLS_FILE, REVIEW_FILE, SOURCES_FILE]:
        if not path.exists():
            path.write_text("[]\n", encoding="utf-8")


def load_json_list(path: Path) -> List[Dict[str, Any]]:
    """Load a JSON list safely."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def write_json_list(path: Path, data: List[Dict[str, Any]]) -> bool:
    """Write JSON list and return True if file content changed."""
    new_text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    if new_text != old_text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def stable_source_id(pollster: str, title: str, url: str) -> str:
    """Generate stable deterministic source ID."""
    raw = f"{pollster}|{title}|{url}".lower().strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def sha256_bytes(content: bytes) -> str:
    """Calculate SHA-256 hash for bytes."""
    return hashlib.sha256(content).hexdigest()


def normalize_date(value: Optional[str]) -> Optional[str]:
    """Normalize a date-like string into YYYY-MM-DD when possible."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    parsed = parse_date(value, settings={"DATE_ORDER": "DMY", "PREFER_DATES_FROM": "past"})
    if parsed and 2000 <= parsed.year <= datetime.utcnow().year + 1:
        return parsed.date().isoformat()
    return None


def is_official_url(url: Optional[str]) -> bool:
    """Check that a URL belongs to a monitored official domain."""
    if not url:
        return False
    lowered = url.lower()
    return any(domain in lowered for domain in OFFICIAL_DOMAINS)


def http_get(url: str, accept: str = "*/*") -> requests.Response:
    """Make a polite HTTP GET request."""
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    response.raise_for_status()
    return response


def discover_all_sources() -> List[Dict[str, Any]]:
    """Run all official source discovery modules."""
    discovered: List[Dict[str, Any]] = []
    for discoverer in [tifa.discover_sources, infotrak.discover_sources]:
        try:
            discovered.extend(discoverer(timeout=REQUEST_TIMEOUT))
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: source discovery failed for {discoverer.__module__}: {exc}")
    if ENABLE_TWITTER_DISCOVERY:
        discovered.extend(discover_twitter_sources())
    return discovered


def discover_twitter_sources() -> List[Dict[str, Any]]:
    """
    Optional X/Twitter discovery using Tweepy.

    This is disabled by default. It requires X API credentials and should only be
    used to discover official report links, not as the source of poll numbers.
    """
    try:
        import tweepy  # type: ignore
    except ImportError:
        print("Warning: tweepy is not installed; skipping optional X discovery.")
        return []

    bearer_token = os.getenv("X_BEARER_TOKEN")
    if not bearer_token:
        print("Warning: ENABLE_TWITTER_DISCOVERY=true but X_BEARER_TOKEN is missing.")
        return []

    client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)
    accounts = ["TifaResearch", "InfotrakKenya"]
    keywords = [
        "presidential poll",
        "popularity",
        "approval rating",
        "candidate ranking",
        "Kenya poll",
        "2027 poll",
    ]
    sources: List[Dict[str, Any]] = []
    for account in accounts:
        query = f"from:{account} ({' OR '.join([repr(k) for k in keywords])}) -is:retweet"
        try:
            response = client.search_recent_tweets(
                query=query,
                tweet_fields=["created_at", "entities"],
                max_results=25,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: X discovery failed for @{account}: {exc}")
            continue
        for tweet in response.data or []:
            urls = []
            entities = getattr(tweet, "entities", None) or {}
            for item in entities.get("urls", []):
                expanded = item.get("expanded_url") or item.get("url")
                if expanded:
                    urls.append(expanded)
            for url in urls:
                if not is_official_url(url):
                    continue
                sources.append(
                    {
                        "pollster": "TIFA Research" if account == "TifaResearch" else "Infotrak Research",
                        "title": f"Official X discovery from @{account}",
                        "page_url": url,
                        "pdf_url": url if url.lower().split("?")[0].endswith(".pdf") else None,
                        "published_date": getattr(tweet, "created_at", None).date().isoformat()
                        if getattr(tweet, "created_at", None)
                        else None,
                    }
                )
    return sources


def merge_sources(discovered: List[Dict[str, Any]], registry: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Merge discovered sources into registry and return updated registry and count of new items."""
    now = utc_now_iso()
    by_key = {}
    for item in registry:
        key = item.get("source_id") or stable_source_id(
            item.get("pollster", ""), item.get("title", ""), item.get("pdf_url") or item.get("page_url") or ""
        )
        item["source_id"] = key
        by_key[key] = item

    new_count = 0
    for src in discovered:
        pollster = src.get("pollster") or "Unknown"
        title = (src.get("title") or "Untitled official source").strip()
        page_url = src.get("page_url")
        pdf_url = src.get("pdf_url")
        canonical_url = pdf_url or page_url
        if not canonical_url or not is_official_url(canonical_url):
            continue
        source_id = stable_source_id(pollster, title, canonical_url)
        published_date = normalize_date(src.get("published_date"))
        if source_id not in by_key:
            by_key[source_id] = {
                "source_id": source_id,
                "pollster": pollster,
                "title": title,
                "page_url": page_url,
                "pdf_url": pdf_url,
                "published_date": published_date,
                "first_seen_at": now,
                "last_checked_at": now,
                "sha256": None,
                "processing_status": "discovered",
            }
            new_count += 1
        else:
            by_key[source_id]["last_checked_at"] = now
            if pdf_url and not by_key[source_id].get("pdf_url"):
                by_key[source_id]["pdf_url"] = pdf_url
            if published_date and not by_key[source_id].get("published_date"):
                by_key[source_id]["published_date"] = published_date

    merged = sorted(by_key.values(), key=lambda x: (x.get("published_date") or "9999-99-99", x.get("title") or ""))
    return merged, new_count


def discover_pdf_from_page(page_url: str) -> Optional[str]:
    """Fetch an official article page and find the most likely PDF link."""
    response = http_get(page_url, accept="text/html,application/xhtml+xml")
    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    for link in soup.select("a[href]"):
        href = link.get("href", "").strip()
        absolute = urljoin(page_url, href)
        label = link.get_text(" ", strip=True).lower()
        if absolute.lower().split("?")[0].endswith(".pdf"):
            candidates.append(absolute)
        elif "download" in label and ".pdf" in absolute.lower():
            candidates.append(absolute)
    for candidate in candidates:
        if is_official_url(candidate):
            return candidate
    return candidates[0] if candidates else None


def extract_text_from_page(page_url: str) -> str:
    """Extract visible article text from an official HTML page."""
    response = http_get(page_url, accept="text/html,application/xhtml+xml")
    soup = BeautifulSoup(response.text, "html.parser")
    for bad in soup.select("script,style,noscript,svg,iframe"):
        bad.decompose()
    article = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    return article.get_text("\n", strip=True)


def download_pdf(pdf_url: str) -> bytes:
    """Download PDF bytes from official URL."""
    response = http_get(pdf_url, accept="application/pdf,*/*")
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" not in content_type and not pdf_url.lower().split("?")[0].endswith(".pdf"):
        raise PollTrackerError(f"URL did not appear to return a PDF: {pdf_url}")
    return response.content


def record_key(record: Dict[str, Any]) -> str:
    """Deduplication key for public poll records."""
    return "|".join(
        [
            str(record.get("source_url") or ""),
            str(record.get("pollster") or ""),
            str(record.get("poll_type") or ""),
            str(record.get("date") or ""),
        ]
    ).lower()


def normalize_figures(figures: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Ensure all tracked candidates exist and values are float/null."""
    normalized: Dict[str, Optional[float]] = {}
    for candidate in TRACKED_CANDIDATES:
        value = figures.get(candidate)
        if isinstance(value, (int, float)) and 0 <= float(value) <= 100:
            normalized[candidate] = round(float(value), 2)
        else:
            normalized[candidate] = None
    return normalized


def merge_poll_records(existing: List[Dict[str, Any]], new_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge public poll records, replacing duplicates only with higher confidence."""
    by_key: Dict[str, Dict[str, Any]] = {record_key(item): item for item in existing if record_key(item).strip("|")}
    for record in new_records:
        key = record_key(record)
        if not key.strip("|"):
            continue
        record["figures"] = normalize_figures(record.get("figures") or {})
        existing_record = by_key.get(key)
        if not existing_record:
            by_key[key] = record
            continue
        old_conf = float(existing_record.get("extraction_confidence") or 0)
        new_conf = float(record.get("extraction_confidence") or 0)
        if new_conf >= old_conf:
            by_key[key] = record
    return sorted(by_key.values(), key=lambda item: (item.get("date") or "9999-99-99", item.get("pollster") or ""))


def review_key(item: Dict[str, Any]) -> str:
    """Deduplication key for review queue."""
    return str(item.get("source_id") or item.get("source_url") or item.get("title") or "").lower()


def merge_review_items(existing: List[Dict[str, Any]], new_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge review queue items without duplicates."""
    by_key = {review_key(item): item for item in existing if review_key(item)}
    for item in new_items:
        by_key[review_key(item)] = item
    return sorted(by_key.values(), key=lambda item: item.get("created_at") or "")


def build_public_record(source: Dict[str, Any], parsed: Any, source_url: str) -> Dict[str, Any]:
    """Build normalized public poll record."""
    return {
        "date": parsed.poll_date,
        "fieldwork_dates": parsed.fieldwork_dates,
        "pollster": source.get("pollster"),
        "poll_type": parsed.poll_type,
        "question_text": parsed.question_text,
        "geography": "Kenya",
        "sample_size": parsed.sample_size,
        "figures": normalize_figures(parsed.figures),
        "source_title": source.get("title"),
        "source_url": source_url,
        "extraction_status": parsed.status,
        "extraction_confidence": parsed.confidence,
        "notes": parsed.reason,
    }


def build_review_item(source: Dict[str, Any], parsed: Any, source_url: str) -> Dict[str, Any]:
    """Build review queue item."""
    extracted = {k: v for k, v in parsed.figures.items() if isinstance(v, (int, float))}
    return {
        "source_id": source.get("source_id"),
        "pollster": source.get("pollster"),
        "title": source.get("title"),
        "source_url": source_url,
        "reason": parsed.reason,
        "extracted_candidates": extracted,
        "raw_snippet": parsed.raw_snippet[:1200],
        "created_at": utc_now_iso(),
    }


def process_source(source: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str, bool]:
    """
    Process one source.

    Returns: public_record, review_item, processing_status, processed_pdf_flag
    """
    source_url = source.get("pdf_url") or None
    processed_pdf = False

    try:
        if not source_url and source.get("page_url"):
            maybe_pdf = discover_pdf_from_page(source["page_url"])
            if maybe_pdf:
                source["pdf_url"] = maybe_pdf
                source_url = maybe_pdf

        if source_url:
            pdf_bytes = download_pdf(source_url)
            digest = sha256_bytes(pdf_bytes)
            source["sha256"] = digest
            (DOWNLOAD_DIR / f"{digest}.pdf").write_bytes(pdf_bytes)
            parsed = parse_pdf_bytes(pdf_bytes, fallback_date=source.get("published_date"))
            processed_pdf = True
        elif source.get("page_url"):
            page_text = extract_text_from_page(source["page_url"])
            digest = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
            source["sha256"] = digest
            parsed = parse_poll_text(page_text, fallback_date=source.get("published_date"))
            source_url = source["page_url"]
        else:
            return None, None, "rejected_no_url", False
    except Exception as exc:  # noqa: BLE001
        source["processing_error"] = str(exc)
        return None, build_review_item(
            source,
            type(
                "ParsedFailure",
                (),
                {
                    "figures": {candidate: None for candidate in TRACKED_CANDIDATES},
                    "reason": f"Processing failed: {exc}",
                    "raw_snippet": "",
                },
            )(),
            source.get("pdf_url") or source.get("page_url") or "",
        ), "needs_review_processing_error", processed_pdf

    if parsed.status == "AUTO_ACCEPTED" and is_official_url(source_url):
        return build_public_record(source, parsed, source_url), None, "processed", processed_pdf
    if parsed.status == "NEEDS_REVIEW":
        return None, build_review_item(source, parsed, source_url), "needs_review", processed_pdf
    return None, None, "rejected", processed_pdf


def run() -> None:
    """Main scheduled job entry point."""
    ensure_data_files()
    existing_polls = load_json_list(POLLS_FILE)
    existing_review = load_json_list(REVIEW_FILE)
    registry = load_json_list(SOURCES_FILE)

    discovered = discover_all_sources()
    registry, new_source_count = merge_sources(discovered, registry)

    existing_hashes = {item.get("sha256") for item in registry if item.get("sha256")}
    new_records: List[Dict[str, Any]] = []
    new_review: List[Dict[str, Any]] = []
    processed_pdf_count = 0
    rejected_count = 0

    for source in registry:
        # Reprocess discovered/needs_review items; skip already processed/rejected unless no hash exists.
        status = source.get("processing_status")
        if status == "processed" and source.get("sha256"):
            continue

        public_record, review_item, processing_status, processed_pdf = process_source(source)
        source["processing_status"] = processing_status
        source["last_checked_at"] = utc_now_iso()
        if processed_pdf:
            processed_pdf_count += 1
        if public_record:
            new_records.append(public_record)
        if review_item:
            new_review.append(review_item)
        if processing_status.startswith("rejected") or processing_status == "rejected":
            rejected_count += 1

    updated_polls = merge_poll_records(existing_polls, new_records)
    updated_review = merge_review_items(existing_review, new_review)

    polls_changed = write_json_list(POLLS_FILE, updated_polls)
    review_changed = write_json_list(REVIEW_FILE, updated_review)
    registry_changed = write_json_list(SOURCES_FILE, registry)

    print(f"Sources discovered: {len(discovered)}")
    print(f"New sources added: {new_source_count}")
    print(f"New PDFs processed: {processed_pdf_count}")
    print(f"Records auto-accepted: {len(new_records)}")
    print(f"Items sent to review: {len(new_review)}")
    print(f"Rejected items: {rejected_count}")
    print(f"polls_data.json updated: {'yes' if polls_changed else 'no'}")
    print(f"review_queue.json updated: {'yes' if review_changed else 'no'}")
    print(f"sources_registry.json updated: {'yes' if registry_changed else 'no'}")


if __name__ == "__main__":
    run()
