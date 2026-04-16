"""
crawlers/whites_web.py -- Whites Pharmacy 소매 가격 크롤러

Whites (https://www.whites.sa)의 공개 페이지에서 소매 가격을 수집한다.

사이트 특성:
  - /en-sa 경로 (영문)
  - 검색 기능 존재
  - OTC·건강보조식품 비중 높음 (Omega-3 등)
  - 접근 가능 확인 (2026-04-12, status 200)

수집 대상:
  - trade_name, price_sar
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua
from normalizer import normalize_record
from supabase_state import AuditLog, MetricsCollector, SourceReputation

import httpx

logger = logging.getLogger("crawlers.whites_web")

WHITES_BASE = "https://www.whites.sa"
WHITES_SEARCH_URL = f"{WHITES_BASE}/en-sa/search"


class WhitesClient:
    """Whites Pharmacy 클라이언트."""

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

    def __enter__(self) -> "WhitesClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def search(self, keyword: str, page: int = 0) -> str:
        """제품 검색 HTML 반환.

        Whites는 search_text 파라미터를 사용한다 (q가 아님).
        """
        self._throttle()
        params = {"search_text": keyword}
        if page > 0:
            params["page"] = str(page)
        resp = self._http.get(WHITES_SEARCH_URL, params=params)
        resp.raise_for_status()
        return resp.text


def _extract_objects_with_price(chunk: str) -> list[dict]:
    """RSC 청크에서 retail_price를 포함하는 JSON 객체를 브레이스 매칭으로 추출.

    기존의 6개 별도 re.findall + 인덱스 정렬 방식을 대체한다.
    인덱스 불일치로 인한 필드 혼용(name↔sku↔price 미스매치)을 방지한다.
    """
    import json as _json

    objects = []
    depth = 0
    start = -1
    for i, c in enumerate(chunk):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = chunk[start:i + 1]
                if '"retail_price"' in fragment and '"name"' in fragment:
                    try:
                        obj = _json.loads(fragment)
                        if isinstance(obj, dict) and obj.get("name") and obj.get("sku"):
                            objects.append(obj)
                    except _json.JSONDecodeError:
                        pass
                start = -1
    return objects


def _parse_products_from_html(html: str) -> list[dict]:
    """검색 결과 HTML에서 제품 정보 추출.

    Whites는 Next.js (Akinon 커머스 백엔드)를 사용한다.
    제품 데이터는 self.__next_f.push() 스트리밍 chunks에 포함되어 있다.

    Akinon 제품 구조:
      - pk: 제품 PK (정수)
      - name: 제품명
      - sku: SKU 코드
      - price: "12.95" (문자열)
      - retail_price: "12.95" (문자열)
      - absolute_url: "/product-slug/"
      - currency_type: "sar"
      - attributes.Brand: 브랜드명
      - attributes.Forms: 제형
    """
    import json

    products = []

    # ── 1차: __next_f 스트리밍 데이터에서 Akinon 제품 추출 ──
    next_f_chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL
    )

    filter_labels = {"Categories", "Brands", "Forms", "In Stock", "Quantity",
                     "Size", "Price", "Sort"}

    for chunk_raw in next_f_chunks:
        # 큰 청크만 확인 (제품 데이터는 대용량)
        if len(chunk_raw) < 500:
            continue
        if "retail_price" not in chunk_raw:
            continue

        try:
            chunk = chunk_raw.encode().decode("unicode_escape")
        except (UnicodeDecodeError, ValueError):
            continue

        # 브레이스 매칭으로 완전한 JSON 객체 단위 추출 → 인덱스 불일치 방지
        for obj in _extract_objects_with_price(chunk):
            name = obj.get("name", "")
            if name in filter_labels or len(name) <= 3:
                continue
            product: dict[str, Any] = {"name": name}
            sku = obj.get("sku")
            if sku:
                product["sku"] = str(sku)
            retail_price = obj.get("retail_price") or obj.get("price")
            if retail_price is not None:
                try:
                    product["price"] = float(retail_price)
                except (TypeError, ValueError):
                    pass
            abs_url = obj.get("absolute_url")
            if abs_url:
                product["url"] = f"{WHITES_BASE}{abs_url}"
            attrs = obj.get("attributes") or {}
            brand = attrs.get("Brand") or obj.get("Brand")
            if brand:
                product["brand"] = brand
            form = attrs.get("Forms") or obj.get("Forms")
            if form:
                product["form"] = form
            products.append(product)

    if products:
        return products

    # ── 2차: JSON-LD fallback ──
    json_ld_pattern = re.compile(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for match in json_ld_pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product":
                    name = item.get("name")
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price")
                    if name:
                        products.append({
                            "name": name,
                            "price": float(price) if price else None,
                            "brand": (item.get("brand", {}) or {}).get("name")
                                     if isinstance(item.get("brand"), dict)
                                     else item.get("brand"),
                            "sku": item.get("sku"),
                            "url": item.get("url"),
                        })
        except (ValueError, KeyError):
            pass

    return products


def map_whites_to_schema(product: dict, *, source_url: str) -> dict[str, Any]:
    """Whites 제품 데이터를 products 스키마로 변환."""
    name = product.get("name", "")
    sku = product.get("sku", "")

    if sku:
        pid = f"WHITES_{sku}"
    else:
        import hashlib
        pid = f"WHITES_{hashlib.sha256(name.encode()).hexdigest()[:16]}"

    price = product.get("price")
    return {
        "product_id": pid,
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "confidence": 0.70,
        "trade_name": name,
        "scientific_name": None,
        "price_local": price,
        "price_sar": price,
        "manufacturer_or_marketing_company": product.get("brand"),
        "source_url": product.get("url", source_url),
        "source_tier": 3,
        "source_name": "whites_web",
        "matching_required": True,
        "raw_payload": product,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Whites Pharmacy 소매 가격 크롤러.

    cfg 옵션:
      - keywords: 검색 키워드 리스트
      - max_pages_per_keyword: 키워드당 최대 페이지 (기본 3)
      - delay: 요청 간격 초 (기본 2.0)
    """
    inserted = 0
    updated = 0  # NOTE: Supabase upsert does not distinguish insert vs update; always 0.
    skipped = 0

    keywords = cfg.get("keywords", [
        "paracetamol", "ibuprofen", "omega-3", "vitamin d",
        "amoxicillin", "omeprazole",
    ])
    max_pages = cfg.get("max_pages_per_keyword", 3)
    delay = cfg.get("delay", 2.0)
    source_url = f"{WHITES_BASE}/en-sa"

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "whites_web", {"keywords": keywords})
    metrics.inc("crawl_attempts")

    seen_ids: set[str] = set()

    with WhitesClient(delay=delay) as client:
        for keyword in keywords:
            for page in range(max_pages):
                t_page = time.time()
                try:
                    html = client.search(keyword, page=page)
                except httpx.HTTPStatusError as e:
                    logger.warning(f"Whites 검색 실패 (keyword={keyword}): {e}")
                    audit.log("error", "whites_web", {"keyword": keyword})
                    break

                products = _parse_products_from_html(html)
                if not products:
                    break

                audit.log("page_fetched", "whites_web", {
                    "keyword": keyword, "page": page, "products": len(products),
                })

                for product in products:
                    record = map_whites_to_schema(product, source_url=source_url)
                    record = normalize_record(record)

                    pid = record["product_id"]
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    bonus = reputation.confidence_bonus("whites_web")
                    record["confidence"] = min(0.99, max(0.0, (record.get("confidence") or 0.70) + bonus))

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

    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("whites_web", True)

    audit.log("crawl_finished", "whites_web", {
        "inserted": inserted, "skipped": skipped,
    })

    logger.info(f"Whites 완료: inserted={inserted}, skipped={skipped}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
