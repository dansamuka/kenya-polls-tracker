"""
Infotrak Research source discovery.

This extractor monitors the official Infotrak poll archive and returns only
plausible official article/PDF sources. It filters pagination, category pages,
country pages, social-share URLs, mailto links, and other navigation noise.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup


INFOTRAK_POLLS_URL = "https://www.infotrakresearch.com/all-infotrak-polls/"

REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "KenyaPollsTracker/1.0 "
        "(primary-source polling monitor; contact: repository owner)"
    )
}

RELEVANT_KEYWORDS = [
    "presidential",
    "president",
    "2027",
    "election",
    "elections",
    "candidate",
    "candidates",
    "popularity",
    "political",
    "alignment",
    "alignments",
    "poll",
    "survey",
    "opinion",
    "race",
    "voice of the people",
    "vop",
]

BAD_URL_PATTERNS = [
    "addtoany.com",
    "mailto:",
    "javascript:",
    "tel:",
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "whatsapp",
    "mastodon",
    "?share=",
    "&share=",
    "/add_to/",
    "#elementor-action",
    "elementor-action",
    "/category/",
    "/tag/",
    "/author/",
    "/page/",
    "/wp-json/",
    "/wp-admin/",
    "/wp-login",
    "/feed/",
    "/ghana-polls/",
    "/nigeria_polls/",
]

GENERIC_TITLES = {
    "",
    "2",
    "3",
    "4",
    "5",
    "kenya",
    "ghana",
    "nigeria",
    "opinion polls",
    "political polls",
    "social polls",
    "all infotrak polls",
    "read more",
    "learn more",
    "home",
}


@dataclass
class DiscoveredSource:
    pollster: str
    title: str
    page_url: str
    pdf_url: Optional[str]
    published_date: Optional[str]


def _stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:20]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _is_bad_url(url: str) -> bool:
    if not url:
        return True

    decoded = unquote(url).lower()

    return any(pattern in decoded for pattern in BAD_URL_PATTERNS)


def _is_infotrak_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    return parsed.netloc in {
        "infotrakresearch.com",
        "www.infotrakresearch.com",
    }


def _is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _looks_relevant(title: str, url: str) -> bool:
    combined = f"{title} {url}".lower()
    return any(keyword in combined for keyword in RELEVANT_KEYWORDS)


def _normalise_url(href: str) -> Optional[str]:
    if not href:
        return None

    href = href.strip()

    if _is_bad_url(href):
        return None

    absolute = urljoin(INFOTRAK_POLLS_URL, href)

    if _is_bad_url(absolute):
        return None

    if not _is_infotrak_url(absolute):
        return None

    return absolute.split("#")[0]


def _extract_pdf_links_from_page(page_url: str) -> List[str]:
    try:
        response = requests.get(
            page_url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    pdf_links: List[str] = []

    for anchor in soup.find_all("a", href=True):
        url = _normalise_url(anchor.get("href", ""))

        if not url:
            continue

        if _is_pdf_url(url) and url not in pdf_links:
            pdf_links.append(url)

    return pdf_links


def discover_sources() -> List[Dict]:
    """
    Discover relevant Infotrak source pages and PDFs.

    Returns dictionaries so the main pipeline can merge them into the source
    registry without depending on this module's dataclass.
    """
    response = requests.get(
        INFOTRAK_POLLS_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    discovered: Dict[str, DiscoveredSource] = {}

    for anchor in soup.find_all("a", href=True):
        url = _normalise_url(anchor.get("href", ""))

        if not url:
            continue

        title = _clean_text(anchor.get_text(" ", strip=True))

        if title.lower() in GENERIC_TITLES:
            continue

        if not title:
            title = "Infotrak Research poll release"

        # Ignore generic navigation links that are not PDFs and not relevant.
        if not _is_pdf_url(url) and not _looks_relevant(title, url):
            continue

        pdf_url = url if _is_pdf_url(url) else None
        page_url = INFOTRAK_POLLS_URL if pdf_url else url

        # If this is a relevant article page, inspect it for official PDFs.
        pdf_links = [pdf_url] if pdf_url else _extract_pdf_links_from_page(page_url)

        if pdf_links:
            for pdf in pdf_links:
                key = pdf
                discovered[key] = DiscoveredSource(
                    pollster="Infotrak Research",
                    title=title,
                    page_url=page_url,
                    pdf_url=pdf,
                    published_date=None,
                )
        else:
            key = page_url
            discovered[key] = DiscoveredSource(
                pollster="Infotrak Research",
                title=title,
                page_url=page_url,
                pdf_url=None,
                published_date=None,
            )

    return [asdict(source) for source in discovered.values()]
