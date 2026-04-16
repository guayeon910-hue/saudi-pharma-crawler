"""
pipeline_persist.py — 대시보드/전체 크롤에서 `search_one_drug` 결과를 Supabase `products`에 반영.

`targeted_search.search_one_drug`는 기본적으로 메모리만 반환하므로, 이 모듈에서 소스별로
스키마 행을 만들어 upsert한다 (기존 크롤러 `map_*_to_schema` 재사용).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# team-kit `products` — upsert 시 허용 키 (id/crawled_at은 DB 기본값)
_PRODUCT_KEYS: frozenset[str] = frozenset({
    "product_id",
    "market_segment",
    "fob_estimated_usd",
    "confidence",
    "country",
    "currency",
    "regulatory_id",
    "trade_name",
    "scientific_name",
    "strength",
    "dosage_form",
    "price_local",
    "manufacturer_or_marketing_company",
    "agent_or_supplier",
    "atc_code",
    "inn_name",
    "inn_id",
    "inn_match_type",
    "source_url",
    "source_tier",
    "source_name",
    "raw_payload",
    "outlier_flagged",
    "anomaly_reason",
    "toggle_id",
})


def _sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k in _PRODUCT_KEYS}
    # 스키마: confidence < 1.0
    try:
        c = float(out.get("confidence", 0.5))
        if c >= 1.0:
            out["confidence"] = 0.99
        elif c < 0.0:
            out["confidence"] = 0.01
    except (TypeError, ValueError):
        out["confidence"] = 0.75
    # scientific_name만 있고 inn_name이 비어 있으면 동기화 (조회 필터 호환)
    if out.get("inn_name") in (None, "") and out.get("scientific_name"):
        out["inn_name"] = out["scientific_name"]
    rp = out.get("raw_payload")
    if rp is not None and not isinstance(rp, (dict, list, type(None))):
        try:
            out["raw_payload"] = json.loads(json.dumps(rp, default=str))
        except (TypeError, ValueError):
            out["raw_payload"] = {"_str": str(rp)[:12000]}
    return out


def _upsert_one(sb: Any, row: dict[str, Any]) -> bool:
    if not row.get("product_id") or not row.get("trade_name"):
        return False
    try:
        sb.table("products").upsert(row, on_conflict="product_id").execute()
        return True
    except Exception as e:
        logger.debug("products upsert 실패 product_id=%s: %s", row.get("product_id"), e)
        return False


def persist_aggregated_search_to_supabase(sb: Any, agg: Any) -> int:
    """`AggregatedResult` 매칭을 `products`에 upsert. 저장된 행 수 반환."""
    if sb is None or agg is None or not hasattr(agg, "source_results"):
        return 0

    stored = 0
    for sr in agg.source_results:
        if sr.error or not sr.matches:
            continue
        name = sr.source_name

        try:
            if name == "sfda_api":
                for m in sr.matches:
                    row = _sanitize_row(m)
                    if not row.get("source_name"):
                        row["source_name"] = "sfda_web"
                    if _upsert_one(sb, row):
                        stored += 1

            elif name == "nahdi_web":
                from crawlers.nahdi_web import map_nahdi_to_schema

                for m in sr.matches:
                    row = map_nahdi_to_schema(m, source_url=sr.source_url)
                    row = _sanitize_row(row)
                    if _upsert_one(sb, row):
                        stored += 1

            elif name == "whites_web":
                from crawlers.whites_web import map_whites_to_schema

                for m in sr.matches:
                    row = map_whites_to_schema(m, source_url=sr.source_url)
                    row = _sanitize_row(row)
                    if _upsert_one(sb, row):
                        stored += 1
        except Exception as e:
            logger.warning("persist 소스 %s 건너뜀: %s", name, e)

    if stored:
        logger.info("Supabase products 적재: %d건", stored)
    return stored
