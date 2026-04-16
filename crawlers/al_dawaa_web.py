"""
crawlers/al_dawaa_web.py -- Al Dawaa 소매약국 가격 크롤러

Al Dawaa Pharmacies (https://www.al-dawaa.com)의 공개 검색 페이지에서
소매 가격을 수집한다.

⚠️ 상태: Cloudflare 방어 활성화 (403 반환, 2026-04-12 확인)
   → 브라우저 자동화 없이는 접근 불가
   → Cloudflare 탐지 시 서킷 브레이커가 자동 open
   → 추후 브라우저 자동화(Playwright) 도입 시 활성화 예정

구조: Nahdi 크롤러와 동일 패턴 (소매약국 HTML 파싱)
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua, detect as detect_antibot, AntiBotType
from normalizer import normalize_record
from supabase_state import AuditLog, MetricsCollector, SourceReputation

import httpx

logger = logging.getLogger("crawlers.al_dawaa_web")

ALDAWAA_BASE = "https://www.al-dawaa.com"
ALDAWAA_SEARCH_URL = f"{ALDAWAA_BASE}/en/catalogsearch/result/"


class AlDawaaClient:
    """Al Dawaa 온라인 약국 클라이언트."""

    def __init__(self, timeout: float = 20.0, delay: float = 2.0) -> None:
        self._delay = delay
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        self._last_request: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AlDawaaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def search(self, keyword: str, page: int = 1) -> str:
        """의약품 검색 HTML 반환."""
        self._throttle()
        params = {"q": keyword}
        if page > 1:
            params["p"] = str(page)
        resp = self._http.get(ALDAWAA_SEARCH_URL, params=params)

        # Anti-bot 감지
        ab_type = detect_antibot(
            resp.status_code,
            resp.text[:2000],
            dict(resp.headers),
        )
        if ab_type != AntiBotType.NONE:
            logger.warning(f"Al Dawaa anti-bot 감지: {ab_type.value}")
            raise httpx.HTTPStatusError(
                f"Anti-bot detected: {ab_type.value}",
                request=resp.request,
                response=resp,
            )

        resp.raise_for_status()
        return resp.text


def _parse_products_from_html(html: str) -> list[dict]:
    """검색 결과 HTML에서 제품 정보 추출 (Magento 기반)."""
    products = []

    # Magento JSON-LD
    import json
    json_ld_pattern = re.compile(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for match in json_ld_pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and data.get("@type") == "Product":
                name = data.get("name")
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price")
                if name:
                    products.append({
                        "name": name,
                        "price": float(price) if price else None,
                        "brand": (data.get("brand", {}) or {}).get("name"),
                        "sku": data.get("sku"),
                    })
        except (ValueError, KeyError):
            pass

    # Fallback: HTML 패턴
    if not products:
        price_re = re.compile(r'(?:SAR|sar)\s*(\d+(?:\.\d{1,2})?)')
        card_re = re.compile(
            r'<li[^>]*class="[^"]*product-item[^"]*"[^>]*>(.*?)</li>',
            re.DOTALL | re.IGNORECASE,
        )
        for card in card_re.finditer(html):
            card_html = card.group(1)
            name_m = re.search(r'class="product-item-link"[^>]*>(.*?)</a>', card_html, re.DOTALL)
            price_m = price_re.search(card_html)
            if name_m:
                products.append({
                    "name": re.sub(r"<[^>]+>", "", name_m.group(1)).strip(),
                    "price": float(price_m.group(1)) if price_m else None,
                })

    return products


def map_aldawaa_to_schema(product: dict, *, source_url: str) -> dict[str, Any]:
    """Al Dawaa 제품 데이터를 products 스키마로 변환."""
    name = product.get("name", "")
    sku = product.get("sku", "")

    if sku:
        pid = f"ALDAWAA_{sku}"
    else:
        import hashlib
        pid = f"ALDAWAA_{hashlib.md5(name.encode()).hexdigest()[:12]}"

    price = product.get("price")
    return {
        "product_id": pid,
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "confidence": 0.75,
        "trade_name": name,
        "scientific_name": None,
        "price_local": price,
        "price_sar": price,
        "manufacturer_or_marketing_company": product.get("brand"),
        "source_url": source_url,
        "source_tier": 3,
        "source_name": "al_dawaa_web",
        "matching_required": True,
        "raw_payload": product,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Al Dawaa 소매약국 가격 크롤러.

    ⚠️ Cloudflare 방어로 인해 현재 대부분 실패 예상.
       서킷 브레이커가 자동으로 차단하며, 재시도는 backoff_retry가 처리.

    cfg 옵션:
      - keywords: 검색 키워드 리스트
      - max_pages_per_keyword: 키워드당 최대 페이지 (기본 3)
      - delay: 요청 간격 초 (기본 2.0)
    """
    inserted = 0
    updated = 0
    skipped = 0

    keywords = cfg.get("keywords", [
        "paracetamol", "ibuprofen", "amoxicillin", "omeprazole",
    ])
    max_pages = cfg.get("max_pages_per_keyword", 3)
    delay = cfg.get("delay", 2.0)
    source_url = f"{ALDAWAA_BASE}/en/"

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "al_dawaa_web", {"keywords": keywords})
    metrics.inc("crawl_attempts")

    logger.info(f"Al Dawaa 크롤링 시작 ({len(keywords)} keywords)")

    seen_ids: set[str] = set()

    with AlDawaaClient(delay=delay) as client:
        for keyword in keywords:
            for page in range(1, max_pages + 1):
                t_page = time.time()
                try:
                    html = client.search(keyword, page=page)
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, 'status_code', None)
                    logger.warning(f"Al Dawaa 검색 실패 (keyword={keyword}): status={status}")
                    audit.log("error", "al_dawaa_web", {
                        "keyword": keyword, "status": status,
                    })
                    break  # 다음 키워드로

                products = _parse_products_from_html(html)
                if not products:
                    break

                audit.log("page_fetched", "al_dawaa_web", {
                    "keyword": keyword, "page": page, "products": len(products),
                })

                for product in products:
                    record = map_aldawaa_to_schema(product, source_url=source_url)
                    record = normalize_record(record)

                    pid = record["product_id"]
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    bonus = reputation.confidence_bonus("al_dawaa_web")
                    record["confidence"] = min(0.99, max(0.0, (record.get("confidence") or 0.75) + bonus))

                    if not dry_run:
                        try:
                            resp = sb.table("products").upsert(
                                record, on_conflict="product_id"
                            ).execute()
                            if getattr(resp, "data", None):
                                inserted += 1
                        except Exception as e:
                            skipped += 1
                    else:
                        inserted += 1

                    metrics.inc("records_processed")

                metrics.observe("page_fetch_sec", time.time() - t_page)

    metrics.inc("crawl_success" if inserted > 0 else "crawl_partial")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("al_dawaa_web", inserted > 0)

    audit.log("crawl_finished", "al_dawaa_web", {
        "inserted": inserted,
        "skipped": skipped,
    })

    logger.info(f"Al Dawaa 완료: inserted={inserted}, skipped={skipped}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
