"""Infotrak Research source discovery."""
from __future__ import annotations

from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

INFOTRAK_POLLS_URL = "https://www.infotrakresearch.com/all-infotrak-polls/"
USER_AGENT = "KenyaPollsTracker/1.0 (+https://github.com/your-org/kenya-polls-tracker)"


def _extract_date_from_element(element) -> Optional[str]:
    """Best-effort extraction of date-like strings from common WordPress markup."""
    selectors = ["time", ".entry-date", ".posted-on", ".date", ".post-date"]
    for selector in selectors:
        found = element.select_one(selector)
        if not found:
            continue
        if found.get("datetime"):
            return found.get("datetime")[:10]
        text = found.get_text(" ", strip=True)
        if text:
            return text
    return None


def discover_sources(timeout: int = 25) -> List[Dict[str, Optional[str]]]:
    """Discover Infotrak poll pages and PDFs from the official polls page."""
    response = requests.get(
        INFOTRAK_POLLS_URL,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    sources: List[Dict[str, Optional[str]]] = []
    seen = set()

    for link in soup.select("a[href]"):
        href = link.get("href", "").strip()
        if not href:
            continue
        absolute_url = urljoin(INFOTRAK_POLLS_URL, href)
        text = link.get_text(" ", strip=True)
        parent = link.find_parent(["article", "li", "div"]) or link.parent
        title = text or (parent.get_text(" ", strip=True)[:160] if parent else absolute_url)
        lower_blob = f"{title} {absolute_url}".lower()

        if "poll" not in lower_blob and "survey" not in lower_blob and not absolute_url.lower().endswith(".pdf"):
            continue

        pdf_url = absolute_url if absolute_url.lower().split("?")[0].endswith(".pdf") else None
        page_url = INFOTRAK_POLLS_URL if pdf_url else absolute_url
        key = pdf_url or page_url
        if key in seen:
            continue
        seen.add(key)

        sources.append(
            {
                "pollster": "Infotrak Research",
                "title": title[:240] or "Infotrak Research poll release",
                "page_url": page_url,
                "pdf_url": pdf_url,
                "published_date": _extract_date_from_element(parent) if parent else None,
            }
        )

    return sources
