"""
crawlers/sfda_drugs_list_html.py -- SFDA 의약품 목록 HTML 크롤러 (sfda_api의 fallback)

sfda_api.py와 동일한 PHP JSON 엔드포인트를 사용하지만,
OAuth 인증 없이 공개 웹사이트 경로로 접근한다.
sfda_api가 OAuth 장애로 실패할 때의 대체 경로(fallback_for: sfda_api) 역할.

실질적으로 sfda_api.py와 동일한 엔드포인트(GetDrugs.php)를 호출하므로
코드를 재사용한다. 차이점:
  - source_name: "sfda_drugs_list_html"
  - confidence_default: 0.85 (sfda_api의 0.92보다 낮음)
  - mode 기본값: "full" (sfda_api는 "search")
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from sfda_web import SFDAWebClient, map_web_to_schema
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record
from supabase_state import AuditLog, MetricsCollector, SourceReputation

logger = logging.getLogger("crawlers.sfda_drugs_list_html")

# INN 정규화기: 모듈 로드 시 1회 초기화
_inn_norm = INNNormalizer()
_inn_norm.load_reference()


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """SFDA 의약품 HTML fallback 크롤러.

    cfg 옵션:
      - mode: "full" (기본, 전체 목록) | "search" (검색)
      - keyword: 검색 키워드 (mode=search일 때)
      - start_page: 시작 페이지 (기본 1)
      - max_pages: 최대 페이지 수 (기본 None=전체)
      - delay: 요청 간격 초 (기본 0.5)
    """
    inserted = 0
    updated = 0
    skipped = 0

    mode = cfg.get("mode", "full")
    keyword = cfg.get("keyword", "")
    start_page = cfg.get("start_page", 1)
    max_pages = cfg.get("max_pages")
    delay = cfg.get("delay", 0.5)
    source_url = "https://www.sfda.gov.sa/en/drugs-list"

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "sfda_drugs_list_html", {"mode": mode, "keyword": keyword})
    metrics.inc("crawl_attempts")

    logger.info(f"SFDA HTML fallback 크롤링 시작 (mode={mode})")

    # 기존 가격 그룹 (이상치 검사용)
    existing_prices: list[float] = []
    if not dry_run:
        try:
            rows = (sb.table("products")
                    .select("price_local")
                    .eq("country", "SA")
                    .not_.is_("price_local", "null")
                    .is_("deleted_at", "null")
                    .execute())
            existing_prices = [float(r["price_local"]) for r in (rows.data or [])]
        except Exception as e:
            logger.warning(f"기존 가격 조회 실패: {e}")

    with SFDAWebClient(delay=delay) as client:
        if mode == "full":
            page_iter = client.iter_all(
                start_page=start_page, max_pages=max_pages
            )
        else:
            page_iter = client.iter_search(
                trade_name=keyword, max_pages=max_pages
            )

        for page_num, items in page_iter:
            t_page = time.time()
            logger.info(f"페이지 {page_num}: {len(items)}건 처리 중")
            audit.log("page_fetched", "sfda_drugs_list_html", {"page": page_num, "items": len(items)})

            for item in items:
                record = map_web_to_schema(item, source_url=source_url)
                # fallback 소스명 오버라이드
                record["source_name"] = "sfda_drugs_list_html"
                record["confidence"] = 0.85  # sfda_api(0.92)보다 낮음

                record = normalize_record(record)
                record = _inn_norm.normalize_record(record)
                record = flag_record(record, existing_prices)

                # 소스 신뢰도 보정
                bonus = reputation.confidence_bonus("sfda_drugs_list_html")
                record["confidence"] = min(0.99, max(0.0, (record.get("confidence") or 0.85) + bonus))

                if not dry_run:
                    try:
                        resp = sb.table("products").upsert(
                            record, on_conflict="product_id"
                        ).execute()

                        if getattr(resp, "data", None):
                            inserted += 1
                            price = record.get("price_local")
                            if not record.get("outlier_flagged") and price is not None:
                                existing_prices.append(float(price))
                    except Exception as e:
                        skipped += 1
                        logger.error(f"DB 저장 실패: {e}")
                else:
                    inserted += 1

                metrics.inc("records_processed")
                if record.get("outlier_flagged"):
                    metrics.inc("outliers_detected")
                audit.log("record_processed", "sfda_drugs_list_html", {
                    "product_id": record.get("product_id"),
                    "outlier": record.get("outlier_flagged", False),
                    "confidence": record.get("confidence"),
                })

            metrics.observe("page_fetch_sec", time.time() - t_page)

    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("sfda_drugs_list_html", True)

    audit.log("crawl_finished", "sfda_drugs_list_html", {
        "inserted": inserted,
        "skipped": skipped,
        "metrics": metrics.summary(),
    })

    logger.info(f"SFDA HTML fallback 완료: inserted={inserted}, skipped={skipped}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
