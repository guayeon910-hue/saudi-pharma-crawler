"""analytics/tender_power.py — 에이전트 Tender Power 스코어링.

페이퍼컴퍼니 걸러내고 진성 우량 바이어만 상위 노출하기 위한 정량 점수.

데이터 소스:
    - `contracts` 테이블 (Etimad API 기반, supplier_name + contract_value)
    - `nupco_awards` 테이블 (crawlers/nupco_awards 에서 생성)

점수 공식 (plan 1:1 구현):
    score =   log10(1 + count_last_2y)     * 50
            + log10(1 + sum_value_mn_sar)   * 30
            + (1 if atc_match else 0)       * 20
    (최대 100점 기준으로 clamp)

에이전트 이름 매칭:
    - agent_portfolio._normalize_agent_name() + rapidfuzz WRatio (threshold 85)
    - unmatched supplier_name 은 unmatched_suppliers 로 반환 → 데이터 품질 추적
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .agent_portfolio import _normalize_agent_name, DEFAULT_FUZZY_THRESHOLD

try:
    from rapidfuzz import fuzz  # type: ignore
    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False


# ─── 기본 상수 ──────────────────────────────────────────────────────

DEFAULT_WINDOW_DAYS = 730  # 2년
SAR_MILLION = 1_000_000.0

# 점수 가중치 (plan 1:1)
WEIGHT_COUNT = 50.0
WEIGHT_VALUE = 30.0
WEIGHT_ATC_MATCH = 20.0


# ─── 데이터클래스 ────────────────────────────────────────────────────

@dataclass
class TenderRecord:
    """단일 낙찰/계약 레코드 (contracts + nupco_awards 통합 뷰)."""
    supplier_name: str
    value_sar: Optional[float]
    date: Optional[str]                # ISO YYYY-MM-DD 또는 None
    category: Optional[str] = None     # atc_code 매칭용 자유 텍스트 (Etimad 'category')
    source: str = "etimad"              # "etimad" | "nupco"

    @property
    def normalized_name(self) -> str:
        return _normalize_agent_name(self.supplier_name)

    def is_within(self, cutoff: datetime) -> bool:
        """레코드 날짜가 cutoff 이후인지. date 파싱 실패시 True (보수적)."""
        if not self.date:
            return True
        try:
            dt = datetime.fromisoformat(self.date[:10])
        except (ValueError, TypeError):
            return True
        # date 는 UTC-naive 가정 — cutoff 도 naive 로 비교
        if cutoff.tzinfo is not None:
            cutoff = cutoff.replace(tzinfo=None)
        return dt >= cutoff


@dataclass
class TenderPowerScore:
    """에이전트별 Tender Power 점수 및 근거."""
    agent_name: str                                # display name
    normalized_name: str                           # normalized
    score: float                                   # 0~100
    count_last_2y: int                             # 기간 내 낙찰 건수
    total_value_sar: float                         # 기간 내 누적 금액 (SAR)
    total_value_mn_sar: float                      # 백만 SAR
    has_target_atc_match: bool                     # 타겟 ATC/카테고리 매치
    sources: dict[str, int] = field(default_factory=dict)  # {"etimad": n, "nupco": m}
    breakdown: dict[str, float] = field(default_factory=dict)  # 가중치 기여도

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "normalized_name": self.normalized_name,
            "score": round(self.score, 1),
            "count_last_2y": self.count_last_2y,
            "total_value_sar": round(self.total_value_sar, 2),
            "total_value_mn_sar": round(self.total_value_mn_sar, 2),
            "has_target_atc_match": self.has_target_atc_match,
            "sources": dict(self.sources),
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
        }


# ─── 레코드 어댑터 ──────────────────────────────────────────────────

def _safe_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or v < 0:
        return None
    return v


def contracts_rows_to_records(rows: Iterable[dict]) -> list[TenderRecord]:
    """Supabase `contracts` 테이블 row 들을 TenderRecord 로 변환."""
    out: list[TenderRecord] = []
    for r in rows or []:
        supplier = (r.get("supplier_name") or "").strip()
        if not supplier:
            continue
        out.append(TenderRecord(
            supplier_name=supplier,
            value_sar=_safe_float(r.get("contract_value")),
            date=(r.get("start_date") or r.get("end_date") or None),
            category=(r.get("category") or None),
            source="etimad",
        ))
    return out


def nupco_awards_rows_to_records(rows: Iterable[dict]) -> list[TenderRecord]:
    """Supabase `nupco_awards` 테이블 row 들을 TenderRecord 로 변환."""
    out: list[TenderRecord] = []
    for r in rows or []:
        winner = (r.get("winner_name") or "").strip()
        if not winner:
            continue
        out.append(TenderRecord(
            supplier_name=winner,
            value_sar=_safe_float(r.get("award_value")),
            date=r.get("award_date"),
            category=r.get("category"),
            source="nupco",
        ))
    return out


# ─── 에이전트 이름 매칭 ─────────────────────────────────────────────

def _best_agent_match(
    supplier_norm: str,
    known_agents_norm: dict[str, str],
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> Optional[str]:
    """supplier 정규화명을 에이전트 목록과 매칭.

    known_agents_norm : { normalized_name: display_name }

    Returns:
        매칭된 normalized_name, 없으면 None.
    """
    if not supplier_norm or not known_agents_norm:
        return None

    # 정확 일치 먼저
    if supplier_norm in known_agents_norm:
        return supplier_norm

    # 포함 관계 (a is substring of b 또는 역)
    for k in known_agents_norm:
        if len(supplier_norm) >= 5 and len(k) >= 5:
            if supplier_norm in k or k in supplier_norm:
                return k

    # fuzzy (rapidfuzz 있을 때만)
    if not _HAS_RAPIDFUZZ:
        return None

    best_key = None
    best_score = 0.0
    threshold_pct = float(threshold) * 100.0
    for k in known_agents_norm:
        s = fuzz.WRatio(supplier_norm, k)
        if s > best_score:
            best_score = s
            best_key = k
    if best_score >= threshold_pct:
        return best_key
    return None


# ─── ATC 카테고리 매칭 ─────────────────────────────────────────────

_ATC_CATEGORY_KEYWORDS = {
    # ATC level3 -> 카테고리 텍스트 키워드 (영문 소문자 + 아랍어 일부)
    "A10A": ["insulin", "إنسولين"],
    "A10B": ["diabetes", "oral antidiabetic", "glucose", "سكري"],
    "B01A": ["anticoagulant", "blood thinner", "heparin", "warfarin", "مضاد التخثر"],
    "C09A": ["ace inhibitor", "antihypertensive", "ضغط"],
    "C10A": ["statin", "cholesterol", "lipid", "دهون"],
    "J01": ["antibiotic", "antimicrobial", "مضاد حيوي"],
    "J06B": ["immunoglobulin", "ivig", "immunoglobulin"],
    "L01": ["oncology", "cancer", "chemotherapy", "أورام", "سرطان"],
    "L04A": ["immunosuppressant", "biologic", "biosimilar", "مناعي"],
    "N02": ["analgesic", "pain", "مسكن"],
    "N05A": ["antipsychotic", "psychiatric"],
    "N06A": ["antidepressant"],
    "R03": ["asthma", "respiratory", "inhaler", "ربو"],
}


def _category_matches_atc(category: Optional[str], target_atc_l3: Optional[str]) -> bool:
    """계약 category 텍스트가 타겟 ATC level3 와 키워드 매칭하는지."""
    if not category or not target_atc_l3:
        return False
    atc = target_atc_l3.upper().strip()
    keywords = _ATC_CATEGORY_KEYWORDS.get(atc) or _ATC_CATEGORY_KEYWORDS.get(atc[:3])
    if not keywords:
        return False
    cat_lc = category.lower()
    return any(kw.lower() in cat_lc for kw in keywords)


# ─── 핵심 스코어링 ─────────────────────────────────────────────────

def _log10_positive(x: float) -> float:
    """log10(1 + x), x < 0 이면 0."""
    if x <= 0:
        return 0.0
    return math.log10(1.0 + x)


def compute_score(
    records: list[TenderRecord],
    *,
    target_atc_l3: Optional[str] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> TenderPowerScore:
    """단일 에이전트(이미 매칭된 레코드 묶음)의 Tender Power 점수.

    Args:
        records: 해당 에이전트의 TenderRecord 리스트 (이미 매칭됨)
        target_atc_l3: 선택적 타겟 ATC level3 (예: "A10B")
        window_days: 최근 N일 창 (기본 730일 = 2년)
        now: 기준 시각 (테스트 주입용)

    Returns:
        TenderPowerScore
    """
    if not records:
        return TenderPowerScore(
            agent_name="", normalized_name="", score=0.0,
            count_last_2y=0, total_value_sar=0.0, total_value_mn_sar=0.0,
            has_target_atc_match=False, sources={}, breakdown={},
        )

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=window_days)

    # 대표 display_name = 첫 레코드의 supplier_name
    display_name = records[0].supplier_name
    norm_name = records[0].normalized_name

    in_window = [r for r in records if r.is_within(cutoff)]
    count = len(in_window)
    total_val = sum((r.value_sar or 0.0) for r in in_window)
    total_mn = total_val / SAR_MILLION

    atc_match = False
    if target_atc_l3:
        atc_match = any(_category_matches_atc(r.category, target_atc_l3) for r in in_window)

    sources: dict[str, int] = {}
    for r in in_window:
        sources[r.source] = sources.get(r.source, 0) + 1

    # 점수 구성요소
    count_component = _log10_positive(count) * WEIGHT_COUNT       # e.g. 10건 → log10(11)*50 = 52.0
    value_component = _log10_positive(total_mn) * WEIGHT_VALUE    # e.g. 100M SAR → log10(101)*30 = 60.3
    atc_component = (WEIGHT_ATC_MATCH if atc_match else 0.0)

    raw_score = count_component + value_component + atc_component
    # 100점 clamp
    score = min(100.0, max(0.0, raw_score))

    return TenderPowerScore(
        agent_name=display_name,
        normalized_name=norm_name,
        score=score,
        count_last_2y=count,
        total_value_sar=total_val,
        total_value_mn_sar=total_mn,
        has_target_atc_match=atc_match,
        sources=sources,
        breakdown={
            "count_component": count_component,
            "value_component": value_component,
            "atc_component": atc_component,
            "raw_total": raw_score,
        },
    )


def compute_tender_power_for_agents(
    agent_names: list[str],
    records: list[TenderRecord],
    *,
    target_atc_l3: Optional[str] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    now: Optional[datetime] = None,
) -> dict[str, dict]:
    """에이전트 이름 리스트 → 각각의 Tender Power 점수 dict.

    Args:
        agent_names: display 이름 리스트 (white-space 후보 agents 등)
        records: 모든 contracts + nupco_awards 의 TenderRecord 통합 리스트
        target_atc_l3: 선택적 타겟 ATC level3
        window_days: 최근 N일 창
        fuzzy_threshold: 에이전트 매칭 fuzzy threshold (0~1)

    Returns:
        { agent_display_name: TenderPowerScore.to_dict() }
        매칭된 레코드가 없는 에이전트는 score=0 zero-entry.
    """
    # agent_names 정규화 맵
    known_agents: dict[str, str] = {}   # {normalized: display}
    for name in agent_names:
        norm = _normalize_agent_name(name)
        if norm and norm not in known_agents:
            known_agents[norm] = name

    # 레코드를 정규화명 기준으로 매칭 → grouped
    grouped: dict[str, list[TenderRecord]] = {k: [] for k in known_agents}
    unmatched_suppliers: dict[str, int] = {}

    for rec in records:
        supplier_norm = rec.normalized_name
        if not supplier_norm:
            continue
        matched_key = _best_agent_match(supplier_norm, known_agents, threshold=fuzzy_threshold)
        if matched_key:
            # display name 은 agent_names 가 우선이지만,
            # 레코드 쪽 원문이 더 자세하면 유지
            # (여기서는 매칭 여부만 추적; display 는 agent_names 를 씀)
            grouped[matched_key].append(rec)
        else:
            unmatched_suppliers[supplier_norm] = unmatched_suppliers.get(supplier_norm, 0) + 1

    result: dict[str, dict] = {}
    for norm_key, display in known_agents.items():
        recs = grouped.get(norm_key, [])
        if recs:
            # score 계산을 위해 첫 레코드의 supplier_name 을 agent display 로 덮어씀
            # (UI 에서는 white-space 의 display name 사용)
            score = compute_score(
                recs, target_atc_l3=target_atc_l3,
                window_days=window_days, now=now,
            )
            d = score.to_dict()
            d["agent_name"] = display  # UI 일관성
            result[display] = d
        else:
            result[display] = {
                "agent_name": display,
                "normalized_name": norm_key,
                "score": 0.0,
                "count_last_2y": 0,
                "total_value_sar": 0.0,
                "total_value_mn_sar": 0.0,
                "has_target_atc_match": False,
                "sources": {},
                "breakdown": {
                    "count_component": 0.0,
                    "value_component": 0.0,
                    "atc_component": 0.0,
                    "raw_total": 0.0,
                },
            }

    # 메타: unmatched 는 호출자가 로그로 남길 수 있게 별도 key 로 반환
    result["__meta__"] = {
        "unmatched_supplier_count": sum(unmatched_suppliers.values()),
        "unmatched_supplier_unique": len(unmatched_suppliers),
        "target_atc_l3": target_atc_l3,
        "window_days": window_days,
    }
    return result


def score_band(score: float) -> str:
    """점수를 UI 배지용 band 로 변환."""
    if score >= 70:
        return "strong"
    if score >= 40:
        return "mid"
    if score > 0:
        return "weak"
    return "none"
