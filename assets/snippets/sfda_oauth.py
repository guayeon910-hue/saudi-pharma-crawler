"""
sfda_oauth.py — SFDA Developer Portal OAuth2 Client Credentials 플로우

SFDA 등록 의약품 API는 Bearer 토큰 기반이며 만료 24시간.
이 모듈은 토큰을 발급받고, 만료 전까지 재사용하며, 만료되면 자동 갱신한다.

사용 예:
    from sfda_oauth import SFDAClient

    client = SFDAClient(
        client_id=os.environ["SFDA_CLIENT_ID"],
        client_secret=os.environ["SFDA_CLIENT_SECRET"],
    )
    result = client.search_drug(keyword="Gadobutrol")
    for item in result.get("items", []):
        print(item["tradeName"], item["registrationNumber"])
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


SFDA_TOKEN_URL = "https://developer.sfda.gov.sa/oauth/token"
SFDA_API_BASE = "https://developer.sfda.gov.sa/apidoc/registered-drug-service/84"


@dataclass
class _Token:
    access_token: str
    expires_at: float  # epoch seconds

    @property
    def is_expired(self) -> bool:
        # 만료 60초 전부터 갱신 (네트워크 지연 버퍼)
        return time.time() >= (self.expires_at - 60)


class SFDAClient:
    """SFDA API 호출 래퍼.

    - 토큰 자동 발급·갱신
    - httpx 세션 재사용 (keep-alive)
    - 429/5xx 재시도는 호출자(backoff_retry)가 담당
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.client_id = client_id or os.environ["SFDA_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SFDA_CLIENT_SECRET"]
        self._token: _Token | None = None
        self._http = httpx.Client(
            timeout=timeout,
            http2=True,
            headers={"User-Agent": "UPharmaExportAI/1.0 (research)"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SFDAClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ─── 토큰 관리 ─────────────────────────────────
    def _fetch_token(self) -> _Token:
        resp = self._http.post(
            SFDA_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
        return _Token(
            access_token=payload["access_token"],
            # 사양상 24h(86400)이지만 응답 expires_in 우선
            expires_at=time.time() + float(payload.get("expires_in", 86400)),
        )

    def _get_bearer(self) -> str:
        if self._token is None or self._token.is_expired:
            self._token = self._fetch_token()
        return self._token.access_token

    # ─── API 호출 ─────────────────────────────────
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_bearer()}"}

    def search_drug(
        self,
        *,
        keyword: str | None = None,
        barcode: str | None = None,
        registration_number: str | None = None,
    ) -> dict[str, Any]:
        """등록 의약품 검색. 파라미터 하나 이상 필요."""
        params: dict[str, str] = {}
        if barcode:
            params["barcode"] = barcode
        if registration_number:
            params["registrationNumber"] = registration_number
        if keyword:
            params["keyword"] = keyword
        if not params:
            raise ValueError("barcode, registration_number, keyword 중 하나는 필요")

        resp = self._http.get(
            SFDA_API_BASE,
            params=params,
            headers=self._auth_headers(),
        )
        # 401이면 토큰 강제 갱신 후 1회 재시도
        if resp.status_code == 401:
            self._token = None
            resp = self._http.get(
                SFDA_API_BASE,
                params=params,
                headers=self._auth_headers(),
            )
        resp.raise_for_status()
        return resp.json()


# ─── 응답 → 8필드 스키마 매핑 ─────────────────────────
def map_sfda_to_schema(item: dict[str, Any], *, source_url: str) -> dict[str, Any]:
    """SFDA API 응답 1건을 saudi_products 삽입용 dict로 변환.

    strength는 strength + strengthUnit 병합, 나머지는 null-safe 매핑.
    Normalizer는 별도 (normalizer.py) — 여기선 원문 그대로만 넘긴다.
    """
    strength_val = item.get("strength")
    strength_unit = item.get("strengthUnit")
    if strength_val and strength_unit:
        strength = f"{strength_val} {strength_unit}"
    else:
        strength = strength_val or None

    return {
        # 공통 6컬럼
        "product_id": f"SFDA_{item.get('registrationNumber', 'UNKNOWN')}",
        "market_segment": "retail",       # 공식 등록은 기본 retail로
        "fob_estimated_usd": None,        # 크롤러는 건드리지 않음
        "confidence": 0.92,               # Tier 1 API 기본값
        # 의약품 공통
        "regulatory_id": item.get("registrationNumber"),
        "trade_name": item.get("tradeName"),
        # 사우디 확장
        "scientific_name": item.get("scientificName"),
        "strength": strength,
        "dosage_form": item.get("dosageForm"),
        "price_sar": item.get("price"),
        "manufacturer_or_marketing_company": (
            item.get("manufacturerName") or item.get("marketingCompany")
        ),
        "agent_or_supplier": (
            item.get("firstAgent")
            or item.get("secondAgent")
            or item.get("thirdAgent")
        ),
        "atc_code": item.get("atcCode"),
        "source_url": source_url,
        "source_tier": 1,
        "source_name": "sfda_api",
        "raw_payload": item,              # API JSON은 저장 OK
    }
