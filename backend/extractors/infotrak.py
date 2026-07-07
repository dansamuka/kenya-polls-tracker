"""
Infotrak Research source discovery.

This extractor monitors official Infotrak poll archive pages and returns a
broader set of plausible political / leader-popularity / VOP poll sources.

Design intent:
- Be more inclusive at discovery time so Infotrak reports are not missed merely
  because their titles use wording like "Voice of the People", "political pulse",
  "leader performance", "succession", or "vote for president" rather than the
  exact phrase "presidential candidate popularity".
- Still reject obvious noise such as share links, mailto links, category-only
  navigation, country navigation, admin/feed URLs, and social links.
- Let the PDF parser decide whether a discovered official source actually
  contains tracked candidate values worth publishing.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


ARCHIVE_URLS = [
    "https://www.infotrakresearch.com/all-infotrak-polls/",
    "https://www.infotrakresearch.com/category/political-polls/",
    "https://www.infotrakresearch.com/category/opinion-polls/",
    "https://www.infotrakresearch.com/infotrak-polls/",
]

REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "KenyaPollsTracker/1.1 "
        "(primary-source polling monitor; contact: repository owner)"
    )
}

# If any tracked name appears, the source is worth processing.
TRACKED_NAME_KEYWORDS = [
    "ruto",
    "kalonzo",
    "matiang",
    "matiang'i",
    "matiang’i",
    "gachagua",
    "sifuna",
]

# Broader Infotrak discovery terms. Infotrak titles often use wide labels like
# VOP, political pulse, perceptions, leadership, succession, or state of the
# nation. These sources are allowed into processing, but are only published if
# the parser finds tracked candidate percentages.
POLITICAL_DISCOVERY_KEYWORDS = [
    "presidential",
    "president",
    "2027",
    "election",
    "elections",
    "candidate",
    "candidates",
    "aspirant",
    "aspirants",
    "successor",
    "succession",
    "vote for president",
    "if presidential elections were held",
    "popularity",
    "popular",
    "favourability",
    "favorability",
    "approval",
    "performance",
    "leader rating",
    "leader ratings",
    "leaders rating",
    "leaders ratings",
    "elected leaders",
    "political",
    "political pulse",
    "political temperature",
    "coalition",
    "coalitions",
    "party",
    "opposition",
    "governance",
    "state of the nation",
    "nationwide perception",
    "perception study",
    "voice of the people",
    "vop",
    "mulembe nation",
]

# Terms that normally indicate the source is unrelated to political/candidate
# popularity even if it is an opinion poll.
NON_POLITICAL_EXCLUSION_KEYWORDS = [
    "nyota programme",
    "cbc",
    "solar cooling",
    "dairy farmers",
    "business",
    "consumer",
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
    "/tag/",
    "/author/",
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
    "polls",
    "polls by country",
    "opinion polls",
    "political polls",
    "social polls",
    "all infotrak polls",
    "read more",
    "read more...",
    "learn more",
    "home",
    "latest news",
    "news",
}


@dataclass
class DiscoveredSource:
    pollster: str
    title: str
    page_url: str
    pdf_url: Optional[str]
    published_date: Optional[str]


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

    return parsed.netloc in {"infotrakresearch.com", "www.infotrakresearch.com"}


def _is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _normalise_url(href: str, base_url: str) -> Optional[str]:
    if not href:
        return None

    href = href.strip()
    if _is_bad_url(href):
        return None

    absolute = urljoin(base_url, href).split("#")[0]
    if _is_bad_url(absolute):
        return None

    if not _is_infotrak_url(absolute):
        return None

    return absolute


def _has_tracked_name(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in TRACKED_NAME_KEYWORDS)


def _looks_politically_relevant(title: str, url: str) -> bool:
    combined = f"{title} {url}".lower()

    if _has_tracked_name(combined):
        return True

    if any(term in combined for term in NON_POLITICAL_EXCLUSION_KEYWORDS):
        # Still allow if the URL/title explicitly has a tracked political name.
        return False

    return any(keyword in combined for keyword in POLITICAL_DISCOVERY_KEYWORDS)


def _extract_page_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in ["h1", "h2.entry-title", ".entry-title", "title"]:
        node = soup.select_one(selector)
        if node:
            title = _clean_text(node.get_text(" ", strip=True))
            if title and title.lower() not in GENERIC_TITLES:
                return title
    return fallback


def _extract_pdf_links_from_page(page_url: str) -> List[str]:
    try:
        response = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    pdf_links: List[str] = []

    for anchor in soup.find_all("a", href=True):
        url = _normalise_url(anchor.get("href", ""), page_url)
        if not url or not _is_pdf_url(url):
            continue

        # Once an article page is relevant, keep its official PDFs even if the
        # anchor text says only "Download report" or "Get full report".
        if url not in pdf_links:
            pdf_links.append(url)

    return pdf_links


def _discover_from_archive(archive_url: str) -> Dict[str, DiscoveredSource]:
    response = requests.get(archive_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    discovered: Dict[str, DiscoveredSource] = {}

    for anchor in soup.find_all("a", href=True):
        url = _normalise_url(anchor.get("href", ""), archive_url)
        if not url:
            continue

        title = _clean_text(anchor.get_text(" ", strip=True))
        if title.lower() in GENERIC_TITLES:
            # Do not accept generic archive navigation as a source record.
            continue

        if not title:
            title = "Infotrak Research poll release"

        # Keep relevant PDFs and relevant article pages. Do not require exact
        # presidential wording; broad political/VOP sources can still be useful
        # if the parser later finds tracked candidate percentages.
        if not _looks_politically_relevant(title, url):
            continue

        if _is_pdf_url(url):
            discovered[url] = DiscoveredSource(
                pollster="Infotrak Research",
                title=title,
                page_url=archive_url,
                pdf_url=url,
                published_date=None,
            )
            continue

        page_url = url
        try:
            page_response = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            page_response.raise_for_status()
            page_soup = BeautifulSoup(page_response.text, "html.parser")
            title = _extract_page_title(page_soup, title)
        except requests.RequestException:
            page_soup = None

        pdf_links = _extract_pdf_links_from_page(page_url)
        if pdf_links:
            for pdf_url in pdf_links:
                discovered[pdf_url] = DiscoveredSource(
                    pollster="Infotrak Research",
                    title=title,
                    page_url=page_url,
                    pdf_url=pdf_url,
                    published_date=None,
                )
        else:
            discovered[page_url] = DiscoveredSource(
                pollster="Infotrak Research",
                title=title,
                page_url=page_url,
                pdf_url=None,
                published_date=None,
            )

    return discovered


def discover_sources() -> List[Dict]:
    """
    Discover relevant Infotrak source pages and PDFs.

    This is intentionally more inclusive than the original version. It may send
    more sources to the parser, but only records with tracked candidate values
    and valid poll metadata can reach the public dashboard.
    """
    all_discovered: Dict[str, DiscoveredSource] = {}

    for archive_url in ARCHIVE_URLS:
        try:
            all_discovered.update(_discover_from_archive(archive_url))
        except requests.RequestException:
            continue

    return [asdict(source) for source in all_discovered.values()]
