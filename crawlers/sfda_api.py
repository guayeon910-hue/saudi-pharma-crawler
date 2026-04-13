"""
crawlers/sfda_api.py -- SFDA 공개 웹 엔드포인트 크롤러

SFDA Developer Portal API 키가 자국민 대상으로만 발급되므로,
공개 웹사이트(www.sfda.gov.sa)의 PHP JSON 엔드포인트를 사용한다.

엔드포인트:
  - GetDrugs.php?page=N           : 전체 목록 (페이지당 10건, ~876페이지)
  - GetDrugsSearch3.php?...&page=N : 검색
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

# assets/snippets 를 경로에 추가
sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from sfda_web import SFDAWebClient, map_web_to_schema
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record
from supabase_state import AuditLog, MetricsCollector, SourceReputation

logger = logging.getLogger("crawlers.sfda_api")

# INN 정규화기: 모듈 로드 시 1회 초기화
_inn_norm = INNNormalizer()
_inn_norm.load_reference()


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """SFDA 공개 웹 엔드포인트 크롤러.

    cfg 옵션:
      - mode: "full" (전체 목록) | "search" (검색, 기본값)
      - keyword: 검색 키워드 (mode=search일 때, 기본 "paracetamol")
      - start_page: 시작 페이지 (기본 1)
      - max_pages: 최대 페이지 수 (기본 None=전체)
      - delay: 요청 간격 초 (기본 0.5)
    """
    inserted = 0
    updated = 0
    skipped = 0

    mode = cfg.get("mode", "search")
    keyword = cfg.get("keyword", "paracetamol")
    start_page = cfg.get("start_page", 1)
    max_pages = cfg.get("max_pages")
    delay = cfg.get("delay", 0.5)
    source_url = "https://www.sfda.gov.sa/en/drugs-list"

    # ── SciSpace: 감사 로그 + 메트릭 + 소스 신뢰도 ──
    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "sfda_web", {"mode": mode, "keyword": keyword})
    metrics.inc("crawl_attempts")

    logger.info(f"SFDA 웹 크롤링 시작 (mode={mode}, keyword={keyword})")

    # 기존 가격 그룹 조회 (이상치 검사용)
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
            logger.info(f"기존 가격 {len(existing_prices)}건 로드 (이상치 검사용)")
        except Exception as e:
            logger.warning(f"기존 가격 조회 실패, 이상치 검사 건너뜀: {e}")

    with SFDAWebClient(delay=delay) as client:
        # 모드에 따라 이터레이터 선택
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
            audit.log("page_fetched", "sfda_web", {"page": page_num, "items": len(items)})

            for item in items:
                record = map_web_to_schema(item, source_url=source_url)
                record = normalize_record(record)            # 함량/제형/가격 정규화
                record = _inn_norm.normalize_record(record)   # WHO INN 매칭
                record = flag_record(record, existing_prices) # K 통계량 이상치 검사

                # ── 소스 신뢰도 보정 ──
                bonus = reputation.confidence_bonus("sfda_web")
                record["confidence"] = min(1.0, max(0.0, (record.get("confidence") or 0.92) + bonus))

                if not dry_run:
                    try:
                        resp = sb.table("products").upsert(
                            record, on_conflict="product_id"
                        ).execute()

                        if getattr(resp, "data", None):
                            inserted += 1
                            # 정상 가격이면 그룹에 추가
                            price = record.get("price_local")
                            if not record.get("outlier_flagged") and price is not None:
                                existing_prices.append(float(price))
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            f"DB 저장 실패 (product_id={record['product_id']}): {e}"
                        )
                else:
                    inserted += 1

                # ── 메트릭 + 감사 ──
                metrics.inc("records_processed")
                if record.get("outlier_flagged"):
                    metrics.inc("outliers_detected")
                audit.log("record_processed", "sfda_web", {
                    "product_id": record.get("product_id"),
                    "outlier": record.get("outlier_flagged", False),
                    "confidence": record.get("confidence"),
                })

            metrics.observe("page_fetch_sec", time.time() - t_page)

    # ── 완료 ──
    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("sfda_web", True)

    audit.log("crawl_finished", "sfda_web", {
        "inserted": inserted,
        "skipped": skipped,
        "metrics": metrics.summary(),
    })

    logger.info(
        f"SFDA 웹 크롤링 완료: inserted={inserted}, skipped={skipped}"
    )
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
