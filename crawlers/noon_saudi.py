"""
crawlers/noon_saudi.py -- Noon Saudi 대형 상거래 가격 크롤러

Noon (https://www.noon.com/saudi-en)의 약국 카테고리에서
보조 가격 데이터를 수집한다.

⚠️ 상태:
  - Cloudflare 방어 활성화 (403 반환, 2026-04-12 확인)
  - sources.yaml에서 enabled: false (기본 비활성)
  - 법무 검토 후 활성화 예정

수집 대상: trade_name, price_sar (보조 데이터)
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

logger = logging.getLogger("crawlers.noon_saudi")

NOON_BASE = "https://www.noon.com"
NOON_PHARMACY_URL = f"{NOON_BASE}/saudi-en/health/main-pharmacy-sa/"
NOON_SEARCH_URL = f"{NOON_BASE}/saudi-en/search/"


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Noon Saudi 가격 크롤러.

    ⚠️ Cloudflare 방어 + enabled: false.
    법무 검토 완료 후 활성화.

    cfg 옵션:
      - keywords: 검색 키워드 리스트
      - max_pages_per_keyword: 키워드당 최대 페이지 (기본 2)
      - delay: 요청 간격 초 (기본 3.0)
    """
    inserted = 0
    updated = 0
    skipped = 0

    keywords = cfg.get("keywords", ["paracetamol", "ibuprofen"])
    max_pages = cfg.get("max_pages_per_keyword", 2)
    delay = cfg.get("delay", 3.0)

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "noon_saudi", {"keywords": keywords})
    metrics.inc("crawl_attempts")

    logger.info("Noon Saudi 크롤링 시작")

    client = httpx.Client(
        timeout=20.0,
        headers={
            "User-Agent": pick_ua(),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True,
    )

    seen_ids: set[str] = set()

    try:
        for keyword in keywords:
            for page in range(1, max_pages + 1):
                t_page = time.time()
                url = f"{NOON_SEARCH_URL}?q={keyword}"
                if page > 1:
                    url += f"&page={page}"

                try:
                    time.sleep(delay)
                    resp = client.get(url)

                    # Anti-bot 감지
                    ab_type = detect_antibot(
                        resp.status_code,
                        resp.text[:2000],
                        dict(resp.headers),
                    )
                    if ab_type != AntiBotType.NONE:
                        logger.warning(f"Noon anti-bot 감지: {ab_type.value}")
                        audit.log("antibot_detected", "noon_saudi", {
                            "type": ab_type.value,
                        })
                        raise httpx.HTTPStatusError(
                            f"Anti-bot: {ab_type.value}",
                            request=resp.request,
                            response=resp,
                        )

                    resp.raise_for_status()
                    html = resp.text

                except httpx.HTTPStatusError:
                    raise  # main.py 핸들러로 전파
                except Exception as e:
                    logger.warning(f"Noon 요청 실패: {e}")
                    break

                # Noon은 SPA 기반 — 서버사이드 렌더링된 JSON 데이터 추출
                products = _parse_noon_products(html)
                if not products:
                    break

                audit.log("page_fetched", "noon_saudi", {
                    "keyword": keyword, "page": page, "products": len(products),
                })

                for product in products:
                    record = _map_noon_to_schema(product)
                    record = normalize_record(record)

                    pid = record["product_id"]
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)

                    bonus = reputation.confidence_bonus("noon_saudi")
                    record["confidence"] = min(1.0, max(0.0, (record.get("confidence") or 0.55) + bonus))

                    if not dry_run:
                        try:
                            sb.table("products").upsert(
                                record, on_conflict="product_id"
                            ).execute()
                            inserted += 1
                        except Exception as e:
                            skipped += 1
                    else:
                        inserted += 1

                    metrics.inc("records_processed")

                metrics.observe("page_fetch_sec", time.time() - t_page)

    finally:
        client.close()

    metrics.inc("crawl_success" if inserted > 0 else "crawl_partial")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("noon_saudi", inserted > 0)

    audit.log("crawl_finished", "noon_saudi", {
        "inserted": inserted, "skipped": skipped,
    })

    logger.info(f"Noon Saudi 완료: inserted={inserted}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }


def _parse_noon_products(html: str) -> list[dict]:
    """Noon HTML/JSON에서 제품 데이터 추출.

    Noon은 Next.js 기반 SPA로, __NEXT_DATA__ JSON에 제품 데이터가 포함됨.
    """
    import json

    products = []

    # __NEXT_DATA__ 패턴
    next_data_match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if next_data_match:
        try:
            data = json.loads(next_data_match.group(1))
            # Noon의 검색 결과 구조 탐색
            props = data.get("props", {}).get("pageProps", {})
            hits = (
                props.get("hits")
                or props.get("products")
                or props.get("searchResults", {}).get("hits", [])
            )
            if isinstance(hits, list):
                for hit in hits:
                    name = hit.get("name") or hit.get("title")
                    price = (
                        hit.get("price")
                        or hit.get("sale_price")
                        or (hit.get("offers", {}) or {}).get("price")
                    )
                    if name:
                        products.append({
                            "name": name,
                            "price": float(price) if price else None,
                            "sku": hit.get("sku") or hit.get("id"),
                            "brand": hit.get("brand"),
                            "url": hit.get("url"),
                        })
        except (ValueError, KeyError, TypeError):
            pass

    # Fallback: JSON-LD
    if not products:
        json_ld_re = re.compile(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            re.DOTALL,
        )
        for match in json_ld_re.finditer(html):
            try:
                item = json.loads(match.group(1))
                if isinstance(item, dict) and item.get("@type") == "Product":
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    products.append({
                        "name": item.get("name"),
                        "price": float(offers.get("price", 0)) or None,
                        "sku": item.get("sku"),
                        "brand": (item.get("brand", {}) or {}).get("name")
                                 if isinstance(item.get("brand"), dict)
                                 else item.get("brand"),
                    })
            except (ValueError, KeyError):
                pass

    return products


def _map_noon_to_schema(product: dict) -> dict[str, Any]:
    """Noon 제품 데이터를 products 스키마로 변환."""
    name = product.get("name", "")
    sku = product.get("sku", "")

    if sku:
        pid = f"NOON_{sku}"
    else:
        import hashlib
        pid = f"NOON_{hashlib.md5(name.encode()).hexdigest()[:12]}"

    price = product.get("price")
    return {
        "product_id": pid,
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "confidence": 0.55,
        "trade_name": name,
        "scientific_name": None,
        "price_local": price,
        "price_sar": price,
        "manufacturer_or_marketing_company": product.get("brand"),
        "source_url": product.get("url") or NOON_PHARMACY_URL,
        "source_tier": 5,
        "source_name": "noon_saudi",
        "matching_required": True,
        "raw_payload": product,
    }
