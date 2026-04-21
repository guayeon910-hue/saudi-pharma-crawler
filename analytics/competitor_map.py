"""analytics/competitor_map.py — 경쟁사 에이전트 역추적.

"우리 경쟁 제품을 사우디에서 누가 독점 유통 중인가?" 를 정량화.

입력:
    - SFDA 매칭 레코드 (targeted_search.py::SearchResult.matches)
      또는 Supabase products 테이블 행 (동일 스키마)
    - 대상 trade_name (경쟁 브랜드 제외용, 선택)

출력:
    - CompetitorAgent[] : 에이전트별 경쟁 브랜드 묶음 + 시장 시그널

시장 점유 휴리스틱 (plan 5-1: "SFDA 가격 + NUPCO 낙찰 횟수 조합"):
    share_weight = log10(1 + brand_count) * 0.5
                 + log10(1 + avg_price_sar/100) * 0.25
                 + log10(1 + tender_count) * 0.25
    → 정규화하여 0~1 상대 점유율 추정

정책:
    - 우리 자사 브랜드(target_brand_norm)는 제외 → 진짜 '경쟁' 에이전트만 남김
    - agent_or_supplier 비어있는 레코드는 drop (추적 불가)
    - 에이전트 이름 정규화: agent_portfolio._normalize_agent_name()
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .agent_portfolio import _normalize_agent_name, _fuzzy_merge_keys, DEFAULT_FUZZY_THRESHOLD


# ─── 데이터클래스 ────────────────────────────────────────────────────

@dataclass
class CompetitorBrand:
    """에이전트가 취급하는 경쟁 브랜드 1건."""
    trade_name: str
    price_sar: Optional[float] = None
    regulatory_id: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "trade_name": self.trade_name,
            "price_sar": self.price_sar,
            "regulatory_id": self.regulatory_id,
            "dosage_form": self.dosage_form,
            "strength": self.strength,
            "source_url": self.source_url,
        }


@dataclass
class CompetitorAgent:
    """경쟁사 유통 에이전트 1건."""
    display_name: str
    normalized_name: str
    brand_count: int                                      # 경쟁 브랜드 수
    competitor_brands: list[CompetitorBrand] = field(default_factory=list)
    avg_price_sar: Optional[float] = None
    min_price_sar: Optional[float] = None
    max_price_sar: Optional[float] = None
    tender_count: int = 0                                  # nupco_awards + contracts 매치 수
    tender_total_mn_sar: float = 0.0
    market_share_est: float = 0.0                         # 0.0 ~ 1.0
    share_weight_raw: float = 0.0                         # 정규화 전 가중치

    def to_dict(self) -> dict:
        return {
            "agent_name": self.display_name,
            "normalized_name": self.normalized_name,
            "brand_count": self.brand_count,
            "competitor_brands": [b.to_dict() for b in self.competitor_brands],
            "avg_price_sar": round(self.avg_price_sar, 2) if self.avg_price_sar else None,
            "min_price_sar": round(self.min_price_sar, 2) if self.min_price_sar else None,
            "max_price_sar": round(self.max_price_sar, 2) if self.max_price_sar else None,
            "tender_count": self.tender_count,
            "tender_total_mn_sar": round(self.tender_total_mn_sar, 2),
            "market_share_est": round(self.market_share_est, 3),
            "share_weight_raw": round(self.share_weight_raw, 4),
        }


# ─── 추출 로직 ───────────────────────────────────────────────────────

def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or v < 0:
        return None
    return v


def _normalize_brand_for_compare(raw: Optional[str]) -> str:
    """브랜드명 비교용 정규화 (공백 축약, 소문자, 영숫자만)."""
    if not raw:
        return ""
    s = raw.lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_competitor_agents(
    sfda_matches: Iterable[dict],
    *,
    target_brand: Optional[str] = None,
    target_agent: Optional[str] = None,
    min_brand_count: int = 1,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> list[CompetitorAgent]:
    """SFDA 매칭 레코드 → 에이전트별 경쟁 브랜드 맵.

    Args:
        sfda_matches: SFDA 레코드들. 필드 기대:
            - trade_name (or brand_name)
            - agent_or_supplier
            - price_sar (or price)
            - regulatory_id
            - dosage_form, strength
            - source_url
        target_brand: 자사 브랜드명 (경쟁 목록에서 제외). None 이면 제외 없음.
        target_agent: 자사 에이전트명 (맵에서 제외). None 이면 제외 없음.
        min_brand_count: 에이전트 당 최소 브랜드 수 (이하는 drop)

    Returns:
        CompetitorAgent 리스트, market_share_est 내림차순.
    """
    target_brand_norm = _normalize_brand_for_compare(target_brand) if target_brand else ""
    target_agent_norm = _normalize_agent_name(target_agent) if target_agent else ""

    # 0단계: 에이전트 이름 수집 후 fuzzy 병합 맵 생성
    #   "Tabuk Pharmaceuticals" vs "Tabuk Pharma" 을 하나로 묶기 위해
    raw_agent_norms: list[str] = []
    for rec in sfda_matches or []:
        agent_raw = (
            rec.get("agent_or_supplier")
            or rec.get("agent_name")
            or rec.get("manufacturer")
            or ""
        ).strip()
        if not agent_raw:
            continue
        norm = _normalize_agent_name(agent_raw)
        if norm and norm not in raw_agent_norms:
            raw_agent_norms.append(norm)
    canonical_map = _fuzzy_merge_keys(raw_agent_norms, threshold=fuzzy_threshold)

    # 1단계: 에이전트별 브랜드 묶음 (canonical 키 사용)
    agent_bucket: dict[str, dict] = {}
    # agent_bucket[normalized_name] = {
    #   "display": str,
    #   "brands": dict[brand_norm → CompetitorBrand],
    # }

    for rec in sfda_matches or []:
        agent_raw = (
            rec.get("agent_or_supplier")
            or rec.get("agent_name")
            or rec.get("manufacturer")
            or ""
        ).strip()
        if not agent_raw:
            continue

        agent_norm_raw = _normalize_agent_name(agent_raw)
        if not agent_norm_raw:
            continue
        # canonical 로 치환 (fuzzy 병합)
        agent_norm = canonical_map.get(agent_norm_raw, agent_norm_raw)

        # 자사 에이전트 제외 (target 도 canonical 로 치환 후 비교)
        if target_agent_norm:
            target_canon = canonical_map.get(target_agent_norm, target_agent_norm)
            if agent_norm == target_canon:
                continue

        trade_name = (
            rec.get("trade_name")
            or rec.get("brand_name")
            or rec.get("product_name")
            or ""
        ).strip()
        if not trade_name:
            continue

        brand_norm = _normalize_brand_for_compare(trade_name)

        # 자사 브랜드 제외
        if target_brand_norm and brand_norm == target_brand_norm:
            continue

        entry = agent_bucket.setdefault(agent_norm, {
            "display": agent_raw,
            "brands": {},
        })
        # display: 가장 긴 원문 보존 (일반적으로 더 자세한 이름)
        if len(agent_raw) > len(entry["display"]):
            entry["display"] = agent_raw

        price = _safe_float(rec.get("price_sar") or rec.get("price"))

        existing = entry["brands"].get(brand_norm)
        if existing is None:
            entry["brands"][brand_norm] = CompetitorBrand(
                trade_name=trade_name,
                price_sar=price,
                regulatory_id=(rec.get("regulatory_id") or rec.get("registration_id") or None),
                dosage_form=rec.get("dosage_form") or None,
                strength=rec.get("strength") or None,
                source_url=rec.get("source_url") or None,
            )
        else:
            # 중복 브랜드: 가격이 있으면 평균 보완 (단순히 최신값 유지하지 않고 첫 가격 보존)
            if existing.price_sar is None and price is not None:
                existing.price_sar = price

    # 2단계: 가중치 계산
    agents: list[CompetitorAgent] = []
    for norm_name, entry in agent_bucket.items():
        brands = list(entry["brands"].values())
        if len(brands) < min_brand_count:
            continue

        prices = [b.price_sar for b in brands if b.price_sar is not None]
        avg_price = sum(prices) / len(prices) if prices else None
        min_price = min(prices) if prices else None
        max_price = max(prices) if prices else None

        agents.append(CompetitorAgent(
            display_name=entry["display"],
            normalized_name=norm_name,
            brand_count=len(brands),
            competitor_brands=brands,
            avg_price_sar=avg_price,
            min_price_sar=min_price,
            max_price_sar=max_price,
        ))

    # 3단계: share_weight 계산 + 정규화
    for a in agents:
        w_brand = math.log10(1 + a.brand_count) * 0.5
        w_price = math.log10(1 + (a.avg_price_sar or 0) / 100.0) * 0.25
        w_tender = math.log10(1 + a.tender_count) * 0.25
        a.share_weight_raw = w_brand + w_price + w_tender

    total_w = sum(a.share_weight_raw for a in agents)
    if total_w > 0:
        for a in agents:
            a.market_share_est = a.share_weight_raw / total_w
    # 정렬
    agents.sort(key=lambda a: (-a.market_share_est, -a.brand_count))
    return agents


# ─── Tender 데이터 조인 (선택) ─────────────────────────────────────────

def enrich_with_tender_data(
    agents: list[CompetitorAgent],
    tender_records: list,   # analytics.tender_power.TenderRecord
    *,
    fuzzy_threshold: float = 0.85,
) -> list[CompetitorAgent]:
    """경쟁 에이전트에 tender 실적 데이터를 병합 → share 재계산.

    Args:
        agents: extract_competitor_agents 반환값
        tender_records: analytics.tender_power.TenderRecord 리스트
        fuzzy_threshold: 매칭 fuzzy threshold (0~1)

    Returns:
        수정된 agents 동일 리스트 (in-place 수정 + share 재정규화).
    """
    from .tender_power import _best_agent_match  # 순환 import 피하기

    # agents 정규화 맵
    known = {a.normalized_name: a for a in agents}

    # tender 매칭
    for rec in tender_records or []:
        supplier_norm = rec.normalized_name
        if not supplier_norm:
            continue
        matched_key = _best_agent_match(supplier_norm, {k: k for k in known},
                                        threshold=fuzzy_threshold)
        if matched_key and matched_key in known:
            a = known[matched_key]
            a.tender_count += 1
            a.tender_total_mn_sar += (rec.value_sar or 0) / 1_000_000.0

    # share_weight 재계산
    for a in agents:
        w_brand = math.log10(1 + a.brand_count) * 0.5
        w_price = math.log10(1 + (a.avg_price_sar or 0) / 100.0) * 0.25
        w_tender = math.log10(1 + a.tender_count) * 0.25
        a.share_weight_raw = w_brand + w_price + w_tender

    total_w = sum(a.share_weight_raw for a in agents)
    if total_w > 0:
        for a in agents:
            a.market_share_est = a.share_weight_raw / total_w

    agents.sort(key=lambda a: (-a.market_share_est, -a.brand_count))
    return agents


# ─── 편의 entry-point ────────────────────────────────────────────────

def build_competitor_map(
    sfda_matches: Iterable[dict],
    *,
    target_brand: Optional[str] = None,
    target_agent: Optional[str] = None,
    tender_records: Optional[list] = None,
    min_brand_count: int = 1,
    top_n: int = 20,
) -> dict:
    """end-to-end: SFDA 매칭 → 경쟁 에이전트 맵 (top_n).

    Returns:
        {
          "agents": [CompetitorAgent.to_dict(), ...],
          "total_agents": int,
          "total_brands": int,
          "target_brand_excluded": bool,
          "target_agent_excluded": bool,
        }
    """
    agents = extract_competitor_agents(
        sfda_matches,
        target_brand=target_brand,
        target_agent=target_agent,
        min_brand_count=min_brand_count,
    )

    if tender_records:
        enrich_with_tender_data(agents, tender_records)

    # top_n slice
    top_agents = agents[: top_n]

    return {
        "agents": [a.to_dict() for a in top_agents],
        "total_agents": len(agents),
        "total_brands": sum(a.brand_count for a in agents),
        "target_brand_excluded": bool(target_brand),
        "target_agent_excluded": bool(target_agent),
    }
