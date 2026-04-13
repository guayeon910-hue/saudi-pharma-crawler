"""
sfda_web.py -- SFDA 공개 웹 엔드포인트 클라이언트

SFDA Developer Portal API는 자국민 대상으로만 키를 발급하므로,
공개 웹사이트(www.sfda.gov.sa)의 PHP JSON 엔드포인트를 사용한다.

엔드포인트:
  - GetDrugs.php?page=N          : 전체 목록 (페이지당 10건, ~876페이지)
  - GetDrugsSearch3.php?...&page=N : 검색 (TradeName, ScientificName, Agent, ManufacturerName, RegNo)
  - GetDrugAgents.php?search=등록번호 : 에이전트(대리점) 조회

sfda_oauth.py 대비:
  - 인증 불필요 (OAuth 토큰 없음)
  - 필드 42개 (Developer API ~20개보다 많음)
  - 총 ~8,756건 (2026-04 기준)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from antibot import pick_ua

logger = logging.getLogger("sfda_web")

SFDA_BASE = "https://www.sfda.gov.sa"
DRUGS_LIST_URL = f"{SFDA_BASE}/GetDrugs.php"
DRUGS_SEARCH_URL = f"{SFDA_BASE}/GetDrugsSearch3.php"
DRUGS_AGENTS_URL = f"{SFDA_BASE}/GetDrugAgents.php"


class SFDAWebClient:
    """SFDA 공개 웹 엔드포인트 클라이언트.

    - 인증 불필요
    - 페이지 단위 조회 (page=1~876)
    - rate limit 보수적 적용 (기본 0.5초 간격)
    """

    def __init__(self, timeout: float = 15.0, delay: float = 0.5) -> None:
        self._delay = delay
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "application/json",
                "Referer": f"{SFDA_BASE}/en/drugs-list",
            },
            follow_redirects=True,
        )
        self._last_request: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SFDAWebClient":
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
        """전체 의약품 목록에서 특정 페이지 조회.

        Returns:
            {results: [...], currentPage, pageCount, pageSize, rowCount, ...}
        """
        return self._get_json(DRUGS_LIST_URL, {"page": str(page)})

    def search(
        self,
        *,
        trade_name: str = "",
        scientific_name: str = "",
        agent: str = "",
        manufacturer: str = "",
        reg_no: str = "",
        page: int = 1,
    ) -> dict[str, Any]:
        """의약품 검색."""
        params = {
            "TradeName": trade_name,
            "ScientificName": scientific_name,
            "Agent": agent,
            "ManufacturerName": manufacturer,
            "RegNo": reg_no,
            "page": str(page),
        }
        return self._get_json(DRUGS_SEARCH_URL, params)

    def get_agents(self, register_number: str) -> dict[str, Any]:
        """등록번호로 에이전트(대리점) 조회."""
        return self._get_json(DRUGS_AGENTS_URL, {"search": register_number})

    def iter_all(self, start_page: int = 1, max_pages: int | None = None):
        """전체 목록을 페이지 단위로 순회하는 제너레이터.

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
        trade_name: str = "",
        scientific_name: str = "",
        agent: str = "",
        manufacturer: str = "",
        reg_no: str = "",
        max_pages: int | None = None,
    ):
        """검색 결과를 페이지 단위로 순회하는 제너레이터."""
        page = 1
        while True:
            data = self.search(
                trade_name=trade_name,
                scientific_name=scientific_name,
                agent=agent,
                manufacturer=manufacturer,
                reg_no=reg_no,
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


def map_web_to_schema(item: dict[str, Any], *, source_url: str) -> dict[str, Any]:
    """공개 웹 API 응답 1건을 products 테이블 INSERT용 dict로 변환.

    필드 매핑: 공개 API 키 -> DB 스키마 키
    """
    strength_val = item.get("strength")
    strength_unit = item.get("strengthUnit", "")
    # strengthUnit에 trailing comma가 붙는 경우 제거 ("%," -> "%")
    if strength_unit:
        strength_unit = strength_unit.rstrip(",").strip()
    if strength_val and strength_unit:
        strength = f"{strength_val} {strength_unit}"
    else:
        strength = str(strength_val) if strength_val else None

    price = item.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    return {
        "product_id": f"SFDA_{item.get('registerNumber', 'UNKNOWN')}",
        "country": "SA",
        "currency": "SAR",
        "market_segment": "retail",
        "fob_estimated_usd": None,
        "confidence": 0.92,
        "regulatory_id": str(item.get("registerNumber", "")),
        "trade_name": item.get("tradeName"),
        "scientific_name": item.get("scientificName"),
        "strength": strength,
        "dosage_form": item.get("doesageForm"),  # SFDA 오타 그대로
        "price_local": price,
        "price_sar": price,
        "manufacturer_or_marketing_company": (
            item.get("manufacturerName") or item.get("companyName")
        ),
        "agent_or_supplier": item.get("agent"),
        "atc_code": item.get("atcCode1"),
        "source_url": source_url,
        "source_tier": 1,
        "source_name": "sfda_web",
        "raw_payload": item,
    }
