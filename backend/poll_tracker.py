"""
Kenya Presidential Opinion Polls Tracker - backend pipeline.

This script discovers official polling sources, downloads official reports/pages,
extracts candidate percentage data, and writes three JSON outputs:

- data/polls_data.json
- data/review_queue.json
- data/sources_registry.json

It is designed to run locally, through cron, or through GitHub Actions.

Important design choice:
The backend is intentionally conservative. It only publishes records to
polls_data.json when the parser returns AUTO_ACCEPTED. Ambiguous extractions go
to review_queue.json instead.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from extractors import infotrak, tifa
from extractors.pdf_parser import parse_pdf_bytes, parse_poll_text


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"

POLLS_DATA_PATH = DATA_DIR / "polls_data.json"
REVIEW_QUEUE_PATH = DATA_DIR / "review_queue.json"
SOURCES_REGISTRY_PATH = DATA_DIR / "sources_registry.json"

REQUEST_TIMEOUT = 45

HEADERS = {
    "User-Agent": (
        "KenyaPollsTracker/1.0 "
        "(primary-source polling monitor; contact: repository owner)"
    )
}

TRACKED_CANDIDATES = [
    "William Ruto",
    "Kalonzo Musyoka",
    "Fred Matiang'i",
    "Rigathi Gachagua",
    "Edwin Sifuna",
]

# Official source URLs only. These are not fake/sample data.
# They ensure the pipeline always checks a known official source even if
# discovery pages change layout or hide links behind scripts.
SEED_SOURCES: List[Dict[str, Optional[str]]] = [
    {
        "pollster": "TIFA Research",
        "title": "TIFA National Poll 2026: Political Alignments and 2027 Election Prospects",
        "page_url": "https://www.tifaresearch.com/tifa-national-poll-2026-1st-release-on-political-alignments-and-2027-election-prospects/",
        "pdf_url": "https://www.tifaresearch.com/wp-content/uploads/2023/03/TIFA-Research_Political-Alignments-and-2027-Election-Prospects_14-May-2026.pdf",
        "published_date": "2026-05-14",
    }
]


def utc_now_iso() -> str:
    """Return the current UTC time in ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_data_files() -> None:
    """Ensure required data directory and JSON files exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for path in [POLLS_DATA_PATH, REVIEW_QUEUE_PATH, SOURCES_REGISTRY_PATH]:
        if not path.exists():
            write_json(path, [])


def read_json(path: Path, default: Any) -> Any:
    """Read JSON safely, returning default on missing or invalid files."""
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        print(f"Warning: {path} contained invalid JSON. Using default.", file=sys.stderr)
        return default


def write_json(path: Path, data: Any) -> None:
    """Write formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def stable_id(value: str) -> str:
    """Create a stable short ID from a URL or identifying value."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:20]


def sha256_bytes(content: bytes) -> str:
    """Return SHA-256 hash of bytes."""
    return hashlib.sha256(content).hexdigest()


def source_key(source: Dict[str, Any]) -> str:
    """Use PDF URL first, otherwise page URL, as the stable source key."""
    return source.get("pdf_url") or source.get("page_url") or source.get("title") or ""


def is_probably_pdf(url: str) -> bool:
    """Return True if URL path looks like a PDF."""
    return urlparse(url).path.lower().endswith(".pdf")


def clean_source(source: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalize source dictionary fields."""
    return {
        "pollster": source.get("pollster") or "Unknown",
        "title": source.get("title") or f"{source.get('pollster', 'Unknown')} poll release",
        "page_url": source.get("page_url"),
        "pdf_url": source.get("pdf_url"),
        "published_date": source.get("published_date"),
    }


def dedupe_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate sources using PDF URL or page URL.

    If the same source is discovered twice, merge useful metadata rather than
    blindly keeping the first copy. This is important because a discovered page
    may have ``published_date=None`` while the official seed source has a known
    date that helps the parser auto-approve a valid extraction.
    """
    by_key: Dict[str, Dict[str, Any]] = {}

    for raw in sources:
        source = clean_source(raw)
        key = source_key(source)

        if not key:
            continue

        existing = by_key.get(key)
        if existing is None:
            by_key[key] = source
            continue

        # Prefer non-empty values from either copy. Keep the more descriptive
        # title and preserve known published dates from seed sources.
        for field in ["pollster", "page_url", "pdf_url", "published_date"]:
            if not existing.get(field) and source.get(field):
                existing[field] = source.get(field)

        if source.get("title") and (not existing.get("title") or len(source["title"]) > len(existing["title"])):
            existing["title"] = source["title"]

    return list(by_key.values())


def download_url(url: str) -> bytes:
    """Download a URL and return response bytes."""
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content


def fetch_page_text(url: str) -> str:
    """Fetch a normal HTML page and extract visible text."""
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return soup.get_text("\n", strip=True)


def registry_index(registry: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index registry records by source_id."""
    return {
        item.get("source_id"): item
        for item in registry
        if item.get("source_id")
    }


