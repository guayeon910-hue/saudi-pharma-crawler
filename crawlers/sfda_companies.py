"""
crawlers/sfda_companies.py -- SFDA 제약사/대리점 마스터 크롤러

SFDA 공개 웹사이트(www.sfda.gov.sa)의 제약사 PHP JSON 엔드포인트를 사용한다.

엔드포인트:
  - Getdrugcompanies.php?page=N             : 전체 목록 (페이지당 10건)
  - GetDrugCompaniesSearch.php?...&page=N    : 검색

제약사/대리점 마스터 데이터:
  - companyRegister, drugType, country_Desc
  - productionLine, companY_ENG_DESC, companY_ADDRESS
  - agenT_NAME, agenT_ADDRESS 등
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any

# assets/snippets 를 경로에 추가
sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua
from supabase_state import AuditLog, MetricsCollector, SourceReputation

import httpx

logger = logging.getLogger("crawlers.sfda_companies")

SFDA_BASE = "https://www.sfda.gov.sa"
COMPANIES_LIST_URL = f"{SFDA_BASE}/Getdrugcompanies.php"
COMPANIES_SEARCH_URL = f"{SFDA_BASE}/GetDrugCompaniesSearch.php"


class SFDACompaniesClient:
    """SFDA 제약사 공개 웹 엔드포인트 클라이언트.

    - 인증 불필요
    - 페이지당 10건
    - rate limit 보수적 적용 (기본 0.5초 간격)
    """

    def __init__(self, timeout: float = 15.0, delay: float = 0.5) -> None:
        self._delay = delay
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "application/json",
                "Referer": f"{SFDA_BASE}/en/drug-companies",
            },
            follow_redirects=True,
        )
        self._last_request: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SFDACompaniesClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        self._throttle()
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_page(self, page: int = 1) -> dict[str, Any]:
        """전체 제약사 목록에서 특정 페이지 조회."""
        return self._get_json(COMPANIES_LIST_URL, {"page": str(page)})

    def search(
        self,
        *,
        company_name: str = "",
        agent_name: str = "",
        country: str = "",
        page: int = 1,
    ) -> dict[str, Any]:
        """제약사 검색."""
        params = {
            "CompanyEnName": company_name,
            "AgentName": agent_name,
            "CountryDesc": country,
            "page": str(page),
        }
        return self._get_json(COMPANIES_SEARCH_URL, params)

    def iter_all(self, start_page: int = 1, max_pages: int | None = None):
        """전체 목록을 페이지 단위로 순회.

        Yields:
            (page_number, [item, item, ...])
        """
        page = start_page
        while True:
            data = self.get_page(page)
            results = data.get("results") or []
            if not results:
                break
            yield page, results
            page_count = data.get("pageCount", 0)
            if page >= page_count:
                break
            if max_pages and (page - start_page + 1) >= max_pages:
                break
            page += 1

    def iter_search(
        self,
        *,
        company_name: str = "",
        agent_name: str = "",
        country: str = "",
        max_pages: int | None = None,
    ):
        """검색 결과를 페이지 단위로 순회."""
        page = 1
        while True:
            data = self.search(
                company_name=company_name,
                agent_name=agent_name,
                country=country,
                page=page,
            )
            results = data.get("results") or []
            if not results:
                break
            yield page, results
            page_count = data.get("pageCount", 0)
            if page >= page_count:
                break
            if max_pages and page >= max_pages:
                break
            page += 1


def map_company_to_schema(item: dict[str, Any], *, source_url: str) -> dict[str, Any]:
    """제약사 API 응답 1건을 companies 테이블 INSERT용 dict로 변환.

    SFDA Company API 필드:
      companyRegister, drugType, country_Desc,
      productionLine, companY_ENG_DESC, companY_ADDRESS,
      agenT_NAME, agenT_ADDRESS
    """
    company_register = item.get("companyRegister") or item.get("companyregister", "UNKNOWN")

    return {
        "company_id": f"SFDA_CO_{company_register}",
        "country": "SA",
        "company_register": str(company_register),
        "company_name": (
            item.get("companY_ENG_DESC")
            or item.get("company_eng_desc")
            or item.get("companyEngDesc")
        ),
        "company_address": (
            item.get("companY_ADDRESS")
            or item.get("company_address")
            or item.get("companyAddress")
        ),
        "drug_type": item.get("drugType") or item.get("drugtype"),
        "production_line": item.get("productionLine") or item.get("productionline"),
        "country_desc": item.get("country_Desc") or item.get("country_desc"),
        "agent_name": (
            item.get("agenT_NAME")
            or item.get("agent_name")
            or item.get("agentName")
        ),
        "agent_address": (
            item.get("agenT_ADDRESS")
            or item.get("agent_address")
            or item.get("agentAddress")
        ),
        "source_url": source_url,
        "source_name": "sfda_companies",
        "source_tier": 1,
        "confidence": 0.80,
        "raw_payload": item,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """SFDA 제약사 마스터 크롤러.

    cfg 옵션:
      - mode: "full" (전체 목록) | "search" (검색, 기본값)
      - company_name: 검색 회사명 (mode=search)
      - agent_name: 검색 에이전트명 (mode=search)
      - start_page: 시작 페이지 (기본 1)
      - max_pages: 최대 페이지 수 (기본 None=전체)
      - delay: 요청 간격 초 (기본 0.5)
    """
    inserted = 0
    updated = 0
    skipped = 0

    mode = cfg.get("mode", "full")
    company_name = cfg.get("company_name", "")
    agent_name = cfg.get("agent_name", "")
    start_page = cfg.get("start_page", 1)
    max_pages = cfg.get("max_pages")
    delay = cfg.get("delay", 0.5)
    source_url = "https://www.sfda.gov.sa/en/drug-companies"

    # 감사 로그 + 메트릭 + 소스 신뢰도
    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "sfda_companies", {"mode": mode})
    metrics.inc("crawl_attempts")

    logger.info(f"SFDA 제약사 크롤링 시작 (mode={mode})")

    with SFDACompaniesClient(delay=delay) as client:
        if mode == "full":
            page_iter = client.iter_all(
                start_page=start_page, max_pages=max_pages
            )
        else:
            page_iter = client.iter_search(
                company_name=company_name,
                agent_name=agent_name,
                max_pages=max_pages,
            )

        for page_num, items in page_iter:
            t_page = time.time()
            logger.info(f"페이지 {page_num}: {len(items)}건 처리 중")
            audit.log("page_fetched", "sfda_companies", {"page": page_num, "items": len(items)})

            for item in items:
                record = map_company_to_schema(item, source_url=source_url)

                # 소스 신뢰도 보정
                bonus = reputation.confidence_bonus("sfda_companies")
                record["confidence"] = min(0.99, max(0.0, record["confidence"] + bonus))

                if not dry_run:
                    try:
                        resp = sb.table("companies").upsert(
                            record, on_conflict="company_id"
                        ).execute()

                        if getattr(resp, "data", None):
                            inserted += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            f"DB 저장 실패 (company_id={record['company_id']}): {e}"
                        )
                else:
                    inserted += 1

                metrics.inc("records_processed")
                audit.log("record_processed", "sfda_companies", {
                    "company_id": record.get("company_id"),
                    "confidence": record.get("confidence"),
                })

            metrics.observe("page_fetch_sec", time.time() - t_page)

    # 완료
    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("sfda_companies", True)

    audit.log("crawl_finished", "sfda_companies", {
        "inserted": inserted,
        "skipped": skipped,
        "metrics": metrics.summary(),
    })

    logger.info(f"SFDA 제약사 크롤링 완료: inserted={inserted}, skipped={skipped}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
