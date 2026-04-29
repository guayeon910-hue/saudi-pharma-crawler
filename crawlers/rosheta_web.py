"""
crawlers/rosheta_web.py -- Rosheta Saudi retail/reference price crawler.

Rosheta exposes medicine pages under /en/{id}/{slug}. Search redirects to
/en/tag/{keyword}, where product/article links are rendered server-side.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("crawlers.rosheta_web")

ROSHETA_BASE = "https://www.rosheta.com"
ROSHETA_SEARCH_URL = f"{ROSHETA_BASE}/en/search"

_PRODUCT_LINK_RE = re.compile(r"/en/\d+/[^/?#]+$", re.IGNORECASE)
_PRICE_RE = re.compile(r"(?P<price>\d+(?:\.\d+)?)\s*SAR", re.IGNORECASE)
_DOSAGE_RE = re.compile(r"\bDosage\s+(?P<value>[^\n\r]+)", re.IGNORECASE)
_TYPE_RE = re.compile(r"\bType\s+(?P<value>[A-Za-z][A-Za-z /.-]+)", re.IGNORECASE)


class RoshetaClient:
    def __init__(self, timeout: float = 20.0, delay: float = 0.8) -> None:
        self._delay = delay
        self._last_request = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RoshetaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def search(self, keyword: str) -> str:
        self._throttle()
        resp = self._http.get(ROSHETA_SEARCH_URL, params={"search": keyword})
        resp.raise_for_status()
        return resp.text

    def fetch(self, url: str) -> str:
        self._throttle()
        resp = self._http.get(urljoin(ROSHETA_BASE, url))
        resp.raise_for_status()
        return resp.text

    def search_products(self, keyword: str, max_links: int = 8) -> list[dict]:
        html = self.search(keyword)
        links = _parse_product_links(html)
        products: list[dict] = []
        for url in links[:max_links]:
            try:
                product = _parse_product_page(self.fetch(url), url)
                if product:
                    products.append(product)
            except Exception as exc:
                logger.debug("Rosheta product fetch failed (%s): %s", url, exc)
        return products


def _parse_product_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not _PRODUCT_LINK_RE.search(href):
            continue
        url = urljoin(ROSHETA_BASE, href)
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def _parse_product_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("h1")
    name = title_el.get_text(" ", strip=True) if title_el else ""
    if not name:
        return None
    if "uae" in url.lower() or "uae" in name.lower():
        return None

    text = soup.get_text("\n", strip=True)
    price = None
    m_price = _PRICE_RE.search(text)
    if m_price:
        try:
            price = float(m_price.group("price"))
        except ValueError:
            price = None
    if price is None:
        return None

    dosage = None
    m_dosage = _DOSAGE_RE.search(text)
    if m_dosage:
        dosage = m_dosage.group("value").split("\n")[0].strip()

    form = None
    m_type = _TYPE_RE.search(text)
    if m_type:
        form = m_type.group("value").split("\n")[0].strip()

    return {
        "name": name,
        "trade_name": name,
        "price": price,
        "price_sar": price,
        "strength": dosage,
        "form": form,
        "dosage_form": form,
        "url": url,
        "source_url": url,
        "source_name": "rosheta_web",
    }


def map_rosheta_to_schema(product: dict, *, source_url: str) -> dict[str, Any]:
    name = product.get("name") or product.get("trade_name") or ""
    url = product.get("url") or source_url
    product_id = "ROSHETA_" + re.sub(r"[^0-9A-Za-z]+", "_", url).strip("_")[-80:]
    price = product.get("price_sar") if product.get("price_sar") is not None else product.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    return {
        "product_id": product_id,
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "confidence": 0.62,
        "trade_name": name,
        "scientific_name": product.get("ingredient"),
        "strength": product.get("strength"),
        "dosage_form": product.get("dosage_form") or product.get("form"),
        "price_local": price,
        "price_sar": price,
        "source_url": url,
        "source_tier": 4,
        "source_name": "rosheta_web",
        "matching_required": True,
        "raw_payload": product,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    keywords = cfg.get("keywords", ["mosapride", "rosuvastatin", "hydroxyurea"])
    inserted = 0
    skipped = 0
    with RoshetaClient(delay=float(cfg.get("delay", 0.8))) as client:
        for keyword in keywords:
            for product in client.search_products(keyword, max_links=int(cfg.get("max_links", 8))):
                record = map_rosheta_to_schema(product, source_url=product.get("url", ROSHETA_BASE))
                if not dry_run:
                    try:
                        resp = sb.table("products").upsert(record, on_conflict="product_id").execute()
                        if getattr(resp, "data", None):
                            inserted += 1
                    except Exception:
                        skipped += 1
                else:
                    inserted += 1
    return {"rows_inserted": inserted, "rows_updated": 0, "rows_skipped": skipped}
