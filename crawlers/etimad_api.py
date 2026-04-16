"""
crawlers/etimad_api.py -- Etimad 공공조달 API 크롤러

Etimad (https://apiportal.etimad.sa)의 ContractsPlus API를 통해
사우디 공공조달 계약 데이터를 수집한다.

⚠️ 상태: API 구독 키 필요 (ETIMAD_API_KEY)
   → 환경변수 ETIMAD_API_KEY가 없으면 건너뜀
   → NAFATH 인증 우회 금지 (sources.yaml 규정)

수집 대상:
  - trade_name, agent_or_supplier, price_sar (계약 기준)
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua
from supabase_state import AuditLog, MetricsCollector, SourceReputation

import httpx

logger = logging.getLogger("crawlers.etimad_api")

ETIMAD_BASE = "https://apiportal.etimad.sa"
CONTRACTS_URL = f"{ETIMAD_BASE}/api/ContractsPlus/v1/contracts"


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Etimad 공공조달 API 크롤러.

    cfg 옵션:
      - api_key: API 키 (env ETIMAD_API_KEY 우선)
      - max_pages: 최대 페이지 수 (기본 10)
      - page_size: 페이지당 건수 (기본 50)
      - delay: 요청 간격 초 (기본 1.0)
      - keyword: 검색 키워드 (기본: "pharmaceutical")
    """
    inserted = 0
    updated = 0  # NOTE: Supabase upsert does not distinguish insert vs update; always 0.
    skipped = 0

    api_key = os.environ.get("ETIMAD_API_KEY") or cfg.get("api_key", "")
    max_pages = cfg.get("max_pages", 10)
    page_size = cfg.get("page_size", 50)
    delay = cfg.get("delay", 1.0)
    keyword = cfg.get("keyword", "pharmaceutical")

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "etimad_api", {"keyword": keyword})
    metrics.inc("crawl_attempts")

    # API 키 체크
    if not api_key:
        logger.warning("ETIMAD_API_KEY 미설정 — 크롤링 건너뜀")
        audit.log("error", "etimad_api", {"reason": "no_api_key"})
        metrics.inc("crawl_skipped")
        return {
            "rows_inserted": 0,
            "rows_updated": 0,
            "rows_skipped": 0,
            "audit_log": audit.to_json(),
            "metrics": metrics.to_json(),
        }

    logger.info(f"Etimad 크롤링 시작 (keyword={keyword})")

    retry_429_count = 0

    client = httpx.Client(
        timeout=30.0,
        headers={
            "User-Agent": pick_ua(),
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
        },
    )

    try:
        for page in range(1, max_pages + 1):
            t_page = time.time()
            params = {
                "keyword": keyword,
                "page": str(page),
                "pageSize": str(page_size),
            }

            try:
                resp = client.get(CONTRACTS_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                retry_429_count = 0  # 성공 시 429 재시도 카운터 리셋
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 401:
                    logger.error("Etimad API 인증 실패 (401)")
                    audit.log("error", "etimad_api", {"status": 401})
                    break
                elif status == 403:
                    logger.error("Etimad API 접근 거부 (403)")
                    audit.log("error", "etimad_api", {"status": 403})
                    break
                elif status == 429:
                    retry_429_count += 1
                    logger.warning(
                        f"Etimad API 레이트 리밋 (429) — attempt {retry_429_count}/3"
                    )
                    if retry_429_count >= 3:
                        logger.error("Etimad API 429 limit reached 3 times, aborting")
                        audit.log("error", "etimad_api", {"reason": "429_retry_exceeded"})
                        break
                    time.sleep(delay * 5)
                    continue
                else:
                    raise

            contracts = data.get("data") or data.get("results") or data.get("items") or []
            if not contracts:
                logger.info(f"페이지 {page}: 데이터 없음, 종료")
                break

            audit.log("page_fetched", "etimad_api", {
                "page": page, "contracts": len(contracts),
            })

            for contract in contracts:
                record = _map_contract_to_schema(contract)

                bonus = reputation.confidence_bonus("etimad_api")
                record["confidence"] = min(0.99, max(0.0, record["confidence"] + bonus))

                if not dry_run:
                    try:
                        sb.table("contracts").upsert(
                            record, on_conflict="contract_id"
                        ).execute()
                        inserted += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(f"DB 저장 실패: {e}")
                else:
                    inserted += 1

                metrics.inc("records_processed")

            metrics.observe("page_fetch_sec", time.time() - t_page)

            # 다음 페이지 존재 여부
            total = data.get("totalCount") or data.get("total", 0)
            if page * page_size >= total:
                break

            time.sleep(delay)

    finally:
        client.close()

    metrics.inc("crawl_success" if inserted > 0 else "crawl_partial")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("etimad_api", inserted > 0)

    audit.log("crawl_finished", "etimad_api", {
        "inserted": inserted, "skipped": skipped,
    })

    logger.info(f"Etimad 완료: inserted={inserted}, skipped={skipped}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }


def _map_contract_to_schema(contract: dict) -> dict[str, Any]:
    """Etimad 계약 데이터를 스키마로 변환."""
    contract_id = (
        contract.get("contractId")
        or contract.get("id")
        or contract.get("referenceNumber")
        or "UNKNOWN"
    )

    return {
        "contract_id": f"ETIMAD_{contract_id}",
        "country": "SA",
        "source_name": "etimad_api",
        "source_tier": 2,
        "market_segment": "tender",
        "confidence": 0.85,
        "title": contract.get("title") or contract.get("name"),
        "supplier_name": contract.get("supplierName") or contract.get("vendor"),
        "contract_value": contract.get("value") or contract.get("amount"),
        "currency": "SAR",
        "start_date": contract.get("startDate"),
        "end_date": contract.get("endDate"),
        "status": contract.get("status"),
        "category": contract.get("category"),
        "source_url": f"{ETIMAD_BASE}/en/api_products/ContractsPlus",
        "raw_payload": contract,
    }
