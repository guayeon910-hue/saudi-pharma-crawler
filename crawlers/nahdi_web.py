"""
crawlers/nahdi_web.py -- Nahdi 소매약국 가격 크롤러

Al Nahdi Medical Company (https://www.nahdionline.com)의
공개 의약품 검색 페이지에서 소매 가격을 수집한다.

사이트 특성:
  - /en-sa/search?q={keyword} 에서 검색 가능 (status 200 확인)
  - 카테고리 경로: /c/medicine/, /c/personal-care/
  - 가격이 HTML에 포함됨
  - robots.txt 확인 완료 (2026-04-09)
  - matching_required: true — SFDA regulatory_id 보강 필요

수집 대상:
  - trade_name, price_sar, manufacturer_or_marketing_company
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

logger = logging.getLogger("crawlers.nahdi_web")

NAHDI_BASE = "https://www.nahdionline.com"
NAHDI_SEARCH_URL = f"{NAHDI_BASE}/en-sa/search"

# Algolia 검색 API 설정 (Nahdi 프론트엔드 JS에서 추출한 공개 키)
ALGOLIA_APP_ID = "H9X4IH7M99"
ALGOLIA_API_KEY = "2bbce1340a1cab2ccebe0307b1310881"
ALGOLIA_INDEX = "prod_en_products"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"


class NahdiClient:
    """Nahdi 온라인 약국 클라이언트.

    Nahdi는 Next.js + Algolia를 사용한다.
    HTML 검색 결과는 SSR에서 빈 쿼리로 렌더링되므로,
    Algolia Search API를 직접 호출하여 정확한 결과를 얻는다.
    """

    def __init__(self, timeout: float = 20.0, delay: float = 2.0) -> None:
        self._delay = delay
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "X-Algolia-Application-Id": ALGOLIA_APP_ID,
                "X-Algolia-API-Key": ALGOLIA_API_KEY,
            },
            follow_redirects=True,
        )
        self._last_request: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NahdiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def search(self, keyword: str, page: int = 0, hits_per_page: int = 20) -> list[dict]:
        """Algolia API로 의약품 검색. dict 리스트 반환.

        Returns:
            [{"name": ..., "sku": ..., "price": float, "brand": ..., ...}, ...]
        """
        self._throttle()
        body = {
            "query": keyword,
            "hitsPerPage": hits_per_page,
            "page": page,
        }
        resp = self._http.post(ALGOLIA_URL, json=body)
        resp.raise_for_status()
        data = resp.json()
        products = []
        for hit in data.get("hits", []):
            product = _extract_from_algolia_hit(hit)
            if product:
                products.append(product)
        return products

    def search_html(self, keyword: str, page: int = 0) -> str:
        """HTML 검색 (fallback용). SSR 결과는 프로모션 아이템만 포함."""
        self._throttle()
        html_http = httpx.Client(
            timeout=20,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        try:
            params = {"q": keyword}
            if page > 0:
                params["page"] = str(page)
            resp = html_http.get(NAHDI_SEARCH_URL, params=params)
            resp.raise_for_status()
            return resp.text
        finally:
            html_http.close()

    def get_category(self, path: str, page: int = 0) -> str:
        """카테고리 페이지 HTML 반환."""
        self._throttle()
        html_http = httpx.Client(
            timeout=20,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        try:
            url = f"{NAHDI_BASE}/en-sa{path}"
            params = {}
            if page > 0:
                params["page"] = str(page)
            resp = html_http.get(url, params=params)
            resp.raise_for_status()
            return resp.text
        finally:
            html_http.close()


def _parse_products_from_html(html: str) -> list[dict]:
    """검색/카테고리 HTML에서 제품 정보 추출.

    Nahdi 사이트는 Next.js + Algolia InstantSearch를 사용한다.
    제품 데이터는 window[Symbol.for("InstantSearchInitialResults")] JSON blob에 있다.

    Algolia hit 구조:
      - name: 제품명
      - sku: SKU 코드
      - price: {SAR: {default: float, default_formated: "xx.xx SAR"}}
      - manufacturer: 제조사/마케팅사
      - product_form: 제형 (Tablets, Gummies 등)
      - url: 제품 URL
    """
    import json

    products = []

    # ── 1차: Algolia InstantSearchInitialResults (주요 데이터 소스) ──
    idx = html.find("InstantSearchInitialResults")
    if idx >= 0:
        # JSON은 '= {' 에서 시작, '</script>' 직전에 끝남
        assign_pos = html.find("=", idx)
        if assign_pos >= 0:
            val_start = html.find("{", assign_pos)
            if val_start >= 0:
                script_end = html.find("</script>", val_start)
                if script_end >= 0:
                    json_str = html[val_start:script_end].rstrip().rstrip(";")
                    try:
                        data = json.loads(json_str)
                        # 인덱스 키: "prod_en_products" (영문) 또는 "prod_ar_products" (아랍어)
                        for index_key in data:
                            results_list = data[index_key].get("results", [])
                            for result_block in results_list:
                                if not isinstance(result_block, dict):
                                    continue
                                for hit in result_block.get("hits", []):
                                    product = _extract_from_algolia_hit(hit)
                                    if product:
                                        products.append(product)
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        logger.debug(f"Algolia JSON 파싱 실패: {e}")

    if products:
        return products

    # ── 2차: JSON-LD fallback (카테고리 페이지 등) ──
    json_ld_pattern = re.compile(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for match in json_ld_pattern.finditer(html):
        try:
            ld_data = json.loads(match.group(1))
            if isinstance(ld_data, dict):
                ld_data = [ld_data]
            if isinstance(ld_data, list):
                for item in ld_data:
                    if item.get("@type") == "Product":
                        product = _extract_from_jsonld(item)
                        if product:
                            products.append(product)
                    elif item.get("@type") == "ItemList":
                        for elem in item.get("itemListElement", []):
                            if isinstance(elem, dict) and elem.get("@type") == "Product":
                                product = _extract_from_jsonld(elem)
                                if product:
                                    products.append(product)
        except (ValueError, KeyError):
            pass

    return products


def _extract_from_algolia_hit(hit: dict) -> dict | None:
    """Algolia hit 객체에서 제품 데이터 추출."""
    name = hit.get("name")
    if not name:
        return None

    product: dict[str, Any] = {"name": name}

    # SKU
    sku = hit.get("sku")
    if sku:
        product["sku"] = str(sku)

    # 가격: {SAR: {default: 12.5, default_formated: "12.50 SAR"}}
    price_data = hit.get("price", {})
    if isinstance(price_data, dict):
        sar_data = price_data.get("SAR", {})
        if isinstance(sar_data, dict):
            default_price = sar_data.get("default")
            if default_price is not None:
                try:
                    product["price"] = float(default_price)
                except (TypeError, ValueError):
                    pass
    elif isinstance(price_data, (int, float)):
        product["price"] = float(price_data)

    # 제조사
    manufacturer = hit.get("manufacturer")
    if manufacturer:
        product["brand"] = manufacturer

    # 제형
    form = hit.get("product_form")
    if form:
        product["form"] = form

    # URL
    url = hit.get("url")
    if url:
        product["url"] = url

    return product


def _extract_from_jsonld(item: dict) -> dict | None:
    """JSON-LD Product 아이템에서 데이터 추출."""
    name = item.get("name")
    if not name:
        return None

    product: dict[str, Any] = {"name": name}

    # 가격
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price")
    if price is not None:
        try:
            product["price"] = float(price)
        except (TypeError, ValueError):
            pass

    # 브랜드
    brand = item.get("brand", {})
    if isinstance(brand, dict):
        product["brand"] = brand.get("name")
    elif isinstance(brand, str):
        product["brand"] = brand

    # URL
    if item.get("url"):
        product["url"] = item["url"]

    # SKU
    if item.get("sku"):
        product["sku"] = item["sku"]

    return product


def map_nahdi_to_schema(product: dict, *, source_url: str) -> dict[str, Any]:
    """Nahdi 제품 데이터를 products 테이블 스키마로 변환."""
    name = product.get("name", "")
    sku = product.get("sku", "")

    # product_id: SKU 우선, 없으면 이름 해시
    if sku:
        pid = f"NAHDI_{sku}"
    else:
        import hashlib
        name_hash = hashlib.md5(name.encode()).hexdigest()[:12]
        pid = f"NAHDI_{name_hash}"

    price = product.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    return {
        "product_id": pid,
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "fob_estimated_usd": None,
        "confidence": 0.75,
        "trade_name": name,
        "scientific_name": None,  # 소매 사이트 — SFDA 매칭 필요
        "strength": None,
        "dosage_form": None,
        "price_local": price,
        "price_sar": price,
        "manufacturer_or_marketing_company": product.get("brand"),
        "agent_or_supplier": None,
        "atc_code": None,
        "source_url": product.get("url", source_url),
        "source_tier": 3,
        "source_name": "nahdi_web",
        "matching_required": True,  # SFDA regulatory_id 보강 대상
        "raw_payload": product,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Nahdi 소매약국 가격 크롤러.

    cfg 옵션:
      - keywords: 검색 키워드 리스트 (기본: 주요 의약품)
      - category_paths: 카테고리 경로 리스트 (기본: ["/c/medicine/"])
      - max_pages_per_keyword: 키워드당 최대 페이지 (기본 5)
      - delay: 요청 간격 초 (기본 2.0, 소매 사이트 보수적)
    """
    inserted = 0
    updated = 0
    skipped = 0

    keywords = cfg.get("keywords", [
        "paracetamol", "ibuprofen", "amoxicillin", "omeprazole",
        "metformin", "amlodipine", "atorvastatin", "aspirin",
    ])
    category_paths = cfg.get("category_paths", ["/c/medicine/"])
    max_pages = cfg.get("max_pages_per_keyword", 5)
    delay = cfg.get("delay", 2.0)
    source_url = NAHDI_BASE + "/en-sa"

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "nahdi_web", {
        "keywords": keywords,
        "category_paths": category_paths,
    })
    metrics.inc("crawl_attempts")

    logger.info(f"Nahdi 크롤링 시작 ({len(keywords)} keywords, {len(category_paths)} categories)")

    seen_ids: set[str] = set()

    with NahdiClient(delay=delay) as client:
        # 1. 키워드 검색 (Algolia API 직접 호출)
        for keyword in keywords:
            for page in range(max_pages):
                t_page = time.time()
                try:
                    products = client.search(keyword, page=page)
                except httpx.HTTPStatusError as e:
                    logger.warning(f"Nahdi 검색 실패 (keyword={keyword}, page={page}): {e}")
                    audit.log("error", "nahdi_web", {
                        "keyword": keyword, "page": page, "status": getattr(e.response, 'status_code', None),
                    })
                    break
                except Exception as e:
                    logger.warning(f"Nahdi 검색 오류 (keyword={keyword}): {e}")
                    audit.log("error", "nahdi_web", {"keyword": keyword, "error": str(e)})
                    break

                if not products:
                    break

                audit.log("page_fetched", "nahdi_web", {
                    "keyword": keyword, "page": page, "products": len(products),
                })

                for product in products:
                    record = map_nahdi_to_schema(product, source_url=source_url)
                    record = normalize_record(record)

                    # 중복 방지
                    pid = record["product_id"]
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    # 소스 신뢰도 보정
                    bonus = reputation.confidence_bonus("nahdi_web")
                    record["confidence"] = min(1.0, max(0.0, (record.get("confidence") or 0.75) + bonus))

                    if not dry_run:
                        try:
                            resp = sb.table("products").upsert(
                                record, on_conflict="product_id"
                            ).execute()
                            if getattr(resp, "data", None):
                                inserted += 1
                        except Exception as e:
                            skipped += 1
                            logger.error(f"DB 저장 실패: {e}")
                    else:
                        inserted += 1

                    metrics.inc("records_processed")
                    audit.log("record_processed", "nahdi_web", {
                        "product_id": pid,
                        "price": record.get("price_sar"),
                        "confidence": record.get("confidence"),
                    })

                metrics.observe("page_fetch_sec", time.time() - t_page)

    # 완료
    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("nahdi_web", True)

    audit.log("crawl_finished", "nahdi_web", {
        "unique_products": len(seen_ids),
        "inserted": inserted,
        "skipped": skipped,
        "metrics": metrics.summary(),
    })

    logger.info(f"Nahdi 크롤링 완료: {len(seen_ids)} unique products, inserted={inserted}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
