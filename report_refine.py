"""
report_refine -- 보고서 셀용 텍스트 정제 및 출처 집계

크롤/LLM 출력에 섞인 JSON 조각을 자연어로 바꾸고,
source_results에서 근거 표용 건수·신뢰도를 집계한다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("report_refine")

_DEFAULT_SOURCE_CONFIDENCE = 0.85

_JSON_KEY_PRIORITY = (
    "notes_plain",
    "message",
    "summary",
    "text",
    "note",
    "description",
    "content",
)


def refine_cell_text(text: str | None) -> str:
    """셀에 들어갈 문자열을 정제한다. JSON blob은 가능한 한 자연어로 추출."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""

    extracted = _try_extract_from_json(s)
    if extracted:
        return _collapse_whitespace(extracted)

    # 인라인 JSON 서브스트링 (전체가 JSON은 아닐 때)
    if "{" in s and "notes_plain" in s:
        m = re.search(r'"notes_plain"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        if m:
            inner = m.group(1).replace("\\n", " ").replace("\\\"", "\"")
            return _collapse_whitespace(inner)

    if len(s) > 280 and ("{" in s[:120] or '"item_' in s[:200]):
        return _collapse_whitespace(s[:200]) + "…"

    return _collapse_whitespace(s)


def _collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _try_extract_from_json(s: str) -> str | None:
    if not s.startswith("{") and not s.startswith("["):
        return None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # 트레일링 쉼표 등으로 실패하면 첫 번째 `{...}` 블록만 시도
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None

    for key in _JSON_KEY_PRIORITY:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # fallback: 짧은 문자열 값만 이어붙이기
    parts: list[str] = []
    for k, v in data.items():
        if k in ("item_collected_at_fallback", "confidence"):
            continue
        if isinstance(v, str) and len(v) < 500 and v.strip():
            parts.append(v.strip())
        elif isinstance(v, (int, float)):
            parts.append(f"{k}: {v}")
    if parts:
        return " ".join(parts[:5])
    return None


def _match_confidences(matches: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for m in matches:
        c = m.get("confidence")
        if c is None:
            continue
        try:
            out.append(float(c))
        except (TypeError, ValueError):
            continue
    return out


def aggregate_evidence_by_source(
    source_results: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """출처별 건수와 평균 신뢰도."""
    if not source_results:
        return []

    rows: list[dict[str, Any]] = []
    for sr in source_results:
        name = str(sr.get("source_name") or sr.get("source") or "unknown").strip()
        matches = sr.get("matches") or []
        if not isinstance(matches, list):
            matches = []
        n = len(matches)
        confs = _match_confidences(matches)
        if confs:
            avg_c = round(sum(confs) / len(confs), 2)
        else:
            avg_c = _DEFAULT_SOURCE_CONFIDENCE

        rows.append({
            "source": name,
            "count": n,
            "avg_confidence": avg_c,
        })

    rows.sort(key=lambda r: (-r["count"], r["source"]))
    return rows


def count_procurement_vs_retail(
    source_results: list[dict[str, Any]] | None,
) -> tuple[int, int]:
    """공공조달 매치 수, 민간 매치 수."""
    pub = 0
    priv = 0
    if not source_results:
        return pub, priv

    for sr in source_results:
        cat = (sr.get("source_category") or "").strip()
        matches = sr.get("matches") or []
        if not isinstance(matches, list):
            continue
        n = len(matches)
        if cat == "공공조달":
            pub += n
        elif cat == "민간":
            priv += n
    return pub, priv


def fill_pillar_fallbacks(
    analysis: dict[str, Any],
) -> dict[str, str]:
    """pillars 누락 시 rationale/key_factors로 5개 축 채우기."""
    rationale = refine_cell_text(analysis.get("rationale") or "")
    factors = analysis.get("key_factors") or []
    if not isinstance(factors, list):
        factors = []

    pillars_in = analysis.get("pillars") or {}
    if not isinstance(pillars_in, dict):
        pillars_in = {}

    keys = (
        "market_medical",
        "regulation",
        "trade",
        "procurement",
        "distribution",
    )
    labels_ko = (
        "시장·의료",
        "규제",
        "무역",
        "조달",
        "유통",
    )

    out: dict[str, str] = {}
    for i, k in enumerate(keys):
        v = pillars_in.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = refine_cell_text(v)
        else:
            if i < len(factors) and isinstance(factors[i], str) and factors[i].strip():
                out[k] = refine_cell_text(factors[i])
            elif rationale:
                out[k] = f"({labels_ko[i]}) {rationale[:400]}"
            else:
                out[k] = "해당 축에 대한 상세 데이터가 충분하지 않습니다."

    return out


def fill_strategy_fallbacks(analysis: dict[str, Any]) -> dict[str, str]:
    """strategy 객체 폴백."""
    s_in = analysis.get("strategy") or {}
    if not isinstance(s_in, dict):
        s_in = {}

    rationale = refine_cell_text(analysis.get("rationale") or "")
    keys = (
        "entry_channels",
        "price_positioning",
        "distribution_partners",
        "risk_conditions",
    )
    out: dict[str, str] = {}
    for k in keys:
        v = s_in.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = refine_cell_text(v)
        else:
            out[k] = rationale[:600] if rationale else "전략 문구 생성 데이터가 부족합니다."
    return out
