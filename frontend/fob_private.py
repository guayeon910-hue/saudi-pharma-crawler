from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("frontend.fob_private")

# assets/snippets 모듈(inn_normalizer, outlier_detector)을 lazy import 할 수 있도록
# sys.path 에 추가. frontend/server.py 가 이미 추가해두지만, 단독 실행 경로에서도 안전하게.
_SNIPPETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "snippets"
if _SNIPPETS_DIR.exists() and str(_SNIPPETS_DIR) not in sys.path:
    sys.path.insert(0, str(_SNIPPETS_DIR))

SAR_PER_USD = 3.75

MARGIN_TIERS: tuple[tuple[float, float, float], ...] = (
    (50.0, 0.15, 0.20),
    (200.0, 0.10, 0.15),
    (float("inf"), 0.10, 0.10),
)
RETAIL_THRESHOLD_T1 = 69.0
RETAIL_THRESHOLD_T2 = 253.0

GENERIC_CAP = {1: 0.70, 2: 0.65, 3: 0.60}
BIOSIMILAR_CAP = {1: 0.80, 2: 0.65, 3: 0.55}
INNOVATIVE_CAP = 1.00

DOSAGE_RATIO_ADJ: tuple[tuple[float, float], ...] = (
    (4.0, 0.30),
    (3.0, 0.24),
    (2.0, 0.18),
)

COMBO_PREMIUM_MAX = 0.20

VAT_RATE_MEDICINE = 0.00
CUSTOMS_RATE_MEDICINE = 0.00

PORT_FEE_RATE = 0.0015
PORT_FEE_MIN = 15.0
PORT_FEE_MAX_MEDICINE = 130.0

SABER_PCOC_PER_YEAR = 575.0
SABER_SCOC_PER_SHIP = 500.0

SFDA_REG_FEE_GENERIC = 48_000.0
SFDA_REG_FEE_INNOVATIVE = 115_000.0

DEFAULT_AGENT_COMMISSION_RANGE = (0.03, 0.10)
DEFAULT_REGULATORY_ASSUMPTIONS = {"annual_units": 10_000, "monthly_shipments": 2}
DEFAULT_HEALTH_FUNCTIONAL_BENCHMARK_SAR = 135.0
DEFAULT_HEALTH_FUNCTIONAL_BENCHMARK_SPREAD = 0.30

FREIGHT_INS_DEFAULT = {
    "solid": 3.0,
    "capsule": 3.0,
    "cream": 5.0,
    "liquid": 6.0,
    "injection": 12.0,
    "other": 5.0,
}

SCENARIO_DEFAULTS = {
    "aggressive": {
        "label": "공격적",
        "entry_rank": 1,
        "price_basis": "p75",
        "agent_commission_pct": 0.03,
        "freight_multiplier": 0.85,
    },
    "average": {
        "label": "평균",
        "entry_rank": 2,
        "price_basis": "median",
        "agent_commission_pct": 0.05,
        "freight_multiplier": 1.00,
    },
    "conservative": {
        "label": "보수적",
        "entry_rank": 3,
        "price_basis": "p25",
        "agent_commission_pct": 0.10,
        "freight_multiplier": 1.20,
    },
}

