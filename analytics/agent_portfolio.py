"""agent_portfolio.py — 에이전트 포트폴리오 집계 + White-Space(빈틈) 분석.

Phase 3 (바이어 인텔리전스 엔진):
    특정 치료군(ATC level3)에서 강한 유통 포트폴리오를 보유하지만 특정 INN 은
    취급하지 않는 에이전트를 정량 추출 → 영업 타겟 리스트.

데이터 흐름:
    products(SFDA)    : agent_or_supplier, atc_code, inn_name, trade_name
    companies(SFDA)   : agent_name, drug_type, production_line
      ↓  _normalize_agent_name (whitespace/case/punct 정규화)
      ↓  rapidfuzz fuzzy-match (threshold 0.85) — 동일 에이전트의 철자 편차 병합
      ↓  agent × atc_level3 집계
      ↓  find_white_space(target_inn, target_atc_level3)
      ↓  {missing_ingredient: True, portfolio_strength: N}

재활용:
    assets/snippets/inn_normalizer.py : 타겟 INN → ATC 변환
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger("analytics.agent_portfolio")


# ─── 상수 ────────────────────────────────────────────────

# ATC level3 = 처음 4글자 (예: "A10BK01" → "A10B")
_ATC_LEVEL3_LEN = 4

# fuzzy match 기본 threshold (0.0~1.0)
DEFAULT_FUZZY_THRESHOLD = 0.85

# 에이전트 이름에서 제거할 법인 접미사 (정규화 단계)
_AGENT_SUFFIX_RE = re.compile(
    r"\b(co|company|companies|corp|corporation|inc|incorporated|ltd|limited|llc|plc|gmbh|sa|sar[l]?|kscc?|ksa|saudi\s+arabia|saudi)\b\.?",
    re.IGNORECASE,
)
# 공백·하이픈·쉼표·괄호를 단일 공백으로
_AGENT_WS_RE = re.compile(r"[\s,\-\u00a0/&\(\)]+")


# ─── 데이터 모델 ────────────────────────────────────────

@dataclass
class AgentPortfolioEntry:
    """에이전트 1명의 포트폴리오 집계."""
    normalized_name: str
    display_name: str                          # 대표 원본 이름 (가장 빈도 높은 철자)
    total_products: int = 0
    atc_level3_counts: dict[str, int] = field(default_factory=dict)   # {"A10B": 12, ...}
    inn_list: set[str] = field(default_factory=set)                    # 취급 INN 집합
    trade_names: list[str] = field(default_factory=list)               # 대표 trade_name 목록
    source_tiers: set[int] = field(default_factory=set)                # {1, 2, 3}
    raw_variants: set[str] = field(default_factory=set)                # 이 normalized 에 매칭된 원본 철자들

    def to_dict(self) -> dict:
        return {
            "normalized_name": self.normalized_name,
            "display_name": self.display_name,
            "total_products": self.total_products,
            "atc_level3_counts": dict(self.atc_level3_counts),
            "inn_list": sorted(self.inn_list),
            "trade_names": self.trade_names[:10],
            "source_tiers": sorted(self.source_tiers),
            "raw_variants": sorted(self.raw_variants),
        }


@dataclass
class WhiteSpaceCandidate:
    """빈틈 분석 결과 1건."""
    agent_name: str
    display_name: str
    atc_level3: str
    product_count_in_atc: int                 # 해당 치료군 취급 품목 수
    total_products: int
    portfolio_strength: float                 # 0~100 점수 (log + tier 가중)
    missing_ingredient: bool                  # 타겟 INN 미취급 여부
    sample_trade_names: list[str]
    sales_pitch: str                          # 영업용 한 문장 자동 생성

    def to_dict(self) -> dict:
        return {
            "agent_name": self.display_name,
            "normalized_name": self.agent_name,
            "atc_level3": self.atc_level3,
            "product_count_in_atc": self.product_count_in_atc,
            "total_products": self.total_products,
            "portfolio_strength": round(self.portfolio_strength, 1),
            "missing_ingredient": self.missing_ingredient,
            "sample_trade_names": self.sample_trade_names[:5],
            "sales_pitch": self.sales_pitch,
        }


# ─── 정규화 유틸 ────────────────────────────────────────

def _normalize_agent_name(raw: Optional[str]) -> str:
    """에이전트 이름 정규화: 소문자화·법인 접미사 제거·공백 축약.

    예: "Tabuk Pharmaceuticals Co., Ltd." → "tabuk pharmaceuticals"
         "SPIMACO  ADDWAEIH "              → "spimaco addwaeih"
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # 아랍어 제거(숫자/라틴 없을 경우만) — 여기서는 단순 유지, 매칭 단계에서 fuzzy 로 흡수
    s = _AGENT_SUFFIX_RE.sub(" ", s)
    s = _AGENT_WS_RE.sub(" ", s)
    s = s.strip()
    # 중복 공백 최종 제거
    s = re.sub(r"\s+", " ", s)
    return s


