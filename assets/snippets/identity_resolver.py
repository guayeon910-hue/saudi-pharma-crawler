"""
identity_resolver.py — 소매 사이트 raw row를 SFDA 공식 데이터에 매칭

소매 사이트는 regulatory_id가 없는 경우가 많다. 이 모듈은
(trade_name, strength, dosage_form, scientific_name, atc_code)를 기반으로
SFDA 후보군과 유사도 점수를 계산해 가장 가까운 1건을 찾는다.

점수 공식 (schema.md 7절):
    score = 0.30 * trade_name_similarity
          + 0.25 * scientific_similarity
          + 0.20 * strength_proximity
          + 0.15 * dosage_form_match
          + 0.10 * atc_match

액션:
    score >= 0.85 → 자동 확정 (confidence += 0.10)
    0.60 ≤ score < 0.85 → 유사품 후보 (confidence += 0.05)
    score < 0.60 → 매칭 실패 (regulatory_id=null, confidence -= 0.10)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable


# ─── 유사도 함수들 ─────────────────────────────────────
def _lower_clean(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.lower()).strip()


def trade_name_similarity(a: str | None, b: str | None) -> float:
    """Levenshtein 비율 기반 0~1"""
    sa, sb = _lower_clean(a), _lower_clean(b)
    if not sa or not sb:
        return 0.0
    return SequenceMatcher(None, sa, sb).ratio()


def scientific_similarity(a: str | None, b: str | None) -> float:
    """성분명 Jaccard (토큰 단위).

    복합제 처리: '+', '/', '&', 'and'로 분리한 뒤 집합 비교.
    """
    if not a or not b:
        return 0.0
    tokens_a = _tokenize_scientific(a)
    tokens_b = _tokenize_scientific(b)
    if not tokens_a or not tokens_b:
        return 0.0
    inter = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(inter) / len(union)


def _tokenize_scientific(text: str) -> frozenset[str]:
    text = _lower_clean(text)
    # 복합제 구분자
    parts = re.split(r"\s*(?:[+/,&]|\band\b)\s*", text)
    return frozenset(p.strip() for p in parts if p.strip())


# ─── 함량 근접도 ───────────────────────────────────────
_STRENGTH_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z%]+)", re.IGNORECASE)


def _parse_strength_tokens(s: str | None) -> list[tuple[float, str]]:
    """'500 mg + 125 mg' → [(500.0, 'mg'), (125.0, 'mg')]"""
    if not s:
        return []
    out: list[tuple[float, str]] = []
    for match in _STRENGTH_NUM_RE.finditer(s.lower()):
        try:
            out.append((float(match.group(1)), match.group(2)))
        except ValueError:
            continue
    return out


def strength_proximity(a: str | None, b: str | None, *, tolerance: float = 0.10) -> float:
    """함량 근접도. 숫자+단위 모두 같고 숫자 오차 ±tolerance 이내면 1.0.

    토큰 수가 다르면 0 (복합제 vs 단일제)
    """
    ta = _parse_strength_tokens(a)
    tb = _parse_strength_tokens(b)
    if not ta or not tb:
        return 0.0
    if len(ta) != len(tb):
        return 0.0

    # 토큰별 비교 (순서 무관하게 정렬 후)
    ta_sorted = sorted(ta)
    tb_sorted = sorted(tb)
    matches = 0
    for (na, ua), (nb, ub) in zip(ta_sorted, tb_sorted):
        if ua != ub:
            continue
        if na == 0 or nb == 0:
            continue
        diff = abs(na - nb) / max(na, nb)
        if diff <= tolerance:
            matches += 1
    return matches / len(ta_sorted)


# ─── 제형 일치 ─────────────────────────────────────────
def dosage_form_match(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if _lower_clean(a) == _lower_clean(b) else 0.0


# ─── ATC 일치 ──────────────────────────────────────────
def atc_match(a: str | None, b: str | None) -> float:
    """ATC 5단계 코드 (예: A10BA02) 비교.

    단계별 점수:
        1단계(1자) 일치 → 0.2
        2단계(3자) 일치 → 0.5
        3단계(4자) 일치 → 0.7
        4단계(5자) 일치 → 0.9
        5단계 전체 일치 → 1.0
    """
    if not a or not b:
        return 0.0
    a = a.upper().strip()
    b = b.upper().strip()
    if a == b:
        return 1.0
    if len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]:
        return 0.9
    if len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]:
        return 0.7
    if len(a) >= 3 and len(b) >= 3 and a[:3] == b[:3]:
        return 0.5
    if a[:1] == b[:1]:
        return 0.2
    return 0.0


# ─── 스코어 합산 ───────────────────────────────────────
@dataclass
class MatchScore:
    total: float
    breakdown: dict[str, float]
    candidate: dict

    @property
    def verdict(self) -> str:
        """auto_confirm / candidate / no_match"""
        if self.total >= 0.85:
            return "auto_confirm"
        if self.total >= 0.60:
            return "candidate"
        return "no_match"

    @property
    def confidence_delta(self) -> float:
        return {
            "auto_confirm": +0.10,
            "candidate": +0.05,
            "no_match": -0.10,
        }[self.verdict]


WEIGHTS = {
    "trade_name": 0.30,
    "scientific": 0.25,
    "strength": 0.20,
    "dosage_form": 0.15,
    "atc": 0.10,
}


def score_candidate(raw: dict, candidate: dict) -> MatchScore:
    """raw(소매에서 긁은 것) vs candidate(SFDA에서 받은 1건)"""
    breakdown = {
        "trade_name": trade_name_similarity(
            raw.get("trade_name"), candidate.get("trade_name")
        ),
        "scientific": scientific_similarity(
            raw.get("scientific_name"), candidate.get("scientific_name")
        ),
        "strength": strength_proximity(
            raw.get("strength"), candidate.get("strength")
        ),
        "dosage_form": dosage_form_match(
            raw.get("dosage_form"), candidate.get("dosage_form")
        ),
        "atc": atc_match(raw.get("atc_code"), candidate.get("atc_code")),
    }
    total = sum(WEIGHTS[k] * v for k, v in breakdown.items())
    return MatchScore(total=total, breakdown=breakdown, candidate=candidate)


def find_best_match(raw: dict, candidates: Iterable[dict]) -> MatchScore | None:
    """후보군 중 가장 높은 점수의 MatchScore 반환. 없으면 None."""
    best: MatchScore | None = None
    for cand in candidates:
        score = score_candidate(raw, cand)
        if best is None or score.total > best.total:
            best = score
    return best


# ─── 자가 테스트 ───────────────────────────────────────
if __name__ == "__main__":
    raw = {
        "trade_name": "Omacor 1000mg",
        "scientific_name": "omega-3 acid ethyl esters",
        "strength": "1000 mg",
        "dosage_form": "soft_capsule",
        "atc_code": "C10AX06",
    }
    candidates = [
        {
            "trade_name": "Omacor",
            "scientific_name": "Omega-3-Acid Ethyl Esters 90",
            "strength": "1000 mg",
            "dosage_form": "soft_capsule",
            "atc_code": "C10AX06",
            "registration_number": "SFDA-12345",
        },
        {
            "trade_name": "Lipitor",
            "scientific_name": "Atorvastatin",
            "strength": "10 mg",
            "dosage_form": "tablet",
            "atc_code": "C10AA05",
            "registration_number": "SFDA-99999",
        },
    ]
    best = find_best_match(raw, candidates)
    assert best is not None
    assert best.candidate["registration_number"] == "SFDA-12345"
    assert best.verdict in ("auto_confirm", "candidate")
    print(f"✅ identity_resolver 테스트 통과: score={best.total:.3f}, verdict={best.verdict}")
    print(f"   breakdown: {best.breakdown}")
