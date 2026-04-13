"""
[나라명] 규제기관 API 크롤러 — 템플릿

이 파일을 복사한 뒤 자기 나라 규제기관에 맞게 수정한다.
사우디의 crawlers/sfda_api.py를 참고하면 됨.

흐름:
  API 호출 → map_to_schema() → normalize_record() → inn.normalize_record() → DB INSERT

규칙:
  - country, currency 필드 반드시 채울 것
  - price_local에 현지 통화 가격 (USD 아님)
  - inn_normalizer는 그대로 쓰면 됨 (WHO INN은 전 세계 공통)
  - 나라 전용 필드는 raw_payload에 넣을 것
"""

import logging
import sys
from pathlib import Path
from typing import Any

# shared/ 모듈 import
sys.path.append(str(Path(__file__).resolve().parent / "shared"))
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record

logger = logging.getLogger("crawlers.example")

# ─── 설정: 나라별로 바꿔야 하는 것 ─────────────────────
COUNTRY = "XX"           # ← ISO 3166-1 alpha-2
CURRENCY = "XXX"         # ← ISO 4217
SOURCE_NAME = "example_regulator_api"
SOURCE_TIER = 1

# INN 정규화기 초기화
_inn = INNNormalizer()
_inn.load_reference()
# 현지 브랜드 추가 (있으면):
# _inn._brand_map["현지브랜드명"] = "inn_name"


# ─── API 응답 → 공통 스키마 매핑 ────────────────────────
def map_to_schema(item: dict, *, source_url: str) -> dict:
    """API 응답 1건을 products 테이블 INSERT용 dict로 변환.

    ⚠️ 이 함수를 자기 나라 API 응답 구조에 맞게 수정할 것.
    아래는 예시일 뿐.
    """
    return {
        # ── 필수 ──
        "country": COUNTRY,
        "currency": CURRENCY,
        "product_id": f"{COUNTRY}_{item.get('registration_number', 'UNKNOWN')}",
        "trade_name": item.get("trade_name"),
        "market_segment": "retail",
        "confidence": 0.92,       # Tier 1 기본값
        "source_url": source_url,
        "source_tier": SOURCE_TIER,
        "source_name": SOURCE_NAME,

        # ── 있으면 채우기 ──
        "regulatory_id": item.get("registration_number"),
        "scientific_name": item.get("scientific_name"),
        "strength": item.get("strength"),
        "dosage_form": item.get("dosage_form"),
        "price_local": item.get("price"),
        "manufacturer_or_marketing_company": item.get("manufacturer"),
        "agent_or_supplier": item.get("distributor"),
        "atc_code": item.get("atc_code"),

        # ── 나라 전용 필드는 raw_payload에 ──
        "raw_payload": item,
    }


# ─── 실행 함수 ───────────────────────────────────────────
def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """크롤러 진입점. main.py의 dispatch table이 호출한다."""
    inserted = 0
    updated = 0

    source_url = cfg.get("url_seed", "")
    logger.info(f"[{COUNTRY}] 크롤링 시작")

    # ── Step 1: 데이터 가져오기 (여기를 자기 나라 API에 맞게 구현) ──
    # 예시:
    # import httpx
    # resp = httpx.get(source_url, params={...})
    # items = resp.json().get("items", [])
    items = []  # ← 실제 API 호출로 교체

    logger.info(f"[{COUNTRY}] {len(items)}건 수신")

    # ── Step 1.5: 기존 가격 그룹 조회 (이상치 검사용) ──
    existing_prices: list[float] = []
    try:
        rows = (sb.table("products")
                .select("price_local")
                .eq("country", COUNTRY)
                .eq("source_name", SOURCE_NAME)
                .not_.is_("price_local", "null")
                .not_.is_("deleted_at", "null")  # soft-delete 제외
                .execute())
        existing_prices = [float(r["price_local"]) for r in (rows.data or [])]
    except Exception as e:
        logger.warning(f"[{COUNTRY}] 기존 가격 조회 실패, 이상치 검사 건너뜀: {e}")

    for item in items:
        # ── Step 2: 매핑 → 정규화 → INN 매칭 → 이상치 검사 ──
        record = map_to_schema(item, source_url=source_url)
        record = normalize_record(record)           # 함량/제형/가격 정규화
        record = _inn.normalize_record(record)       # WHO INN 매칭
        record = flag_record(record, existing_prices)  # K 통계량 이상치 검사

        # ── Step 3: DB 저장 ──
        if not dry_run:
            try:
                sb.table("products").upsert(
                    record, on_conflict="product_id"
                ).execute()
                inserted += 1
                # 정상 가격이면 그룹에 추가 (다음 건 검사에 반영)
                if not record.get("outlier_flagged") and record.get("price_local"):
                    existing_prices.append(float(record["price_local"]))
            except Exception as e:
                logger.error(f"[{COUNTRY}] DB 저장 실패: {e}")
        else:
            inserted += 1

    return {"rows_inserted": inserted, "rows_updated": updated}