# 공공 조달(NUPCO/SFDA 벤치마크): 동일 분포 가정하되 에이전트·운임 가정을 입찰 통행에 맞춰 조정
PUBLIC_SCENARIO_DEFAULTS = {
    "aggressive": {
        "label": "공격적",
        "entry_rank": 1,
        "price_basis": "p75",
        "agent_commission_pct": 0.025,
        "freight_multiplier": 0.82,
    },
    "average": {
        "label": "평균",
        "entry_rank": 2,
        "price_basis": "median",
        "agent_commission_pct": 0.040,
        "freight_multiplier": 1.00,
    },
    "conservative": {
        "label": "보수적",
        "entry_rank": 3,
        "price_basis": "p25",
        "agent_commission_pct": 0.080,
        "freight_multiplier": 1.12,
    },
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_money(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _round_krw(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return int(round(float(value)))


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return sorted_values[lower]
    weight = pos - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def select_tier_by_retail(p_pub_sar: float) -> int:
    if p_pub_sar <= RETAIL_THRESHOLD_T1:
        return 1
    if p_pub_sar <= RETAIL_THRESHOLD_T2:
        return 2
    return 3


def _tier_to_margins(tier_index: int) -> tuple[float, float]:
    _, wholesale, pharmacy = MARGIN_TIERS[tier_index - 1]
    return wholesale, pharmacy


def reverse_retail_to_cif(p_pub_sar: float) -> tuple[float, int, tuple[float, float]]:
    tier_index = select_tier_by_retail(p_pub_sar)
    wholesale_mark, pharmacy_mark = _tier_to_margins(tier_index)
    cif = p_pub_sar / ((1.0 + wholesale_mark) * (1.0 + pharmacy_mark))
    return cif, tier_index, (wholesale_mark, pharmacy_mark)


def _generic_cap_factor(product_kind: str, entry_rank: int) -> float:
    normalized_rank = max(1, min(3, int(entry_rank)))
    kind = (product_kind or "generic").strip().lower()
    if kind == "innovative":
        return INNOVATIVE_CAP
    if kind == "biosimilar":
        return BIOSIMILAR_CAP.get(normalized_rank, BIOSIMILAR_CAP[3])
    return GENERIC_CAP.get(normalized_rank, GENERIC_CAP[3])


def apply_generic_cap(cif_sar: float, product_kind: str, entry_rank: int) -> float:
    return cif_sar * _generic_cap_factor(product_kind, entry_rank)


def dosage_adjustment_pct(ratio: float) -> float:
    for threshold, discount in DOSAGE_RATIO_ADJ:
        if ratio >= threshold:
            return discount
    return 0.0


def dosage_multiplier(ratio: float) -> float:
    if ratio <= 1.0:
        return 1.0
    discount = dosage_adjustment_pct(ratio)
    return ratio * (1.0 - discount)


def apply_dosage_adjustment(cif_sar: float, ratio: float) -> float:
    return cif_sar * dosage_multiplier(ratio)


def apply_combo_premium(cif_sar: float, factor: float) -> float:
    normalized_factor = _clamp(float(factor or 0.0), 0.0, COMBO_PREMIUM_MAX)
    return cif_sar * (1.0 + normalized_factor)


def cif_to_fob(cif_sar: float, freight_ins_sar: float, agent_commission: float) -> float:
    return (cif_sar - freight_ins_sar) * (1.0 - agent_commission)


def compute_port_fee(cif_sar: float) -> float:
    return min(max(cif_sar * PORT_FEE_RATE, PORT_FEE_MIN), PORT_FEE_MAX_MEDICINE)


def compute_regulatory_amortization(
    is_innovative: bool,
    monthly_shipments: int,
    annual_units: int,
) -> dict:
    annual_units = max(int(annual_units), 1)
    monthly_shipments = max(int(monthly_shipments), 0)
    sfda_reg = SFDA_REG_FEE_INNOVATIVE if is_innovative else SFDA_REG_FEE_GENERIC
    saber_pcoc = SABER_PCOC_PER_YEAR
    saber_scoc = SABER_SCOC_PER_SHIP * monthly_shipments * 12
    total_annual = sfda_reg + saber_pcoc + saber_scoc
    per_unit = total_annual / annual_units
    return {
        "sfda_registration_sar": sfda_reg,
        "saber_pcoc_annual_sar": saber_pcoc,
        "saber_scoc_annual_sar": saber_scoc,
        "per_unit_amortization_sar": _round_money(per_unit),
        "assumptions": {
            "annual_units": annual_units,
            "monthly_shipments": monthly_shipments,
        },
    }


def _dedupe_price_samples(raw_items: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple[str, str, float]] = set()
    for item in raw_items:
        if item.get("is_verified_price") is False:
            continue
        price = _safe_float(item.get("price"))
        if price is None or price <= 0:
            continue
        key = (
            str(item.get("trade_name") or "").strip().lower(),
            str(item.get("strength") or "").strip().lower(),
            round(price, 4),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            {
                "trade_name": str(item.get("trade_name") or "").strip(),
                "strength": str(item.get("strength") or "").strip(),
                "price": float(price),
                "source": str(item.get("source") or "").strip(),
                "source_url": str(item.get("source_url") or item.get("url") or "").strip(),
                "is_verified_price": item.get("is_verified_price"),
                "verification_status": str(item.get("verification_status") or "").strip(),
                "type": str(item.get("type") or "").strip(),
            }
        )
    return deduped


def _is_health_functional_report(report_data: dict) -> bool:
    text = " ".join(
        str(report_data.get(key) or "")
        for key in (
            "trade_name",
            "product_id",
            "inn",
            "ingredient",
            "dosage_form",
            "drug_type",
            "product_type",
            "case_type",
        )
    ).lower()
    markers = (
        "health functional",
        "functional food",
        "inner beauty",
        "nutraceutical",
        "dietary supplement",
        "supplement",
        "agatri",
        "agastache",
        "baechohyang",
        "oral sunscreen",
        "beauty from within",
    )
    return any(marker in text for marker in markers)


def _price_range_from_report_estimate(report_data: dict) -> tuple[float, float, float] | None:
    comparison = report_data.get("price_comparison") or {}
    estimated = comparison.get("estimated") if isinstance(comparison, dict) else None
    if not isinstance(estimated, dict):
        estimated = {}

    avg = (
        _safe_float(report_data.get("estimated_avg_sar"))
        or _safe_float(estimated.get("avg_sar"))
        or _safe_float(estimated.get("avg"))
    )
    if avg is None or avg <= 0:
        return None

    low = (
        _safe_float(report_data.get("estimated_min_sar"))
        or _safe_float(estimated.get("min_sar"))
        or _safe_float(estimated.get("min"))
        or avg * 0.80
    )
    high = (
        _safe_float(report_data.get("estimated_max_sar"))
        or _safe_float(estimated.get("max_sar"))
        or _safe_float(estimated.get("max"))
        or avg * 1.20
    )
    low = max(0.01, min(float(low), float(avg)))
    high = max(float(avg), float(high))
    return (low, float(avg), high)


def _health_functional_benchmark_range() -> tuple[float, float, float]:
    midpoint = (
        _safe_float(os.environ.get("AGATRI_BENCHMARK_SAR"))
        or _safe_float(os.environ.get("HEALTH_FUNCTIONAL_BENCHMARK_SAR"))
        or DEFAULT_HEALTH_FUNCTIONAL_BENCHMARK_SAR
    )
    low = _safe_float(os.environ.get("AGATRI_BENCHMARK_LOW_SAR"))
    high = _safe_float(os.environ.get("AGATRI_BENCHMARK_HIGH_SAR"))
    spread = DEFAULT_HEALTH_FUNCTIONAL_BENCHMARK_SPREAD
    if low is None or low <= 0:
        low = midpoint * (1.0 - spread)
    if high is None or high <= 0:
        high = midpoint * (1.0 + spread)
    low = max(0.01, min(float(low), float(midpoint)))
    high = max(float(midpoint), float(high))
    return (low, float(midpoint), high)


def _append_estimated_price_range(
    deduped: list[dict],
    report_data: dict,
    price_range: tuple[float, float, float],
    *,
    source: str,
    status: str,
) -> None:
    labels = ("benchmark_low", "benchmark_mid", "benchmark_high")
    trade_name = str(report_data.get("trade_name") or report_data.get("product_id") or "estimated benchmark")
    strength = str(report_data.get("strength") or "")
    for label, price in zip(labels, price_range):
        if price <= 0:
            continue
        deduped.append(
            {
                "trade_name": f"{trade_name} {label}",
                "strength": strength,
                "price": float(price),
                "source": source,
                "source_url": "",
                "is_verified_price": False,
                "verification_status": status,
                "type": "estimated_benchmark",
            }
        )


# ─────────────────────────────────────────────────────────────────────────
# 가격 출처 tier 분류 (Cloudflare 편향 투명성 — Phase 2)
#
# tier 1 = public (SFDA 등록 DB, 공공 가격 공시)
# tier 2 = procurement (NUPCO/Etimad 조달 입찰)
# tier 3 = retail (Nahdi/Dawaa/Whites/Noon 등 민간 소매)
# tier 0 = self/estimated (자기 제품 가격 or Claude 추정)
# ─────────────────────────────────────────────────────────────────────────

_SOURCE_TIER_MAP: dict[str, tuple[int, str]] = {
    # public (SFDA)
    "sfda": (1, "public"),
    "sfda_api": (1, "public"),
    "sfda_web": (1, "public"),
    "sfda_drugs_list_html": (1, "public"),
    "sfda_companies": (1, "public"),
    # procurement
    "nupco": (2, "procurement"),
    "nupco_tenders": (2, "procurement"),
    "etimad": (2, "procurement"),
    "etimad_api": (2, "procurement"),
    # retail / e-commerce
    "nahdi": (3, "retail"),
    "nahdi_web": (3, "retail"),
    "al_dawaa": (3, "retail"),
    "al_dawaa_web": (3, "retail"),
    "dawaa": (3, "retail"),
    "whites": (3, "retail"),
    "whites_web": (3, "retail"),
    "rosheta": (3, "retail"),
    "rosheta_web": (3, "retail"),
    "noon": (3, "retail"),
    "noon_saudi": (3, "retail"),
    "tamer": (3, "retail"),
    "tamer_group": (3, "retail"),
    # self / estimated
    "report_data": (0, "self"),
    "selected": (0, "self"),
    "estimated": (0, "estimated"),
    "phase1_estimated": (0, "estimated"),
    "health_functional_benchmark": (0, "estimated"),
    "same_ingredient": (0, "unknown"),
    "competitors": (0, "unknown"),
    "크롤링": (0, "unknown"),
    "동일성분": (0, "unknown"),
    "유사제형": (0, "unknown"),
}


def _classify_source(source_str: str) -> tuple[int, str]:
    """원본 source 문자열을 (tier, origin) 튜플로 분류.

    완전 일치가 없으면 부분 일치 fuzzy fallback.
    """
    key = (source_str or "").strip().lower()
    if key in _SOURCE_TIER_MAP:
        return _SOURCE_TIER_MAP[key]
    if "sfda" in key:
        return (1, "public")
    if "nupco" in key or "etimad" in key or "tender" in key or "조달" in key:
        return (2, "procurement")
    if any(tok in key for tok in ("nahdi", "dawaa", "whites", "noon", "tamer", "retail", "pharmacy", "소매")):
        return (3, "retail")
    if "report" in key or "selected" in key:
        return (0, "self")
    if "estimate" in key or "benchmark" in key or "추정" in key or "벤치마크" in key:
        return (0, "estimated")
    return (0, "unknown")


def _collect_price_pool(report_data: dict) -> list[dict]:
    raw_items: list[dict] = []

    price_sar = _safe_float(report_data.get("price_sar"))
    if price_sar and price_sar > 0:
        raw_items.append(
            {
                "trade_name": report_data.get("trade_name") or report_data.get("product_id") or "selected",
                "strength": report_data.get("strength") or "",
                "price": price_sar,
                "source": "report_data",
                "type": "selected",
            }
        )

    comparison = report_data.get("price_comparison") or {}
    for key in ("same_ingredient", "competitors"):
        for item in comparison.get(key) or []:
            raw_items.append(
                {
                    "trade_name": item.get("trade_name") or item.get("name") or "",
                    "strength": item.get("strength") or report_data.get("strength") or "",
                    "price": item.get("price") or item.get("price_sar") or item.get("retail_price"),
                    "source": item.get("source") or key,
                    "source_url": item.get("source_url") or item.get("url") or "",
                    "is_verified_price": item.get("is_verified_price"),
                    "verification_status": item.get("verification_status") or "",
                    "type": item.get("type") or key,
                }
            )

    deduped = _dedupe_price_samples(raw_items)

    # Fallback: 직접 검증 가격이 없으면 P1 추정 범위 또는 건강기능식품 벤치마크를
    # 명시적으로 "미검증 추정"으로 분리해 사용한다.
    if not deduped:
        estimated_range = _price_range_from_report_estimate(report_data)
        if estimated_range:
            _append_estimated_price_range(
                deduped,
                report_data,
                estimated_range,
                source="phase1_estimated",
                status="phase1_estimated_no_direct_source",
            )
        elif _is_health_functional_report(report_data):
            _append_estimated_price_range(
                deduped,
                report_data,
                _health_functional_benchmark_range(),
                source="health_functional_benchmark",
                status="benchmark_no_direct_source",
            )

    # ── tier/origin 메타데이터 부여 (Phase 2-1) ──
    for item in deduped:
        tier, origin = _classify_source(item.get("source", ""))
        item["tier"] = tier
        item["origin"] = origin

    return deduped


def _analyze_pool_diversity(price_pool: list[dict]) -> dict:
    """price_pool 의 출처 다양성을 분석하여 투명성 경고를 생성.

    반환 구조:
        {
          "sources": [{"source": str, "tier": int, "origin": str, "count": int}, ...],
          "tier_counts": {"public": N, "procurement": N, "retail": N, "self": N, ...},
          "warnings": [str, ...],
        }
    """
    from collections import Counter

    source_keys = Counter()
    origin_counter = Counter()
    src_to_meta: dict[str, tuple[int, str]] = {}

    for item in price_pool:
        src = (item.get("source") or "").strip() or "unknown"
        tier = int(item.get("tier", 0))
        origin = str(item.get("origin") or "unknown")
        source_keys[src] += 1
        origin_counter[origin] += 1
        src_to_meta[src] = (tier, origin)

    sources_list = [
        {
            "source": src,
            "tier": src_to_meta[src][0],
            "origin": src_to_meta[src][1],
            "count": cnt,
        }
        for src, cnt in sorted(source_keys.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    retail_sources = {
        src for src, (_t, origin) in src_to_meta.items() if origin == "retail"
    }
    public_count = origin_counter.get("public", 0)
    procurement_count = origin_counter.get("procurement", 0)
    retail_count = origin_counter.get("retail", 0)
    self_count = origin_counter.get("self", 0) + origin_counter.get("estimated", 0)

    warnings_list: list[str] = []

    if retail_count == 0 and (public_count + procurement_count) > 0:
        warnings_list.append(
            "⚠️ 소매 가격 소스 전체 누락(Cloudflare 차단 가능성) — "
            "FOB 가 실제 시장가 대비 상향 편향 우려"
        )
    elif retail_count > 0 and len(retail_sources) == 1:
        only_chain = next(iter(retail_sources))
        warnings_list.append(
            f"⚠️ 소매 표본이 단일 체인({only_chain})뿐 — 대표성 제한, "
            "다른 체인 대비 가격 편차 반영 불가"
        )

    if retail_count == 0 and procurement_count == 0 and public_count > 0 and self_count == 0:
        warnings_list.append(
            "⚠️ 공공 SFDA 단가만 존재 — 민간 마진 구조(약국 + 도매) 반영 불가"
        )

    if retail_count == 0 and public_count == 0 and procurement_count > 0:
        warnings_list.append(
            "⚠️ 공공조달 단가 only — 민간 시장 소매 분포와 다를 수 있음"
        )

    if self_count > 0 and (retail_count + public_count + procurement_count) == 0:
        warnings_list.append(
            "⚠️ 자체/추정 가격만 존재 — 외부 시장 검증 불가. 결과를 참고용으로만 사용"
        )
    if self_count > 0 and self_count == sum(origin_counter.values()):
        warnings_list.append(
            "직접 검증된 SAR 가격 샘플이 없어 추정/벤치마크 가격으로 계산했습니다."
        )

    return {
        "sources": sources_list,
        "tier_counts": {
            "public": public_count,
            "procurement": procurement_count,
            "retail": retail_count,
            "self_or_estimated": self_count,
            "unknown": origin_counter.get("unknown", 0),
        },
        "retail_chain_count": len(retail_sources),
        "warnings": warnings_list,
    }


def _scan_outliers_safe(values: list[float]) -> list[float]:
    """outlier_detector.scan_group 래퍼. 로드 실패/ n<5 등에서 조용히 빈 리스트.

    K 통계량(IQR 기반 Relative Range) 으로 반복 제거되는 가격들을 반환.
    논문 임계값(alpha=0.05) 기준, 최대 3회 반복으로 제한.
    """
    if not values or len(values) < 5:
        return []
    try:
        from outlier_detector import scan_group  # type: ignore

        removed = scan_group(list(values), alpha=0.05, max_iter=3)
        return [float(price) for (price, _result) in removed]
    except Exception as exc:
        logger.debug("outlier scan_group 건너뜀: %s", exc)
        return []


def _build_competitor_stats(price_pool: list[dict]) -> dict:
    values_all = sorted(item["price"] for item in price_pool)
    if not values_all:
        raise ValueError(
            "가격 분포 역산에 사용할 SAR 가격 샘플을 찾지 못했습니다. "
            "1공정 보고서에 동일 성분 가격·추정가가 포함되어 있는지 확인하세요."
        )

    # ── K 통계량 기반 이상치 필터 (n>=5 일 때만 동작) ──
    outliers_removed = _scan_outliers_safe(values_all)
    if outliers_removed:
        # 동일 값 다중 제거 방지 위해 각 outlier 값당 1건씩만 빼기
        remaining_to_remove = list(outliers_removed)
        filtered: list[float] = []
        for v in values_all:
            matched_idx = None
            for idx, rv in enumerate(remaining_to_remove):
                if math.isclose(v, rv, rel_tol=1e-6, abs_tol=1e-6):
                    matched_idx = idx
                    break
            if matched_idx is not None:
                remaining_to_remove.pop(matched_idx)
            else:
                filtered.append(v)
        # 전부 outlier 로 간주되면 원본 유지 (극단 안전장치)
        values = filtered if filtered else values_all
        if not filtered:
            outliers_removed = []
    else:
        values = values_all

    count = len(values)
    if count >= 3:
        p25 = _percentile(values, 0.25)
        median = _percentile(values, 0.50)
        p75 = _percentile(values, 0.75)
        warning = None
    elif count == 2:
        p25 = values[0]
        median = sum(values) / 2.0
        p75 = values[1]
        warning = "표본 3개 미만 — 시나리오 retail 기준이 제한적으로 추정되었습니다."
    else:
        p25 = median = p75 = values[0]
        warning = "표본 3개 미만 — 세 시나리오 모두 동일 retail 기준을 사용합니다."

    extra_warnings: list[str] = []
    if outliers_removed:
        formatted = ", ".join(f"{v:.2f}" for v in outliers_removed)
        extra_warnings.append(
            f"⚠️ 이상치 {len(outliers_removed)}건 제외(K 통계량 기준): [{formatted}] SAR — "
            "용량 파싱 오류 또는 통화 혼입 가능성"
        )

    origins = {str(item.get("origin") or "unknown") for item in price_pool}
    estimated_only = bool(price_pool) and origins.issubset({"estimated"})

    return {
        "count": count,
        "min": _round_money(values[0]),
        "max": _round_money(values[-1]),
        "avg": _round_money(sum(values) / count),
        "p25": _round_money(p25),
        "median": _round_money(median),
        "p75": _round_money(p75),
        "warning": warning,
        "extra_warnings": extra_warnings,
        "outliers_removed": [_round_money(v) for v in outliers_removed],
        "sample_count_before_filter": len(values_all),
        "sample_count_after_filter": count,
        "estimated_only": estimated_only,
        "price_source_label": "추정 소매 벤치마크" if estimated_only else "경쟁제품 소매가",
    }


def _infer_dosage_bucket(product: dict) -> str:
    dosage_form = str(product.get("dosage_form") or "").lower()
    trade_name = str(product.get("trade_name") or "").lower()
    blob = f"{dosage_form} {trade_name}"
    if any(token in blob for token in ("inj", "inject", "vial", "amp", "infusion")):
        return "injection"
    if any(token in blob for token in ("cream", "gel", "ointment", "topical")):
        return "cream"
    if any(token in blob for token in ("syrup", "suspension", "solution", "liquid")):
        return "liquid"
    if "capsule" in blob:
        return "capsule"
    if any(token in blob for token in ("tablet", "tab", "caplet", "powder")):
        return "solid"
    return "other"


def _heuristic_classification(product: dict) -> dict:
    inn = str(product.get("inn") or "")
    trade_name = str(product.get("trade_name") or "")
    dosage_form = str(product.get("dosage_form") or "")
    blob = f"{trade_name} {inn} {dosage_form}".lower()
    is_combination = "+" in inn or " / " in inn or "combo" in blob
    is_extended_release = bool(re.search(r"\b(cr|sr|xr|er|mr|xl)\b", blob))
    dosage_bucket = _infer_dosage_bucket(product)
    return {
        "product_kind": "generic",
        "is_combination": is_combination,
        "is_extended_release": is_extended_release,
        "premium_factor": 0.0,
        "dosage_ratio": 1.0,
        "freight_base_sar_per_unit": FREIGHT_INS_DEFAULT[dosage_bucket],
        "rationale": "Claude 미설정 또는 분류 실패로 기본 규칙을 사용했습니다.",
        "warnings": [],
    }


# ═════════════════════════════════════════════════════════════════════════
# 하드룰 분류기 (LLM 환각 방어 1차선)
#
# 설계 원칙:
#   1. 브랜드 → INN 매핑(inn_normalizer.BRAND_TO_INN) → INN 확보
#   2. INN → ATC 코드(inn_normalizer.normalize_to_inn) → 치료계열 확보
#   3. ATC 접두어가 biologic 계열이면 biosimilar
#   4. INN 이 "사우디 상위 제네릭" 목록에 있으면 generic
#   5. 위 규칙 모두 불발이면 None 을 반환해 LLM 에게 양보
# ═════════════════════════════════════════════════════════════════════════

_inn_normalizer_instance: Any = None
_inn_normalizer_failed: bool = False
_brand_to_inn_cache: dict[str, str] | None = None

# ATC 접두어 기반 biologic/biosimilar 판정 규칙
# (WHO ATC 분류 중 재조합단백·단클론항체·생물학적 제제 계열)
_BIOLOGIC_ATC_PREFIXES: tuple[str, ...] = (
    "A10A",   # 인슐린 전체 (glargine, lispro, aspart ...)
    "B01AB",  # 헤파린·저분자량 헤파린 (enoxaparin ...)
    "H01",    # 뇌하수체/시상하부 호르몬 (recombinant)
    "J06B",   # 면역글로불린
    "L01X",   # 항암 단클론항체·표적치료제 (trastuzumab, bevacizumab ...)
    "L03A",   # 면역자극제 (interferon ...)
    "L04AB",  # TNF-α 억제제 (adalimumab, infliximab ...)
    "L04AC",  # 인터루킨 억제제
)

# 사우디 시장에서 동일 INN 으로 다수 제약사가 판매 중인(= 사실상 제네릭) 성분.
# 이 목록에 있으면 innovative cap 대신 generic cap 적용.
_KNOWN_WIDELY_GENERIC_INNS: frozenset[str] = frozenset({
    "paracetamol", "acetaminophen", "ibuprofen", "amoxicillin",
    "amoxicillin trihydrate", "metformin", "metformin hydrochloride",
    "atorvastatin", "atorvastatin calcium", "omeprazole", "esomeprazole",
    "amlodipine", "amlodipine besylate", "losartan", "losartan potassium",
    "clopidogrel", "acetylsalicylic acid", "ciprofloxacin", "azithromycin",
    "doxycycline", "prednisolone", "prednisone", "dexamethasone",
    "levothyroxine", "warfarin", "pantoprazole", "lansoprazole",
    "rosuvastatin", "simvastatin", "lisinopril", "enalapril", "ramipril",
    "valsartan", "telmisartan", "bisoprolol", "carvedilol", "furosemide",
    "hydrochlorothiazide", "spironolactone", "gabapentin", "pregabalin",
    "tramadol", "diclofenac", "celecoxib", "montelukast", "salbutamol",
    "fluticasone",
})


def _load_inn_assets() -> tuple[Any, dict[str, str]]:
    """inn_normalizer 싱글턴과 BRAND_TO_INN 매핑을 지연 로드."""
    global _inn_normalizer_instance, _inn_normalizer_failed, _brand_to_inn_cache
    if _inn_normalizer_failed:
        return None, _brand_to_inn_cache or {}
    if _inn_normalizer_instance is not None:
        return _inn_normalizer_instance, _brand_to_inn_cache or {}
    try:
        from inn_normalizer import INNNormalizer, BRAND_TO_INN  # type: ignore

        normalizer = INNNormalizer()
        normalizer.load_reference()
        _inn_normalizer_instance = normalizer
        _brand_to_inn_cache = dict(BRAND_TO_INN)
        return normalizer, _brand_to_inn_cache
    except Exception as exc:
        logger.warning("INN 정규화기 로드 실패 — 하드룰 분류 건너뜀: %s", exc)
        _inn_normalizer_failed = True
        _brand_to_inn_cache = {}
        return None, _brand_to_inn_cache


def _is_biologic_by_atc(atc_code: Optional[str]) -> bool:
    """ATC 코드 접두어로 biologic 여부 판정."""
    if not atc_code:
        return False
    code = str(atc_code).upper().strip()
    return any(code.startswith(prefix) for prefix in _BIOLOGIC_ATC_PREFIXES)


def _hard_rule_classify(product: dict) -> Optional[dict]:
    """브랜드/INN/ATC 하드룰로 product_kind 결정.

    반환:
        {
          "product_kind": "innovative" | "generic" | "biosimilar",
          "confidence": 0.0~1.0,
          "inn_name": str | None,
          "atc_code": str | None,
          "rule_applied": str,
          "rationale": str,
        }
        또는 None (규칙 불발)
    """
    inn_input = str(product.get("inn") or "").strip()
    trade_name = str(product.get("trade_name") or "").strip()

    normalizer, brand_map = _load_inn_assets()

    inn_resolved: Optional[str] = None
    atc_code: Optional[str] = None
    chain: list[str] = []

    # 1. 브랜드 매핑 (trade_name 첫 단어)
    if trade_name and brand_map:
        brand_key = trade_name.lower().split()[0] if trade_name.strip() else ""
        if brand_key in brand_map:
            inn_resolved = brand_map[brand_key]
            chain.append(f"brand({brand_key}→{inn_resolved})")

    # 2. INNNormalizer 로 INN + ATC 확보 (INN 필드 우선, fallback trade_name)
    if normalizer is not None:
        source = inn_input or trade_name
        if source:
            try:
                result = normalizer.normalize(source)
                if getattr(result, "success", False):
                    if not inn_resolved and getattr(result, "inn_name", None):
                        inn_resolved = result.inn_name
                    candidate_atc = getattr(result, "inn_id", None)
                    if candidate_atc:
                        atc_code = str(candidate_atc)
                        chain.append(f"inn({source}→{inn_resolved}/{atc_code})")
            except Exception as exc:
                logger.debug("normalize() 실패: %s (%s)", source, exc)

    if not inn_resolved and not atc_code:
        return None

    inn_lower = (inn_resolved or "").lower().strip()

    # 3. biologic → biosimilar (ATC 접두어 기반)
    if _is_biologic_by_atc(atc_code):
        return {
            "product_kind": "biosimilar",
            "confidence": 0.85,
            "inn_name": inn_resolved,
            "atc_code": atc_code,
            "rule_applied": "atc_biologic",
            "rationale": (
                f"ATC={atc_code} (생물학적 제제 계열) → biosimilar cap 적용. "
                f"해결 경로: {' → '.join(chain) if chain else 'direct'}"
            ),
        }

    # 4. 광범위 제네릭 목록에 포함
    if inn_lower and inn_lower in _KNOWN_WIDELY_GENERIC_INNS:
        return {
            "product_kind": "generic",
            "confidence": 0.90,
            "inn_name": inn_resolved,
            "atc_code": atc_code,
            "rule_applied": "widely_generic",
            "rationale": (
                f"INN={inn_resolved} 은 사우디에서 복수 제약사가 생산 중인 제네릭 → generic cap. "
                f"해결 경로: {' → '.join(chain) if chain else 'direct'}"
            ),
        }

    # 5. ATC 만 확보됐고 biologic 도 widely-generic 도 아님 — 중간 신뢰도 generic
    if atc_code:
        return {
            "product_kind": "generic",
            "confidence": 0.72,
            "inn_name": inn_resolved,
            "atc_code": atc_code,
            "rule_applied": "inn_match",
            "rationale": (
                f"INN/ATC 매칭({inn_resolved}/{atc_code}) 확인되나 광범위 제네릭 목록 외. "
                f"보수적으로 generic 분류."
            ),
        }

    # 6. INN 만 알고 ATC 없음 — 하드룰 판정 보류
    return None


def _classify_private_context(product: dict, price_pool: list[dict], llm: Any | None) -> dict:
    classification = _heuristic_classification(product)
    warnings: list[str] = []

    # ── Step 1: 하드룰 선판정 (LLM 환각 방어 1차선) ──
    hard = _hard_rule_classify(product)
    if hard is not None:
        classification["product_kind"] = hard["product_kind"]
        classification["rationale"] = hard["rationale"]
        classification["hard_rule"] = {
            "rule_applied": hard["rule_applied"],
            "inn_name": hard.get("inn_name"),
            "atc_code": hard.get("atc_code"),
            "confidence": hard["confidence"],
        }

    # ── Step 2: LLM 이 없으면 하드룰(있으면) 또는 휴리스틱으로 종료 ──
    if llm is None:
        if hard is None:
            warnings.append("Claude 미설정 — 하드룰도 불발, 기본 분류 규칙을 사용했습니다.")
        else:
            warnings.append(
                f"Claude 미설정 — 하드룰({hard['rule_applied']})로만 분류: {hard['product_kind']}."
            )
        classification["warnings"] = warnings
        return classification

    # ── Step 3: LLM 보조 — 세부 파라미터 산출 + 하드룰 교차검증 ──
    # 프롬프트 injection 방어: 모든 사용자 입력을 json.dumps 로 감싸 braces/따옴표 escape
    safe_trade = json.dumps(product.get("trade_name") or "", ensure_ascii=False)
    safe_inn = json.dumps(product.get("inn") or "", ensure_ascii=False)
    safe_form = json.dumps(product.get("dosage_form") or "", ensure_ascii=False)
    safe_strength = json.dumps(product.get("strength") or "", ensure_ascii=False)
    safe_hs = json.dumps(product.get("hs_code") or "", ensure_ascii=False)
    safe_samples = json.dumps(
        [round(x["price"], 2) for x in price_pool[:12]], ensure_ascii=False
    )

    hard_rule_hint = ""
    if hard is not None:
        hard_rule_hint = (
            "\n[하드룰 사전 판정] "
            f"product_kind={hard['product_kind']} "
            f"(신뢰도 {hard['confidence']:.2f}, 규칙={hard['rule_applied']}, "
            f"INN={hard.get('inn_name') or 'n/a'}, ATC={hard.get('atc_code') or 'n/a'}). "
            "이 판정을 존중하되 dosage_ratio / premium_factor / freight 를 산출하세요. "
            "판정 변경 사유가 있으면 rationale 에 명시하세요."
        )

    prompt = (
        "당신은 SFDA 민간 의약품 가격 규칙 전문가입니다.\n"
        "다음 정보를 바탕으로 민간 시장 FOB 역산 파이프라인에서 사용할 분류값만 JSON으로 반환하세요.\n\n"
        f"품목명: {safe_trade}\n"
        f"INN: {safe_inn}\n"
        f"제형: {safe_form}\n"
        f"함량: {safe_strength}\n"
        f"HS 코드: {safe_hs}\n"
        f"가격 샘플(SAR): {safe_samples}"
        f"{hard_rule_hint}\n\n"
        "응답 JSON 스키마:\n"
        "{\n"
        '  "product_kind": "innovative" | "generic" | "biosimilar",\n'
        '  "is_combination": true | false,\n'
        '  "is_extended_release": true | false,\n'
        '  "premium_factor": 0.0,\n'
        '  "dosage_ratio": 1.0,\n'
        '  "freight_base_sar_per_unit": 0.0,\n'
        '  "confidence": 0.0,\n'
        '  "rationale": "한국어 2문장 이내"\n'
        "}\n\n"
        "원칙:\n"
        "- 분류 확신 없으면 generic 대신 innovative 로 (FOB 과소산출 편향 방지)\n"
        "- 조합제/서방형이 명확하지 않으면 false\n"
        "- premium_factor 는 0.0~0.20\n"
        "- dosage_ratio 는 1.0 이상\n"
        "- freight_base_sar_per_unit 는 고형제 2~5, 액상 4~8, 주사제 8~15 범위 권장\n"
        "- confidence 는 0.0~1.0 — 분류 자체에 대한 확신도\n"
        "JSON만 반환하세요."
    )

    try:
        from llm_client import MODEL_HAIKU

        response = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=800)
        parsed = response.parse_json()

        llm_kind = str(parsed.get("product_kind") or classification["product_kind"]).strip().lower()
        if llm_kind not in {"innovative", "generic", "biosimilar"}:
            llm_kind = classification["product_kind"]

        try:
            llm_confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            llm_confidence = 0.0
        llm_confidence = _clamp(llm_confidence, 0.0, 1.0)

        # ── 하드룰 vs LLM 충돌 조정 ──
        if hard is not None and llm_kind != hard["product_kind"]:
            # LLM 이 매우 강하고(≥0.80) 하드룰은 약할 때(<0.85)만 LLM 채택
            if llm_confidence >= 0.80 and hard["confidence"] < 0.85:
                warnings.append(
                    f"하드룰({hard['product_kind']}) ↔ LLM({llm_kind}) 충돌 — "
                    f"LLM 신뢰도 {llm_confidence:.2f}로 LLM 채택"
                )
                classification["product_kind"] = llm_kind
            else:
                warnings.append(
                    f"하드룰({hard['product_kind']}) ↔ LLM({llm_kind}) 충돌 — "
                    f"하드룰 유지 (LLM 신뢰도 {llm_confidence:.2f} 불충분)"
                )
                # product_kind 는 하드룰 값 그대로 둠
        elif hard is None:
            classification["product_kind"] = llm_kind

        # ── 저신뢰도 LLM → manual review + 보수적 fallback ──
        if llm_confidence < 0.70:
            warnings.append(
                f"manual_review_required: LLM 분류 신뢰도 {llm_confidence:.2f} < 0.70"
            )
            if hard is None:
                # 하드룰도 없고 LLM도 모호 → "generic" 기본값 대신 innovative 로 과소산출 방지
                classification["product_kind"] = "innovative"
                warnings.append(
                    "하드룰 부재 + LLM 저신뢰 — 과소산출 방지 위해 보수적 innovative 유지"
                )

        premium_factor = _clamp(float(parsed.get("premium_factor") or 0.0), 0.0, COMBO_PREMIUM_MAX)
        dosage_ratio = max(float(parsed.get("dosage_ratio") or 1.0), 1.0)
        freight_base = max(
            float(parsed.get("freight_base_sar_per_unit") or classification["freight_base_sar_per_unit"]),
            0.0,
        )

        classification.update(
            {
                "is_combination": bool(parsed.get("is_combination", classification["is_combination"])),
                "is_extended_release": bool(parsed.get("is_extended_release", classification["is_extended_release"])),
                "premium_factor": premium_factor,
                "dosage_ratio": dosage_ratio,
                "freight_base_sar_per_unit": freight_base,
                "llm_confidence": llm_confidence,
            }
        )
        # rationale: 하드룰이 있으면 하드룰 rationale 우선, LLM rationale 은 추가 설명으로 append
        llm_rationale = str(parsed.get("rationale") or "").strip()
        if hard is not None and llm_rationale:
            classification["rationale"] = f"{hard['rationale']} | LLM: {llm_rationale}"
        elif llm_rationale:
            classification["rationale"] = llm_rationale
    except Exception as exc:
        warnings.append(f"Claude 분류 실패 — 기본 규칙으로 계산했습니다. ({exc})")

    classification["warnings"] = warnings
    return classification


def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency path
        raise RuntimeError("pdfplumber가 설치되어 있지 않아 PDF 텍스트를 추출할 수 없습니다.") from exc

    texts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                texts.append(text)
    return "\n".join(texts).strip()


def _extract_fields_from_text(text: str) -> dict:
    def _match(pattern: str) -> str | None:
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if not found:
            return None
        return found.group(1).strip()

    trade_name = _match(r"(?:Trade Name|제품명|품목명)\s*[:：]\s*(.+)")
    inn = _match(r"(?:INN|Ingredient|성분|주성분)\s*[:：]\s*(.+)")
    dosage_form = _match(r"(?:Dosage Form|제형)\s*[:：]\s*(.+)")
    strength = _match(r"(?:Strength|함량|용량)\s*[:：]\s*(.+)")
    hs_code = _match(r"(?:HS(?:\s*Code)?|HS 코드)\s*[:：]?\s*([0-9]{4}(?:\.[0-9]+)?)")

    price_values: list[dict] = []
    for idx, match in enumerate(re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(?:SAR|리얄)", text, flags=re.IGNORECASE), start=1):
        price_values.append(
            {
                "trade_name": trade_name or f"sample-{idx}",
                "strength": strength or "",
                "price": float(match.group(1)),
                "source": "pdf_text",
                "type": "pdf_text",
            }
        )

    if not price_values:
        raise ValueError("업로드한 PDF에서 SAR 가격 정보를 찾지 못했습니다.")

    return {
        "trade_name": trade_name or "PDF 입력 품목",
        "inn": inn or "",
        "dosage_form": dosage_form or "",
        "strength": strength or "",
        "hs_code": hs_code,
        "price_sar": price_values[0]["price"],
        "price_comparison": {
            "same_ingredient": price_values,
            "competitors": [],
        },
    }


def _pdf_report_needs_enrichment(report: dict) -> bool:
    trade_name = str(report.get("trade_name") or "").strip()
    return any(
        not str(report.get(field) or "").strip()
        for field in ("inn", "dosage_form", "strength")
    ) or trade_name in {"", "PDF 입력 품목"}


def _enrich_pdf_report_from_text(text: str, report: dict, llm: Any | None) -> tuple[dict, str | None]:
    if llm is None:
        return report, "PDF 메타데이터가 일부 비어 있지만 Claude를 사용할 수 없어 추출 텍스트 기준으로 계산했습니다."

    prompt = f"""당신은 사우디 의약품 시장 보고서를 구조화하는 분석가입니다.
다음 PDF 추출 텍스트에서 민간 시장 FOB 역산에 필요한 메타데이터를 JSON으로만 정리하세요.

추출 텍스트:
{text[:6000]}

현재 추출값:
{{
  "trade_name": {report.get("trade_name")!r},
  "inn": {report.get("inn")!r},
  "dosage_form": {report.get("dosage_form")!r},
  "strength": {report.get("strength")!r},
  "hs_code": {report.get("hs_code")!r}
}}

응답 JSON 스키마:
{{
  "trade_name": "string or null",
  "inn": "string or null",
  "dosage_form": "string or null",
  "strength": "string or null",
  "hs_code": "string or null",
  "rationale": "한국어 1문장"
}}

원칙:
- 확실하지 않으면 null
- 기존 추출값이 더 구체적이면 그대로 둔다
- JSON 외 텍스트는 쓰지 않는다
"""

    try:
        from llm_client import MODEL_HAIKU

        response = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=800)
        parsed = response.parse_json()
        enriched = dict(report)
        for field in ("trade_name", "inn", "dosage_form", "strength", "hs_code"):
            current = str(enriched.get(field) or "").strip()
            candidate = str(parsed.get(field) or "").strip()
            if not current or current == "PDF 입력 품목":
                enriched[field] = candidate or enriched.get(field)
        rationale = str(parsed.get("rationale") or "").strip()
        note = "PDF 추출 텍스트만으로 부족한 항목을 Claude로 보강했습니다."
        if rationale:
            note = f"{note} {rationale}"
        return enriched, note
    except Exception as exc:
        return report, f"PDF 메타데이터 보강에 실패해 추출 텍스트 기준으로 계산했습니다. ({exc})"


def _normalize_report_source(report_data: dict | None, pdf_bytes: bytes | None, llm: Any | None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    if report_data:
        comparison = report_data.get("price_comparison") or {}
        estimated = comparison.get("estimated") if isinstance(comparison, dict) else None
        if not isinstance(estimated, dict):
            estimated = {}
        return {
            "trade_name": report_data.get("trade_name") or report_data.get("product_name") or report_data.get("product_id") or "Unknown",
            "inn": report_data.get("inn") or report_data.get("ingredient") or "",
            "ingredient": report_data.get("ingredient") or report_data.get("inn") or "",
            "drug_type": report_data.get("drug_type") or report_data.get("product_type") or "",
            "dosage_form": report_data.get("dosage_form") or "",
            "strength": report_data.get("strength") or "",
            "price_sar": report_data.get("price_sar"),
            "price_comparison": report_data.get("price_comparison") or {},
            "estimated_min_sar": report_data.get("estimated_min_sar") or estimated.get("min_sar"),
            "estimated_avg_sar": report_data.get("estimated_avg_sar") or estimated.get("avg_sar"),
            "estimated_max_sar": report_data.get("estimated_max_sar") or estimated.get("max_sar"),
            "hs_code": report_data.get("hs_code"),
        }, notes

    if not pdf_bytes:
        raise ValueError("2공정 FOB 역산에는 저장된 보고서(report_data) 또는 PDF 업로드가 필요합니다.")

    text = _extract_text_from_pdf_bytes(pdf_bytes)
    if not text:
        raise ValueError("업로드한 PDF에서 텍스트를 추출하지 못했습니다.")
    notes.append("PDF 업로드에서 텍스트를 추출해 가격 분포를 구성했습니다.")
    parsed_report = _extract_fields_from_text(text)
    if _pdf_report_needs_enrichment(parsed_report):
        parsed_report, enrichment_note = _enrich_pdf_report_from_text(text, parsed_report, llm)
        if enrichment_note:
            notes.append(enrichment_note)
    return parsed_report, notes


def _scenario_overrides(
    overrides: dict | None,
    scenario_defaults: dict[str, dict],
) -> dict[str, dict[str, float]]:
    raw = overrides or {}
    scenarios = raw.get("scenarios") if isinstance(raw.get("scenarios"), dict) else raw
    sanitized: dict[str, dict[str, float]] = {}
    for name in scenario_defaults:
        current = scenarios.get(name) if isinstance(scenarios, dict) else None
        if not isinstance(current, dict):
            sanitized[name] = {}
            continue
        scenario_values: dict[str, float] = {}
        agent = _safe_float(current.get("agent_commission_pct"))
        freight = _safe_float(current.get("freight_multiplier"))
        retail_override = _safe_float(current.get("retail_base"))
        if retail_override is None:
            retail_override = _safe_float(current.get("retail_sar"))
        if agent is not None:
            scenario_values["agent_commission_pct"] = _clamp(agent, 0.0, 0.20)
        if freight is not None:
            scenario_values["freight_multiplier"] = _clamp(freight, 0.50, 2.00)
        if retail_override is not None:
            scenario_values["retail_base"] = _clamp(retail_override, 0.01, 10_000_000.0)
        sanitized[name] = scenario_values
    return sanitized


def _build_scenario_configs(
    stats: dict,
    overrides: dict | None,
    scenario_defaults: dict[str, dict],
) -> dict[str, dict]:
    sanitized_overrides = _scenario_overrides(overrides, scenario_defaults)
    scenarios: dict[str, dict] = {}
    for name, defaults in scenario_defaults.items():
        basis_key = defaults["price_basis"]
        retail_base = _safe_float(stats.get(basis_key)) or _safe_float(stats.get("avg")) or _safe_float(stats.get("median"))
        scenario = {
            **defaults,
            "retail_base": float(retail_base),
        }
        scenario.update(sanitized_overrides.get(name) or {})
        scenarios[name] = scenario
    return scenarios


def _make_step(label: str, value_sar: Optional[float]) -> dict:
    return {"label": label, "value_sar": _round_money(value_sar)}


def _run_market_fob_pipeline(
    *,
    report_data: dict | None,
    pdf_bytes: bytes | None,
    overrides: dict | None,
    exchange_rates: dict | None,
    llm: Any | None,
    scenario_defaults: dict[str, dict],
    market_type: str,
) -> dict:
    product, source_notes = _normalize_report_source(report_data, pdf_bytes, llm)
    price_pool = _collect_price_pool(product)
    competitor_stats = _build_competitor_stats(price_pool)
    classification = _classify_private_context(product, price_pool, llm)
    scenarios = _build_scenario_configs(competitor_stats, overrides, scenario_defaults)

    # ── Phase 2: 출처 다양성 분석 ──
    diversity = _analyze_pool_diversity(price_pool)

    notes = list(source_notes)
    is_health_functional = _is_health_functional_report(product)
    if market_type == "public":
        notes.append(
            "공공 시장 역산은 NUPCO/SFDA 등 참고 가격 분포와 조달 입찰 통행을 모델링한 벤치마크입니다. "
            "실제 입찰가·계약 조건과 다를 수 있습니다."
        )
    if is_health_functional:
        notes.append(
            "Agatri/건강기능식품 원료는 의약품 HS 3004·약가 제도와 다를 수 있어 VAT, 관세, SFDA 식품/보충제 요건을 별도 확인해야 합니다."
        )
    else:
        notes.append("HS 3004 적격 의약품은 VAT 0%·관세 0%를 기본 가정으로 사용합니다.")
        hs_code = str(product.get("hs_code") or "").strip()
        if not hs_code or "3004" not in hs_code:
            notes.append("HS 코드가 3004로 확인되지 않았습니다. VAT 0%·관세 0% 가정이 맞는지 재확인하세요.")

    warnings = list(classification.get("warnings") or [])
    if competitor_stats.get("warning"):
        warnings.append(competitor_stats["warning"])
    for extra in competitor_stats.get("extra_warnings") or []:
        if extra:
            warnings.append(extra)
    for dw in diversity.get("warnings") or []:
        if dw:
            warnings.append(dw)
    classification["warnings"] = warnings

    rates = exchange_rates or {}
    sar_usd = _safe_float(rates.get("sar_usd"))
    usd_krw = _safe_float(rates.get("usd_krw"))
    if sar_usd is None or sar_usd <= 0:
        sar_usd = 1.0 / SAR_PER_USD
    if usd_krw is None or usd_krw <= 0:
        usd_krw = 1472.0

    regulatory_cost = compute_regulatory_amortization(
        is_innovative=classification["product_kind"] == "innovative",
        monthly_shipments=DEFAULT_REGULATORY_ASSUMPTIONS["monthly_shipments"],
        annual_units=DEFAULT_REGULATORY_ASSUMPTIONS["annual_units"],
    )

    if competitor_stats.get("estimated_only"):
        ref_price_label = competitor_stats.get("price_source_label") or "추정 소매 벤치마크"
        notes.append(
            "직접 검증된 SAR 판매가가 없어 1공정 추정가 또는 건강기능식품 벤치마크 가격대로 역산했습니다. "
            "이 값은 실제 Agatri 판매가가 아니라 제안가 검토용 기준입니다."
        )
    else:
        ref_price_label = (
            "공공 조달 참고 가격대" if market_type == "public" else "경쟁사 소매가"
        )

    response_scenarios: dict[str, dict] = {}
    for name, config in scenarios.items():
        retail_sar = float(config["retail_base"])
        cif_original, tier_index, margins = reverse_retail_to_cif(retail_sar)
        wholesale_mark, pharmacy_mark = margins
        warehouse_price = retail_sar / (1.0 + pharmacy_mark)

        cap_factor = _generic_cap_factor(classification["product_kind"], config["entry_rank"])
        cif_after_cap = apply_generic_cap(cif_original, classification["product_kind"], config["entry_rank"])
        dosage_ratio = max(float(classification["dosage_ratio"]), 1.0)
        dosage_adj_pct = dosage_adjustment_pct(dosage_ratio)
        cif_after_dosage = apply_dosage_adjustment(cif_after_cap, dosage_ratio)
        premium_factor = float(classification["premium_factor"] or 0.0)
        cif_final = apply_combo_premium(cif_after_dosage, premium_factor)

        freight_sar = float(classification["freight_base_sar_per_unit"]) * float(config["freight_multiplier"])
        agent_commission = float(config["agent_commission_pct"])
        before_commission = cif_final - freight_sar
        fob_sar = cif_to_fob(cif_final, freight_sar, agent_commission)
        port_fee_sar = compute_port_fee(cif_final)

        steps = [
            _make_step(f"{ref_price_label} 기준 ({config['price_basis']})", retail_sar),
            _make_step(f"약국 마진 제거 (/{1.0 + pharmacy_mark:.2f})", warehouse_price),
            _make_step(f"도매 마진 제거 (/{1.0 + wholesale_mark:.2f})", cif_original),
            _make_step(f"{classification['product_kind']} 상한 적용 (×{cap_factor:.2f})", cif_after_cap),
            _make_step(f"용량 조정 반영 (ratio {dosage_ratio:.2f})", cif_after_dosage),
            _make_step(f"복합제/개량신약 프리미엄 (+{premium_factor * 100:.1f}%)", cif_final),
            _make_step(f"운임·보험 차감 (-{freight_sar:.2f} SAR)", before_commission),
            _make_step(f"에이전트 커미션 차감 (-{agent_commission * 100:.1f}%)", fob_sar),
        ]

        scenario_payload = {
            "label": config["label"],
            "entry_rank": config["entry_rank"],
            "retail_basis": config["price_basis"],
            "retail_sar": _round_money(retail_sar),
            "tier": tier_index,
            "margins": {
                "wholesale": wholesale_mark,
                "pharmacy": pharmacy_mark,
            },
            "cif_original_sar": _round_money(cif_original),
            "cap_factor": cap_factor,
            "cif_after_cap_sar": _round_money(cif_after_cap),
            "dosage_ratio": dosage_ratio,
            "dosage_adjustment_pct": dosage_adj_pct,
            "cif_after_dosage_sar": _round_money(cif_after_dosage),
            "combo_premium_pct": premium_factor,
            "cif_final_sar": _round_money(cif_final),
            "freight_multiplier": float(config["freight_multiplier"]),
            "freight_insurance_sar": _round_money(freight_sar),
            "agent_commission_pct": agent_commission,
            "port_fee_sar": _round_money(port_fee_sar),
            "steps": steps,
        }

        if fob_sar < 0:
            scenario_payload.update(
                {
                    "fob_sar": None,
                    "fob_usd": None,
                    "fob_krw": None,
                    "error": "역산 결과가 음수입니다. 경쟁사 가격 또는 운임 가정을 재확인하세요.",
                }
            )
        else:
            fob_usd = fob_sar * sar_usd
            fob_krw = fob_usd * usd_krw
            scenario_payload.update(
                {
                    "fob_sar": _round_money(fob_sar),
                    "fob_usd": _round_money(fob_usd),
                    "fob_krw": _round_krw(fob_krw),
                    "error": None,
                }
            )

        response_scenarios[name] = scenario_payload

    mt = "public" if market_type == "public" else "private"

    return {
        "ok": True,
        "market_type": mt,
        "product": {
            "trade_name": product["trade_name"],
            "inn": product["inn"],
            "dosage_form": product["dosage_form"],
            "strength": product["strength"],
            "hs_code": product.get("hs_code"),
        },
        "classification": {
            "product_kind": classification["product_kind"],
            "is_combination": classification["is_combination"],
            "is_extended_release": classification["is_extended_release"],
            "premium_factor": _round_money(classification["premium_factor"], 4),
            "dosage_ratio": _round_money(classification["dosage_ratio"], 4),
            "freight_base_sar_per_unit": _round_money(classification["freight_base_sar_per_unit"]),
            "rationale": classification["rationale"],
            "warnings": warnings,
            "hard_rule": classification.get("hard_rule"),
            "llm_confidence": classification.get("llm_confidence"),
        },
        "competitor_stats": competitor_stats,
        "price_pool_sources": diversity.get("sources", []),
        "price_pool_tier_counts": diversity.get("tier_counts", {}),
        "price_pool_retail_chains": diversity.get("retail_chain_count", 0),
        "diversity_warnings": diversity.get("warnings", []),
        "exchange_rates": {
            "sar_krw": _round_money((usd_krw / (1.0 / sar_usd)) if sar_usd else None),
            "usd_krw": _round_money(usd_krw),
            "sar_usd": round(sar_usd, 4),
            "source": rates.get("source") or "fallback",
        },
        "scenarios": response_scenarios,
        "regulatory_cost": regulatory_cost,
        "notes": notes,
    }


def run_private_pipeline(
    *,
    report_data: dict | None,
    pdf_bytes: bytes | None,
    overrides: dict | None,
    exchange_rates: dict | None,
    llm: Any | None,
) -> dict:
    return _run_market_fob_pipeline(
        report_data=report_data,
        pdf_bytes=pdf_bytes,
        overrides=overrides,
        exchange_rates=exchange_rates,
        llm=llm,
        scenario_defaults=SCENARIO_DEFAULTS,
        market_type="private",
    )


def run_public_pipeline(
    *,
    report_data: dict | None,
    pdf_bytes: bytes | None,
    overrides: dict | None,
    exchange_rates: dict | None,
    llm: Any | None,
) -> dict:
    return _run_market_fob_pipeline(
        report_data=report_data,
        pdf_bytes=pdf_bytes,
        overrides=overrides,
        exchange_rates=exchange_rates,
        llm=llm,
        scenario_defaults=PUBLIC_SCENARIO_DEFAULTS,
        market_type="public",
    )