def _atc_level3(atc_code: Optional[str]) -> Optional[str]:
    """ATC 코드의 level3 (앞 4글자) 추출."""
    if not atc_code:
        return None
    s = str(atc_code).strip().upper()
    if len(s) < _ATC_LEVEL3_LEN:
        return None
    return s[:_ATC_LEVEL3_LEN]


# ─── fuzzy 매칭 ──────────────────────────────────────────

def _get_rapidfuzz():
    """rapidfuzz 지연 import. 없으면 None 반환."""
    try:
        from rapidfuzz import fuzz, process  # type: ignore
        return fuzz, process
    except ImportError:
        return None, None


def _fuzzy_merge_keys(
    keys: Iterable[str],
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> dict[str, str]:
    """유사한 정규화 이름들을 하나의 canonical 키로 병합.

    반환: {variant_key: canonical_key}
    rapidfuzz 가 없으면 identity mapping.
    """
    fuzz, process = _get_rapidfuzz()
    items = [k for k in keys if k]
    mapping: dict[str, str] = {}
    if not items:
        return mapping
    if fuzz is None or process is None:
        logger.debug("rapidfuzz 부재 — fuzzy 병합 생략(identity)")
        for k in items:
            mapping[k] = k
        return mapping

    # 처리 순서: 길이 내림차순 (긴 이름이 canonical이 되도록)
    # scorer: WRatio — token/partial/ratio 를 가중 평균한 균형잡힌 매칭
    sorted_items = sorted(items, key=lambda x: -len(x))
    canonicals: list[str] = []
    for k in sorted_items:
        matched_canonical = None
        if canonicals:
            best = process.extractOne(
                k, canonicals, scorer=fuzz.WRatio
            )
            if best and best[1] >= threshold * 100:
                matched_canonical = best[0]
        if matched_canonical:
            mapping[k] = matched_canonical
        else:
            mapping[k] = k
            canonicals.append(k)
    return mapping


# ─── 포트폴리오 집계 ─────────────────────────────────────

def build_agent_portfolio(
    products: list[dict],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[dict[str, AgentPortfolioEntry], list[str]]:
    """products 목록에서 에이전트 × ATC level3 포트폴리오 집계.

    Args:
        products: SFDA products 테이블 dict 목록.
                  필수 키: agent_or_supplier, atc_code, inn_name, trade_name
                  선택 키: source_tier
        fuzzy_threshold: 에이전트 이름 fuzzy 병합 기준 (0~1).

    Returns:
        (portfolio, unmatched_agents)
        portfolio: {normalized_agent_name: AgentPortfolioEntry}
        unmatched_agents: agent_or_supplier 가 공란이거나 정규화 실패한 product id 목록
    """
    # 1단계: 에이전트 이름 후보 수집
    raw_to_norm: dict[str, str] = {}
    unmatched: list[str] = []

    for p in products:
        raw = str(p.get("agent_or_supplier") or "").strip()
        if not raw:
            pid = str(p.get("product_id") or p.get("trade_name") or "?")
            unmatched.append(pid)
            continue
        norm = _normalize_agent_name(raw)
        if not norm:
            unmatched.append(str(p.get("product_id") or p.get("trade_name") or "?"))
            continue
        raw_to_norm[raw] = norm

    # 2단계: fuzzy 병합 (Tabuk Pharma ↔ Tabuk Pharmaceuticals 등)
    unique_norms = set(raw_to_norm.values())
    canonical_map = _fuzzy_merge_keys(unique_norms, threshold=fuzzy_threshold)

    # 3단계: 에이전트별 집계
    portfolio: dict[str, AgentPortfolioEntry] = {}
    display_name_votes: dict[str, dict[str, int]] = {}

    for p in products:
        raw = str(p.get("agent_or_supplier") or "").strip()
        if not raw:
            continue
        norm = _normalize_agent_name(raw)
        if not norm:
            continue
        canonical = canonical_map.get(norm, norm)

        entry = portfolio.get(canonical)
        if entry is None:
            entry = AgentPortfolioEntry(
                normalized_name=canonical,
                display_name=raw,
            )
            portfolio[canonical] = entry
            display_name_votes[canonical] = {}

        entry.total_products += 1
        entry.raw_variants.add(raw)

        # 대표 display_name: 가장 많이 등장한 원본 철자를 선택
        votes = display_name_votes[canonical]
        votes[raw] = votes.get(raw, 0) + 1

        atc_l3 = _atc_level3(p.get("atc_code"))
        if atc_l3:
            entry.atc_level3_counts[atc_l3] = entry.atc_level3_counts.get(atc_l3, 0) + 1

        inn = str(p.get("inn_name") or p.get("inn") or "").strip().lower()
        if inn:
            entry.inn_list.add(inn)

        tn = str(p.get("trade_name") or "").strip()
        if tn and tn not in entry.trade_names:
            entry.trade_names.append(tn)

        tier = p.get("source_tier")
        if tier is not None:
            try:
                entry.source_tiers.add(int(tier))
            except (TypeError, ValueError):
                pass

    # 4단계: display_name 최종 확정 (최다 득표)
    for canonical, entry in portfolio.items():
        votes = display_name_votes.get(canonical, {})
        if votes:
            entry.display_name = max(votes.items(), key=lambda kv: kv[1])[0]

    return portfolio, unmatched


# ─── 빈틈 분석 ─────────────────────────────────────────

def _compute_portfolio_strength(entry: AgentPortfolioEntry, atc_l3: str) -> float:
    """0~100 점수. log(제품수) + tier 1/2 비중 가중.

    = 50 * log10(1 + atc_products) + 30 * tier1_ratio + 20 * log10(1 + total) / 2
    """
    import math

    atc_count = entry.atc_level3_counts.get(atc_l3, 0)
    atc_score = 50.0 * min(math.log10(1 + atc_count) / math.log10(1 + 30), 1.0)
    tier1_score = 30.0 if 1 in entry.source_tiers else 0.0
    total_score = 20.0 * min(math.log10(1 + entry.total_products) / math.log10(1 + 100), 1.0)
    return round(atc_score + tier1_score + total_score, 1)


def _generate_sales_pitch(
    display_name: str,
    atc_l3: str,
    atc_count: int,
    target_inn: str,
    missing: bool,
) -> str:
    """영업용 한 문장 자동 생성."""
    if missing:
        return (
            f"{display_name} 는 {atc_l3} 치료군 {atc_count}개 품목을 SFDA 에 등록했으나 "
            f"{target_inn} 는 미취급 — 포트폴리오 보강 니즈 예상"
        )
    return (
        f"{display_name} 는 {atc_l3} 에 {atc_count}개 등록, "
        f"{target_inn} 이미 취급 중 — 교체/추가 공급 협상"
    )


def find_white_space(
    portfolio: dict[str, AgentPortfolioEntry],
    target_atc_level3: str,
    target_inn: str,
    *,
    min_atc_products: int = 3,
    top_n: int = 15,
) -> list[WhiteSpaceCandidate]:
    """타겟 ATC level3 × 타겟 INN 로 빈틈 에이전트 추출.

    Args:
        portfolio: build_agent_portfolio() 결과
        target_atc_level3: 예) "A10B" (당뇨 치료제)
        target_inn: 예) "dapagliflozin"
        min_atc_products: 최소 이 치료군 등록 품목 수 (페이퍼 거르기)
        top_n: 반환 최대 건수

    Returns:
        portfolio_strength 내림차순 WhiteSpaceCandidate 리스트
    """
    target_atc = (target_atc_level3 or "").upper().strip()
    target_inn_lower = (target_inn or "").lower().strip()
    candidates: list[WhiteSpaceCandidate] = []

    for canonical, entry in portfolio.items():
        atc_count = entry.atc_level3_counts.get(target_atc, 0)
        if atc_count < min_atc_products:
            continue
        missing = target_inn_lower not in entry.inn_list if target_inn_lower else True
        strength = _compute_portfolio_strength(entry, target_atc)
        pitch = _generate_sales_pitch(
            entry.display_name, target_atc, atc_count, target_inn or "타겟 성분", missing
        )
        candidates.append(
            WhiteSpaceCandidate(
                agent_name=canonical,
                display_name=entry.display_name,
                atc_level3=target_atc,
                product_count_in_atc=atc_count,
                total_products=entry.total_products,
                portfolio_strength=strength,
                missing_ingredient=missing,
                sample_trade_names=list(entry.trade_names[:5]),
                sales_pitch=pitch,
            )
        )

    # missing=True 먼저, 그 다음 strength 내림차순
    candidates.sort(key=lambda c: (not c.missing_ingredient, -c.portfolio_strength))
    return candidates[:top_n]


# ─── 통합 편의 함수 ──────────────────────────────────────

# inn_normalizer builtin 에 없는 최신 성분 보강 맵 (ATC 7자리 → level3 추출)
_EXTENDED_INN_ATC: dict[str, str] = {
    # SGLT-2 저해제 (당뇨)
    "dapagliflozin": "A10BK01",
    "empagliflozin": "A10BK03",
    "canagliflozin": "A10BK02",
    "ertugliflozin": "A10BK04",
    # GLP-1 작용제 (당뇨/비만)
    "semaglutide": "A10BJ06",
    "liraglutide": "A10BJ02",
    "dulaglutide": "A10BJ05",
    "tirzepatide": "A10BX16",
    # DPP-4 억제제
    "sitagliptin": "A10BH01",
    "vildagliptin": "A10BH02",
    "linagliptin": "A10BH05",
    # 최신 항응고제
    "rivaroxaban": "B01AF01",
    "apixaban": "B01AF02",
    "dabigatran": "B01AE07",
    "edoxaban": "B01AF03",
    # PCSK9 억제제
    "evolocumab": "C10AX13",
    "alirocumab": "C10AX14",
    # 단클론항체 (항암)
    "trastuzumab": "L01FD01",
    "bevacizumab": "L01FG01",
    "rituximab": "L01FA01",
    "cetuximab": "L01FE01",
    "pembrolizumab": "L01FF02",
    "nivolumab": "L01FF01",
    # 항류마티스 biologic
    "adalimumab": "L04AB04",
    "infliximab": "L04AB02",
    "etanercept": "L04AB01",
    "tocilizumab": "L04AC07",
    # 최신 HCV 치료제
    "sofosbuvir": "J05AP08",
    "ledipasvir": "J05AP51",
    "glecaprevir": "J05AP57",
    # 기타 자주 쓰는 성분 보강
    "tadalafil": "G04BE08",
    "sildenafil": "G04BE03",
    "vardenafil": "G04BE09",
    "finasteride": "G04CB01",
    "tamsulosin": "G04CA02",
    "rosuvastatin calcium": "C10AA07",
    "ezetimibe": "C10AX09",
}


def resolve_atc_from_inn(inn: str) -> Optional[str]:
    """INN 문자열 → ATC level3 (4글자) 변환. 실패 시 None.

    순서:
      1. inn_normalizer.normalize_to_inn 호출 (WHO 기반)
      2. 실패 시 확장 맵(_EXTENDED_INN_ATC)에서 lookup
    """
    if not inn:
        return None
    inn_key = inn.strip().lower()

    # 1. WHO INN normalizer
    try:
        from inn_normalizer import normalize_to_inn  # type: ignore

        result = normalize_to_inn(inn)
        if result and getattr(result, "success", False) and getattr(result, "inn_id", None):
            atc = _atc_level3(result.inn_id)
            if atc:
                return atc
    except Exception as exc:
        logger.debug("normalize_to_inn(%s) 실패: %s", inn, exc)

    # 2. 확장 맵
    ext = _EXTENDED_INN_ATC.get(inn_key)
    if ext:
        return _atc_level3(ext)

    # 3. 부분 일치 (공백·염 제거 후 재시도)
    simplified = re.sub(r"[^a-z]", "", inn_key)
    for key, atc in _EXTENDED_INN_ATC.items():
        if simplified == re.sub(r"[^a-z]", "", key):
            return _atc_level3(atc)

    return None


def analyze_white_space_for_inn(
    products: list[dict],
    target_inn: str,
    *,
    target_atc_level3: Optional[str] = None,
    min_atc_products: int = 3,
    top_n: int = 15,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> dict:
    """end-to-end 편의 함수: products + target_inn → 빈틈 분석 결과.

    Returns:
        {
          "target_inn": str,
          "target_atc_level3": str,
          "total_agents": int,
          "agents_in_atc": int,
          "candidates": [WhiteSpaceCandidate.to_dict(), ...],
          "unmatched_product_count": int,
        }
    """
    portfolio, unmatched = build_agent_portfolio(products, fuzzy_threshold=fuzzy_threshold)

    atc = (target_atc_level3 or "").upper().strip() or resolve_atc_from_inn(target_inn) or ""

    if not atc:
        return {
            "target_inn": target_inn,
            "target_atc_level3": None,
            "total_agents": len(portfolio),
            "agents_in_atc": 0,
            "candidates": [],
            "unmatched_product_count": len(unmatched),
            "error": f"INN '{target_inn}' 에서 ATC level3 를 결정할 수 없습니다.",
        }

    candidates = find_white_space(
        portfolio, atc, target_inn,
        min_atc_products=min_atc_products, top_n=top_n,
    )

    agents_in_atc = sum(
        1 for e in portfolio.values()
        if e.atc_level3_counts.get(atc, 0) >= min_atc_products
    )

    return {
        "target_inn": target_inn,
        "target_atc_level3": atc,
        "total_agents": len(portfolio),
        "agents_in_atc": agents_in_atc,
        "candidates": [c.to_dict() for c in candidates],
        "unmatched_product_count": len(unmatched),
    }
