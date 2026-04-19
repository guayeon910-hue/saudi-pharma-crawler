from __future__ import annotations

import io
import logging
import math
import re
from typing import Any, Optional

logger = logging.getLogger("frontend.fob_private")

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
                "type": str(item.get("type") or "").strip(),
            }
        )
    return deduped


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
                    "type": item.get("type") or key,
                }
            )

    deduped = _dedupe_price_samples(raw_items)

    # Fallback: DB/크롤 가격이 전혀 없으면 1공정 Claude 추정가를 단일 기준점으로 사용
    if not deduped:
        est = _safe_float(report_data.get("estimated_avg_sar"))
        if est and est > 0:
            deduped.append({
                "trade_name": str(report_data.get("trade_name") or report_data.get("product_id") or ""),
                "strength":   str(report_data.get("strength") or ""),
                "price":      est,
                "source":     "estimated",
                "type":       "estimated",
            })

    return deduped


def _build_competitor_stats(price_pool: list[dict]) -> dict:
    values = sorted(item["price"] for item in price_pool)
    if not values:
        raise ValueError("민간 시장 FOB 역산에 사용할 가격 샘플을 찾지 못했습니다.")

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

    return {
        "count": count,
        "min": _round_money(values[0]),
        "max": _round_money(values[-1]),
        "avg": _round_money(sum(values) / count),
        "p25": _round_money(p25),
        "median": _round_money(median),
        "p75": _round_money(p75),
        "warning": warning,
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


def _classify_private_context(product: dict, price_pool: list[dict], llm: Any | None) -> dict:
    classification = _heuristic_classification(product)
    warnings: list[str] = []

    if llm is None:
        warnings.append("Claude 미설정 — 기본 분류 규칙을 사용했습니다.")
        classification["warnings"] = warnings
        return classification

    prompt = f"""당신은 SFDA 민간 의약품 가격 규칙 전문가입니다.
다음 정보를 바탕으로 민간 시장 FOB 역산 파이프라인에서 사용할 분류값만 JSON으로 반환하세요.

품목명: {product.get("trade_name") or ""}
INN: {product.get("inn") or ""}
제형: {product.get("dosage_form") or ""}
함량: {product.get("strength") or ""}
HS 코드: {product.get("hs_code") or ""}
가격 샘플(SAR): {[round(x["price"], 2) for x in price_pool[:12]]}

응답 JSON 스키마:
{{
  "product_kind": "innovative" | "generic" | "biosimilar",
  "is_combination": true | false,
  "is_extended_release": true | false,
  "premium_factor": 0.0,
  "dosage_ratio": 1.0,
  "freight_base_sar_per_unit": 0.0,
  "rationale": "한국어 2문장 이내"
}}

원칙:
- 확실하지 않으면 generic
- 조합제/서방형이 명확하지 않으면 false
- premium_factor는 0.0~0.20
- dosage_ratio는 1.0 이상
- freight_base_sar_per_unit는 고형제 2~5, 액상 4~8, 주사제 8~15 범위 권장
JSON만 반환하세요."""

    try:
        from llm_client import MODEL_HAIKU

        response = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=800)
        parsed = response.parse_json()

        product_kind = str(parsed.get("product_kind") or classification["product_kind"]).strip().lower()
        if product_kind not in {"innovative", "generic", "biosimilar"}:
            product_kind = classification["product_kind"]

        premium_factor = _clamp(float(parsed.get("premium_factor") or 0.0), 0.0, COMBO_PREMIUM_MAX)
        dosage_ratio = max(float(parsed.get("dosage_ratio") or 1.0), 1.0)
        freight_base = max(float(parsed.get("freight_base_sar_per_unit") or classification["freight_base_sar_per_unit"]), 0.0)

        classification.update(
            {
                "product_kind": product_kind,
                "is_combination": bool(parsed.get("is_combination", classification["is_combination"])),
                "is_extended_release": bool(parsed.get("is_extended_release", classification["is_extended_release"])),
                "premium_factor": premium_factor,
                "dosage_ratio": dosage_ratio,
                "freight_base_sar_per_unit": freight_base,
                "rationale": str(parsed.get("rationale") or classification["rationale"]).strip(),
            }
        )
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
        return {
            "trade_name": report_data.get("trade_name") or report_data.get("product_name") or report_data.get("product_id") or "Unknown",
            "inn": report_data.get("inn") or report_data.get("ingredient") or "",
            "dosage_form": report_data.get("dosage_form") or "",
            "strength": report_data.get("strength") or "",
            "price_sar": report_data.get("price_sar"),
            "price_comparison": report_data.get("price_comparison") or {},
            "hs_code": report_data.get("hs_code"),
        }, notes

    if not pdf_bytes:
        raise ValueError("민간 시장 FOB 역산에는 report_data 또는 PDF 업로드가 필요합니다.")

    text = _extract_text_from_pdf_bytes(pdf_bytes)
    if not text:
        raise ValueError("업로드한 PDF에서 텍스트를 추출하지 못했습니다.")
    notes.append("PDF 업로드에서 텍스트를 추출해 민간 시장 FOB를 역산했습니다.")
    parsed_report = _extract_fields_from_text(text)
    if _pdf_report_needs_enrichment(parsed_report):
        parsed_report, enrichment_note = _enrich_pdf_report_from_text(text, parsed_report, llm)
        if enrichment_note:
            notes.append(enrichment_note)
    return parsed_report, notes


def _scenario_overrides(overrides: dict | None) -> dict[str, dict[str, float]]:
    raw = overrides or {}
    scenarios = raw.get("scenarios") if isinstance(raw.get("scenarios"), dict) else raw
    sanitized: dict[str, dict[str, float]] = {}
    for name in SCENARIO_DEFAULTS:
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


def _build_scenario_configs(stats: dict, overrides: dict | None) -> dict[str, dict]:
    sanitized_overrides = _scenario_overrides(overrides)
    scenarios: dict[str, dict] = {}
    for name, defaults in SCENARIO_DEFAULTS.items():
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


def run_private_pipeline(
    *,
    report_data: dict | None,
    pdf_bytes: bytes | None,
    overrides: dict | None,
    exchange_rates: dict | None,
    llm: Any | None,
) -> dict:
    product, source_notes = _normalize_report_source(report_data, pdf_bytes, llm)
    price_pool = _collect_price_pool(product)
    competitor_stats = _build_competitor_stats(price_pool)
    classification = _classify_private_context(product, price_pool, llm)
    scenarios = _build_scenario_configs(competitor_stats, overrides)

    notes = list(source_notes)
    notes.append("HS 3004 적격 의약품은 VAT 0%·관세 0%를 기본 가정으로 사용합니다.")
    hs_code = str(product.get("hs_code") or "").strip()
    if not hs_code or "3004" not in hs_code:
        notes.append("HS 코드가 3004로 확인되지 않았습니다. VAT 0%·관세 0% 가정이 맞는지 재확인하세요.")

    warnings = list(classification.get("warnings") or [])
    if competitor_stats.get("warning"):
        warnings.append(competitor_stats["warning"])
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
            _make_step(f"경쟁사 소매가 기준 ({config['price_basis']})", retail_sar),
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

    return {
        "ok": True,
        "market_type": "private",
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
        },
        "competitor_stats": competitor_stats,
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