def build_registry_record(
    source: Dict[str, Any],
    status: str,
    content_hash: Optional[str] = None,
    existing: Optional[Dict[str, Any]] = None,
    processing_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a source registry record."""
    key = source_key(source)
    source_id = stable_id(key)

    first_seen_at = existing.get("first_seen_at") if existing else utc_now_iso()

    record = {
        "source_id": source_id,
        "pollster": source.get("pollster"),
        "title": source.get("title"),
        "page_url": source.get("page_url"),
        "pdf_url": source.get("pdf_url"),
        "published_date": source.get("published_date"),
        "first_seen_at": first_seen_at,
        "last_checked_at": utc_now_iso(),
        "sha256": content_hash,
        "processing_status": status,
    }

    if processing_error:
        record["processing_error"] = processing_error

    return record


def build_public_record(source: Dict[str, Any], parse_result: Any, series_record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Convert an AUTO_ACCEPTED parse result into one public poll record."""
    record_data = series_record or {}
    source_url = source.get("pdf_url") or source.get("page_url")

    figures_source = record_data.get("figures") or parse_result.figures
    figures = {}
    for candidate in TRACKED_CANDIDATES:
        value = figures_source.get(candidate)
        figures[candidate] = value if value is not None else None

    return {
        "date": record_data.get("date") or parse_result.poll_date,
        "fieldwork_dates": record_data.get("fieldwork_dates") or parse_result.fieldwork_dates,
        "pollster": source.get("pollster"),
        "poll_type": record_data.get("poll_type") or parse_result.poll_type,
        "question_text": record_data.get("question_text") or parse_result.question_text,
        "geography": "Kenya",
        "sample_size": record_data.get("sample_size") or parse_result.sample_size,
        "figures": figures,
        "source_title": source.get("title"),
        "source_url": source_url,
        "extraction_status": parse_result.status,
        "extraction_confidence": record_data.get("confidence") or parse_result.confidence,
        "notes": record_data.get("notes") or parse_result.reason,
    }


def build_public_records(source: Dict[str, Any], parse_result: Any) -> List[Dict[str, Any]]:
    """
    Convert a parse result into one or more public poll records.

    Grouped chart parsers can attach ``series_records`` to emit a full trend
    from one PDF. Ordinary parsers emit a single record.
    """
    series_records = getattr(parse_result, "series_records", None) or []
    if series_records:
        return [build_public_record(source, parse_result, item) for item in series_records]
    return [build_public_record(source, parse_result)]

def build_review_item(
    source: Dict[str, Any],
    parse_result: Any,
    reason_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a NEEDS_REVIEW or processing failure into a review queue item."""
    source_url = source.get("pdf_url") or source.get("page_url")
    source_id = stable_id(source_url or source.get("title") or "")

    extracted_candidates = {}

    if hasattr(parse_result, "figures"):
        extracted_candidates = {
            name: value
            for name, value in parse_result.figures.items()
            if value is not None
        }

    return {
        "source_id": source_id,
        "pollster": source.get("pollster"),
        "title": source.get("title"),
        "source_url": source_url,
        "reason": reason_override or getattr(parse_result, "reason", "Needs review"),
        "extracted_candidates": extracted_candidates,
        "raw_snippet": getattr(parse_result, "raw_snippet", ""),
        "created_at": utc_now_iso(),
    }


def dedupe_poll_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate poll records.

    If a duplicate exists, keep the higher-confidence record.
    """
    by_key: Dict[str, Dict[str, Any]] = {}

    for record in records:
        key = "|".join(
            [
                str(record.get("source_url")),
                str(record.get("pollster")),
                str(record.get("poll_type")),
                str(record.get("date")),
            ]
        )

        current = by_key.get(key)

        if current is None:
            by_key[key] = record
            continue

        if float(record.get("extraction_confidence") or 0) > float(
            current.get("extraction_confidence") or 0
        ):
            by_key[key] = record

    return sorted(
        by_key.values(),
        key=lambda item: item.get("date") or "",
    )


def dedupe_review_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate review queue items by source_id and reason."""
    seen = set()
    output = []

    for item in items:
        key = f"{item.get('source_id')}|{item.get('reason')}"

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

    return output


def discover_all_sources() -> List[Dict[str, Any]]:
    """Run all configured discovery modules and include official seed sources."""
    # Put seed sources first so known official metadata is always present. The
    # deduper will still merge any matching discovered source details.
    sources: List[Dict[str, Any]] = list(SEED_SOURCES)

    try:
        sources.extend(tifa.discover_sources())
    except Exception as exc:  # noqa: BLE001
        print(f"TIFA discovery failed: {exc}", file=sys.stderr)

    try:
        sources.extend(infotrak.discover_sources())
    except Exception as exc:  # noqa: BLE001
        print(f"Infotrak discovery failed: {exc}", file=sys.stderr)

    return dedupe_sources(sources)


def process_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download/process one source and return processing outputs.

    Return shape:
    {
      "registry_status": str,
      "content_hash": str | None,
      "public_records": list[dict],
      "review_item": dict | None,
      "error": str | None
    }
    """
    fallback_date = source.get("published_date")
    source_url = source.get("pdf_url") or source.get("page_url")

    if not source_url:
        return {
            "registry_status": "rejected",
            "content_hash": None,
            "public_records": [],
            "review_item": None,
            "error": "Source has no URL.",
        }

    try:
        if source.get("pdf_url") or is_probably_pdf(source_url):
            content = download_url(source_url)
            content_hash = sha256_bytes(content)
            parse_result = parse_pdf_bytes(content, fallback_date=fallback_date)
        else:
            text = fetch_page_text(source_url)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            parse_result = parse_poll_text(text, fallback_date=fallback_date)

    except Exception as exc:  # noqa: BLE001
        review_item = build_review_item(
            source=source,
            parse_result=object(),
            reason_override=f"Processing failed: {exc}",
        )

        return {
            "registry_status": "needs_review_processing_error",
            "content_hash": None,
            "public_records": [],
            "review_item": review_item,
            "error": str(exc),
        }

    if parse_result.status == "AUTO_ACCEPTED":
        return {
            "registry_status": "processed",
            "content_hash": content_hash,
            "public_records": build_public_records(source, parse_result),
            "review_item": None,
            "error": None,
        }

    if parse_result.status == "NEEDS_REVIEW":
        return {
            "registry_status": "needs_review",
            "content_hash": content_hash,
            "public_records": [],
            "review_item": build_review_item(source, parse_result),
            "error": None,
        }

    return {
        "registry_status": "rejected",
        "content_hash": content_hash,
        "public_records": [],
        "review_item": None,
        "error": parse_result.reason,
    }


def main() -> None:
    """Run full polling update pipeline."""
    ensure_data_files()

    existing_polls = read_json(POLLS_DATA_PATH, [])
    existing_review = read_json(REVIEW_QUEUE_PATH, [])
    existing_registry = read_json(SOURCES_REGISTRY_PATH, [])

    registry_by_id = registry_index(existing_registry)

    discovered_sources = discover_all_sources()

    new_public_records = []
    new_review_items = []
    updated_registry = registry_by_id.copy()

    summary = {
        "sources_discovered": len(discovered_sources),
        "new_pdfs_or_pages_processed": 0,
        "records_auto_accepted": 0,
        "items_sent_to_review": 0,
        "rejected_items": 0,
        "polls_data_updated": "no",
    }

    for source in discovered_sources:
        key = source_key(source)
        source_id = stable_id(key)
        existing_registry_record = updated_registry.get(source_id)

        result = process_source(source)

        summary["new_pdfs_or_pages_processed"] += 1

        updated_registry[source_id] = build_registry_record(
            source=source,
            status=result["registry_status"],
            content_hash=result["content_hash"],
            existing=existing_registry_record,
            processing_error=result["error"]
            if result["registry_status"] == "needs_review_processing_error"
            else None,
        )

        if result["public_records"]:
            new_public_records.extend(result["public_records"])
            summary["records_auto_accepted"] += len(result["public_records"])

        elif result["review_item"]:
            new_review_items.append(result["review_item"])
            summary["items_sent_to_review"] += 1

        else:
            summary["rejected_items"] += 1

    merged_polls = dedupe_poll_records(existing_polls + new_public_records)
    merged_review = dedupe_review_items(existing_review + new_review_items)

    registry_list = sorted(
        updated_registry.values(),
        key=lambda item: (
            item.get("pollster") or "",
            item.get("title") or "",
            item.get("page_url") or "",
            item.get("pdf_url") or "",
        ),
    )

    if merged_polls != existing_polls:
        summary["polls_data_updated"] = "yes"

    write_json(POLLS_DATA_PATH, merged_polls)
    write_json(REVIEW_QUEUE_PATH, merged_review)
    write_json(SOURCES_REGISTRY_PATH, registry_list)

    print(f"Sources discovered: {summary['sources_discovered']}")
    print(f"New PDFs/pages processed: {summary['new_pdfs_or_pages_processed']}")
    print(f"Records auto-accepted: {summary['records_auto_accepted']}")
    print(f"Items sent to review: {summary['items_sent_to_review']}")
    print(f"Rejected items: {summary['rejected_items']}")
    print(f"polls_data.json updated: {summary['polls_data_updated']}")


if __name__ == "__main__":
    main()
