"""
frontend/fob_private.py -- 2공정 민간 시장 FOB 역산 파이프라인

단일 책임:
  1) 1공정에서 저장된 report_data(JSON) 또는 업로드된 PDF 텍스트를 입력받는다.
  2) 저장된 `price_comparison`에서 경쟁가 풀을 정규화한다.
  3) Claude(+로컬 휴리스틱)로 품목 분류(originator/generic/biosimilar, combo, ER, 운임 base 등)를 얻는다.
  4) 공격적/평균/보수적 세 시나리오에 대해
       Retail → CIF 역산 → generic/biosimilar cap → dosage adjustment
       → combination premium → freight 차감 → agent commission 차감
     순으로 FOB(SAR/USD/KRW)를 계산한다.
  5) 항만료/감가상각 규제비는 FOB에서 차감하지 않고 별도 패널로 반환한다.

순환 import를 피하려 `_fetch_exchange_rates()`와 `_get_llm()` 결과는 server.py에서 주입한다.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("frontend.fob_private")

# ═══════════════════════════════════════════════════════════════════════════
# 상수 (Saudi PPR 단계별 약가 역산 모델 — 공개 문서 기반 근사)
# ═══════════════════════════════════════════════════════════════════════════

WHOLESALER_MARGIN_PCT = 0.10      # 도매 실효 마진 (CIF → 도매 정산)
VAT_PCT_PRIVATE = 0.15            # 민간 부가세 (HS 3004 의약품은 면세인 경우가 많음)
VAT_PCT_HS3004 = 0.00             # HS 3004 면세

# retail 금액대별 약국 실효 마진 (Saudi SFDA Price Regulation Model 근사)
PHARMACY_MARGIN_LOW = 0.20        # retail < 69 SAR
PHARMACY_MARGIN_MID = 0.15        # 69 <= retail < 253
PHARMACY_MARGIN_HIGH = 0.10       # retail >= 253

THRESHOLD_LOW_SAR = 69.0
THRESHOLD_HIGH_SAR = 253.0

# 제네릭/바이오시밀러 오리지널 대비 CIF 상한
GENERIC_CIF_CAP_PCT = 0.70        # 오리지널 대비 30% 할인 상한
BIOSIMILAR_CIF_CAP_PCT = 0.80     # 20% 할인

# 제형별 기본 운임 (SAR/포장단위, 1box 기준 추산)
DEFAULT_FREIGHT_BY_FORM = {
    "tablet": 0.15,
    "tab": 0.15,
    "capsule": 0.15,
    "cap": 0.15,
    "injection": 0.80,
    "inj": 0.80,
    "vial": 0.80,
    "amp": 0.80,
    "syrup": 0.40,
    "solution": 0.40,
    "sol": 0.40,
    "cream": 0.25,
    "ointment": 0.25,
    "gel": 0.25,
    "drop": 0.20,
    "drops": 0.20,
    "patch": 0.20,
    "powder": 0.30,
    "suspension": 0.40,
    "sachet": 0.18,
}
DEFAULT_FREIGHT_FALLBACK_SAR = 0.30

# 규제비 (감가상각 5년 기준, 정보용)
PORT_FEE_SAR = 2.50
SFDA_REG_KRW = 5_000_000
SABER_REG_KRW = 1_500_000
REG_AMORT_YEARS = 5

# 시나리오 고정 파라미터
SCENARIO_ORDER = ["aggressive", "average", "conservative"]
SCENARIO_LABELS = {
    "aggressive": "공격적 (1위)",
    "average": "평균 (2위)",
    "conservative": "보수적 (3위)",
}
SCENARIO_PARAMS: dict[str, dict] = {
    "aggressive": {
        "rank": 1,
        "retail_base": "p75",
        "agent_commission_pct": 0.03,
        "freight_multiplier": 0.85,
    },
    "average": {
        "rank": 2,
        "retail_base": "median",
        "agent_commission_pct": 0.05,
        "freight_multiplier": 1.0,
    },
    "conservative": {
        "rank": 3,
        "retail_base": "p25",
        "agent_commission_pct": 0.10,
        "freight_multiplier": 1.2,
    },
}

_FX_FALLBACK = {"sar_krw": 392.64, "sar_usd": 0.2667, "usd_krw": 1472.0}


# ═══════════════════════════════════════════════════════════════════════════
# 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ProductClassification:
    product_kind: str = "generic"           # originator | generic | biosimilar
    is_combination: bool = False
    is_extended_release: bool = False
    premium_factor: float = 0.0             # combo 프리미엄 (ex: 0.2 = +20%)
    dosage_ratio: float = 1.0               # target_strength / competitor_strength
    freight_base_sar_per_unit: float = DEFAULT_FREIGHT_FALLBACK_SAR
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "product_kind": self.product_kind,
            "is_combination": self.is_combination,
            "is_extended_release": self.is_extended_release,
            "premium_factor": round(self.premium_factor, 4),
            "dosage_ratio": round(self.dosage_ratio, 4),
            "freight_base_sar_per_unit": round(self.freight_base_sar_per_unit, 4),
            "rationale": self.rationale,
            "warnings": list(self.warnings),
        }


# ═══════════════════════════════════════════════════════════════════════════
# 순수 계산 함수 (모두 단위 테스트 대상)
# ═══════════════════════════════════════════════════════════════════════════

def pharmacy_margin_for(retail_sar: float) -> float:
    """Retail SAR 구간별 약국 실효 마진."""
    if retail_sar < THRESHOLD_LOW_SAR:
        return PHARMACY_MARGIN_LOW
    if retail_sar < THRESHOLD_HIGH_SAR:
        return PHARMACY_MARGIN_MID
    return PHARMACY_MARGIN_HIGH


def reverse_margins_to_cif(
    retail_sar: float,
    *,
    vat_pct: float = VAT_PCT_HS3004,
    wholesaler_margin_pct: float = WHOLESALER_MARGIN_PCT,
) -> float:
    """민간 시장 Retail → CIF 복리 역산.

    Retail = CIF × (1+wholesaler) × (1+pharmacy) × (1+VAT)
    69/253 SAR 임계값에 따라 pharmacy margin이 계단형으로 달라진다.
    """
    if retail_sar is None or retail_sar <= 0:
        return 0.0
    pm = pharmacy_margin_for(retail_sar)
    compound = (1.0 + wholesaler_margin_pct) * (1.0 + pm) * (1.0 + vat_pct)
    return retail_sar / compound if compound > 0 else 0.0


def apply_generic_cap(cif_sar: float, product_kind: str) -> float:
    """제네릭/바이오시밀러의 CIF 상한을 적용한다 (오리지널 대비 할인)."""
    if cif_sar <= 0:
        return cif_sar
    pk = (product_kind or "").lower()
    if pk == "generic":
        return cif_sar * GENERIC_CIF_CAP_PCT
    if pk == "biosimilar":
        return cif_sar * BIOSIMILAR_CIF_CAP_PCT
    return cif_sar


def apply_dosage_adjustment(cif_sar: float, dosage_ratio: float) -> float:
    """함량 비율 조정. dosage_ratio = target_strength / competitor_strength.

    1.0 → 변화 없음, 0.5 → 경쟁품의 절반 용량이라 단가의 절반.
    """
    if cif_sar <= 0:
        return cif_sar
    r = dosage_ratio if dosage_ratio and dosage_ratio > 0 else 1.0
    # 극단값 방지: 0.25~4.0 범위로 clip
    r = max(0.25, min(4.0, r))
    return cif_sar * r


def apply_combination_premium(
    cif_sar: float,
    is_combination: bool,
    premium_factor: float,
) -> float:
    """복합제 프리미엄. premium_factor는 0~1 (예: 0.2 = +20%)."""
    if cif_sar <= 0 or not is_combination:
        return cif_sar
    pf = max(0.0, min(1.0, premium_factor or 0.0))
    return cif_sar * (1.0 + pf)


def deduct_freight(cif_sar: float, freight_base_sar: float, freight_mult: float) -> float:
    """CIF에서 운임을 빼 FOB 직전 값을 얻는다."""
    if cif_sar <= 0:
        return cif_sar
    fb = max(0.0, float(freight_base_sar or 0.0))
    fm = max(0.0, float(freight_mult or 1.0))
    return cif_sar - fb * fm


def deduct_agent_commission(pre_fob_sar: float, agent_commission_pct: float) -> float:
    """에이전트 수수료를 FOB에서 차감.

    계약상 'FOB의 X%'로 지급하는 구조이므로 FOB = pre_fob / (1 + agent%).
    """
    if pre_fob_sar <= 0:
        return pre_fob_sar
    pct = max(0.0, float(agent_commission_pct or 0.0))
    return pre_fob_sar / (1.0 + pct)


# ═══════════════════════════════════════════════════════════════════════════
# 경쟁가 풀 정규화
# ═══════════════════════════════════════════════════════════════════════════

def _coerce_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if v == v else None  # NaN 방지
    try:
        s = str(x).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def normalize_competitor_pool(report_data: Optional[dict]) -> list[dict]:
    """1공정 report_data에서 경쟁가(SAR) 풀을 만든다.

    우선순위: price_sar → price_comparison.same_ingredient → price_comparison.competitors.
    (trade_name, strength, price) 튜플로 중복 제거하고 양수 SAR만 유지한다.
    """
    if not isinstance(report_data, dict):
        return []

    pool: list[dict] = []

    top_price = _coerce_float(report_data.get("price_sar"))
    if top_price and top_price > 0:
        pool.append({
            "trade_name": str(report_data.get("trade_name") or "").strip(),
            "strength": str(report_data.get("strength") or "").strip(),
            "price_sar": top_price,
            "origin": "report",
        })

    pc = report_data.get("price_comparison") or {}
    if isinstance(pc, dict):
        for origin_key in ("same_ingredient", "competitors"):
            items = pc.get(origin_key) or []
            if not isinstance(items, list):
                continue
            for row in items:
                if not isinstance(row, dict):
                    continue
                price = _coerce_float(row.get("price") or row.get("price_sar"))
                if not price or price <= 0:
                    continue
                currency = str(row.get("currency") or "SAR").upper()
                if currency not in ("SAR", ""):
                    continue
                pool.append({
                    "trade_name": str(row.get("trade_name") or "").strip(),
                    "strength": str(row.get("strength") or "").strip(),
                    "price_sar": price,
                    "origin": origin_key,
                })

    seen: set[tuple[str, str, float]] = set()
    uniq: list[dict] = []
    for p in pool:
        key = (p["trade_name"].lower(), p["strength"].lower(), round(p["price_sar"], 4))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    arr = sorted(values)
    if len(arr) == 1:
        return arr[0]
    k = (len(arr) - 1) * pct
    f = int(k)
    c = min(f + 1, len(arr) - 1)
    if f == c:
        return arr[f]
    return arr[f] + (arr[c] - arr[f]) * (k - f)


def compute_competitor_stats(pool: list[dict]) -> dict:
    """경쟁가 풀의 p25/median/p75 (표본 부족 시 min/avg/max 혹은 단일값)."""
    prices = [float(p["price_sar"]) for p in pool if p.get("price_sar", 0) > 0]
    n = len(prices)
    if n == 0:
        return {"count": 0, "p25": None, "median": None, "p75": None, "mode": "empty"}
    if n == 1:
        v = prices[0]
        return {"count": 1, "p25": v, "median": v, "p75": v, "mode": "single"}
    if n == 2:
        lo, hi = sorted(prices)
        avg = (lo + hi) / 2.0
        return {"count": 2, "p25": lo, "median": avg, "p75": hi, "mode": "pair"}
    return {
        "count": n,
        "p25": _percentile(prices, 0.25),
        "median": statistics.median(prices),
        "p75": _percentile(prices, 0.75),
        "mode": "quartile",
    }


# ═══════════════════════════════════════════════════════════════════════════
# 품목 분류 (Claude 호출 + 휴리스틱 폴백)
# ═══════════════════════════════════════════════════════════════════════════

def _infer_freight_base(dosage_form: str) -> float:
    df = (dosage_form or "").lower().strip()
    if not df:
        return DEFAULT_FREIGHT_FALLBACK_SAR
    for key, val in DEFAULT_FREIGHT_BY_FORM.items():
        if key in df:
            return val
    return DEFAULT_FREIGHT_FALLBACK_SAR


_STRENGTH_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|iu|%)?", re.IGNORECASE)


def _parse_strength_mg(text: str) -> Optional[float]:
    """문자열에서 함량(mg 환산)을 추출."""
    if not text:
        return None
    m = _STRENGTH_NUM_RE.search(str(text))
    if not m:
        return None
    try:
        val = float(m.group(1))
    except (TypeError, ValueError):
        return None
    unit = (m.group(2) or "mg").lower()
    if unit == "g":
        return val * 1000.0
    if unit == "mcg":
        return val / 1000.0
    if unit in ("iu", "%"):
        return val
    return val


def _heuristic_classify(product: dict) -> ProductClassification:
    cls = ProductClassification()
    name = str(product.get("trade_name") or "").lower()
    inn = str(product.get("ingredient") or product.get("inn") or "")
    form = str(product.get("dosage_form") or "")

    # generic 기본
    cls.product_kind = "generic"
    # 복합제는 INN에 '+' 기호가 있으면 True
    cls.is_combination = "+" in inn or "/" in inn
    cls.premium_factor = 0.20 if cls.is_combination else 0.0
    # ER/CR 제형 heuristics
    lname = name + " " + form.lower()
    cls.is_extended_release = any(tok in lname for tok in (" cr", " xr", " er", " sr", "-cr", "-xr", "-er", "-sr"))
    cls.freight_base_sar_per_unit = _infer_freight_base(form)
    cls.rationale = "로컬 휴리스틱 (LLM 미사용)"
    cls.warnings.append("Claude 분류 불가 — 휴리스틱 기본값을 사용했습니다.")
    return cls


def _llm_classify(product: dict, llm: Any) -> Optional[ProductClassification]:
    """Claude로 품목 분류. 실패 시 None을 반환한다."""
    if not llm:
        return None

    prompt = f"""다음 의약품을 JSON으로 분류하세요. 숫자/불린만 사용하고 설명은 짧게.

품목:
  trade_name: {product.get('trade_name', '')}
  ingredient: {product.get('ingredient') or product.get('inn') or ''}
  strength: {product.get('strength', '')}
  dosage_form: {product.get('dosage_form', '')}

다음 JSON만 반환하세요:
{{
  "product_kind": "originator" | "generic" | "biosimilar",
  "is_combination": true | false,
  "is_extended_release": true | false,
  "premium_factor": 0~1 사이 숫자 (복합제 가치 프리미엄),
  "freight_base_sar_per_unit": 양수 숫자 (SAR/포장, 제형별 운임 추정),
  "rationale": "50자 이내 한국어 근거"
}}"""

    try:
        from llm_client import MODEL_HAIKU  # type: ignore
        resp = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=512)
        parsed = resp.parse_json()
        if not isinstance(parsed, dict):
            return None
        cls = ProductClassification(
            product_kind=str(parsed.get("product_kind") or "generic").lower(),
            is_combination=bool(parsed.get("is_combination", False)),
            is_extended_release=bool(parsed.get("is_extended_release", False)),
            premium_factor=float(parsed.get("premium_factor") or 0.0),
            freight_base_sar_per_unit=float(
                parsed.get("freight_base_sar_per_unit")
                or _infer_freight_base(str(product.get("dosage_form") or ""))
            ),
            rationale=str(parsed.get("rationale") or "")[:120],
        )
        if cls.product_kind not in ("originator", "generic", "biosimilar"):
            cls.product_kind = "generic"
            cls.warnings.append("product_kind 값이 비정상 — generic으로 보정")
        if cls.premium_factor < 0:
            cls.premium_factor = 0.0
        if cls.premium_factor > 1:
            cls.premium_factor = 1.0
        if cls.freight_base_sar_per_unit <= 0:
            cls.freight_base_sar_per_unit = _infer_freight_base(
                str(product.get("dosage_form") or "")
            )
        return cls
    except Exception as exc:
        logger.warning("Claude 분류 실패: %s", exc)
        return None


def classify_product(product: dict, llm: Any = None) -> ProductClassification:
    cls = _llm_classify(product, llm) if llm else None
    if cls is None:
        cls = _heuristic_classify(product)

    # dosage ratio는 product 자체에 target_strength가 있을 때만 의미가 있다.
    tgt = _parse_strength_mg(product.get("strength") or "")
    cmp_strength = _parse_strength_mg(product.get("competitor_strength") or "")
    if tgt and cmp_strength and cmp_strength > 0:
        cls.dosage_ratio = tgt / cmp_strength
    else:
        cls.dosage_ratio = 1.0
    return cls


# ═══════════════════════════════════════════════════════════════════════════
# 시나리오 계산
# ═══════════════════════════════════════════════════════════════════════════

def _retail_base_for(stats: dict, base_key: str) -> Optional[float]:
    if not stats or stats.get("count", 0) == 0:
        return None
    return stats.get(base_key)


def compute_scenario(
    *,
    scenario_name: str,
    retail_sar: float,
    classification: ProductClassification,
    freight_base_sar: float,
    freight_mult: float,
    agent_commission_pct: float,
    vat_pct: float = VAT_PCT_HS3004,
) -> dict:
    """단일 시나리오의 전 단계 숫자를 담은 계산 결과를 반환."""
    steps: list[dict] = []

    if retail_sar is None or retail_sar <= 0:
        return {
            "scenario": scenario_name,
            "retail_sar": None,
            "steps": [],
            "fob_sar": None,
            "error": "경쟁가 표본이 없어 retail base를 결정할 수 없습니다.",
        }

    cif_0 = reverse_margins_to_cif(retail_sar, vat_pct=vat_pct)
    steps.append({"step": "reverse_margins", "label": "Retail→CIF 역산", "value": cif_0})

    cif_1 = apply_generic_cap(cif_0, classification.product_kind)
    steps.append({"step": "generic_cap", "label": f"{classification.product_kind} cap", "value": cif_1})

    cif_2 = apply_dosage_adjustment(cif_1, classification.dosage_ratio)
    steps.append({"step": "dosage", "label": f"용량조정 ×{classification.dosage_ratio:.3f}", "value": cif_2})

    cif_3 = apply_combination_premium(cif_2, classification.is_combination, classification.premium_factor)
    steps.append({
        "step": "combo_premium",
        "label": "복합제 프리미엄" if classification.is_combination else "복합제 미적용",
        "value": cif_3,
    })

    pre_fob = deduct_freight(cif_3, freight_base_sar, freight_mult)
    steps.append({
        "step": "freight",
        "label": f"운임 차감 ({freight_base_sar:.2f}×{freight_mult:.2f} SAR)",
        "value": pre_fob,
    })

    fob = deduct_agent_commission(pre_fob, agent_commission_pct)
    steps.append({
        "step": "agent_commission",
        "label": f"에이전트 수수료 {agent_commission_pct*100:.1f}% 차감",
        "value": fob,
    })

    error = None
    if fob is None or fob <= 0:
        error = "산정 결과가 0 이하 — 운임/수수료 가정을 재검토하세요."
        fob_final: Optional[float] = None
    else:
        fob_final = fob

    return {
        "scenario": scenario_name,
        "retail_sar": retail_sar,
        "steps": steps,
        "pre_fob_sar": pre_fob,
        "fob_sar": fob_final,
        "error": error,
    }


def _serialize_step(step: dict, fx: dict) -> dict:
    v = step.get("value")
    out = {"step": step.get("step"), "label": step.get("label")}
    if v is None or v != v:
        out.update({"sar": None, "usd": None, "krw": None})
        return out
    out.update({
        "sar": round(float(v), 2),
        "usd": round(float(v) * float(fx.get("sar_usd") or 0.0), 2),
        "krw": round(float(v) * float(fx.get("sar_krw") or 0.0)),
    })
    return out


def _regulatory_cost(fx: dict) -> dict:
    sar_krw = float(fx.get("sar_krw") or _FX_FALLBACK["sar_krw"]) or 1.0
    sfda_amort_krw = SFDA_REG_KRW / REG_AMORT_YEARS
    saber_amort_krw = SABER_REG_KRW / REG_AMORT_YEARS
    return {
        "sfda_registration_krw": SFDA_REG_KRW,
        "saber_registration_krw": SABER_REG_KRW,
        "amortization_years": REG_AMORT_YEARS,
        "sfda_amort_per_year_krw": round(sfda_amort_krw),
        "saber_amort_per_year_krw": round(saber_amort_krw),
        "sfda_amort_per_year_sar": round(sfda_amort_krw / sar_krw, 2),
        "saber_amort_per_year_sar": round(saber_amort_krw / sar_krw, 2),
        "port_fee_sar": PORT_FEE_SAR,
        "note": "규제비/항만료는 FOB에서 차감하지 않고 총 수익 모델에 별도 반영합니다.",
    }


# ═══════════════════════════════════════════════════════════════════════════
# PDF 텍스트 추출 (있으면)
# ═══════════════════════════════════════════════════════════════════════════

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """pdfplumber로 텍스트 추출. 실패 시 빈 문자열."""
    if not pdf_bytes:
        return ""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        logger.warning("pdfplumber 미설치 — PDF 텍스트 추출 스킵")
        return ""
    try:
        import io
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = []
            for p in pdf.pages[:20]:
                t = p.extract_text() or ""
                if t.strip():
                    pages.append(t)
            return "\n\n".join(pages)
    except Exception as exc:
        logger.warning("PDF 추출 실패: %s", exc)
        return ""


def _pdf_text_to_report_data(pdf_text: str, llm: Any) -> Optional[dict]:
    """PDF 텍스트에서 최소한의 product/competitor 정보를 Claude로 추출."""
    if not pdf_text or not llm:
        return None

    head = pdf_text[:6000]
    prompt = f"""다음 PDF 보고서 텍스트에서 의약품 정보를 JSON으로 추출하세요.
가격(SAR)이 없으면 빈 배열로 두세요. 숫자만, 설명 없이.

### 텍스트
{head}

### JSON
{{
  "trade_name": "",
  "ingredient": "",
  "strength": "",
  "dosage_form": "",
  "price_sar": null,
  "competitors": [ {{ "trade_name": "", "strength": "", "price_sar": 0.0 }} ]
}}"""
    try:
        from llm_client import MODEL_HAIKU  # type: ignore
        resp = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=800)
        parsed = resp.parse_json()
        if not isinstance(parsed, dict):
            return None
        data = {
            "trade_name": str(parsed.get("trade_name") or "").strip(),
            "ingredient": str(parsed.get("ingredient") or "").strip(),
            "strength": str(parsed.get("strength") or "").strip(),
            "dosage_form": str(parsed.get("dosage_form") or "").strip(),
            "price_sar": _coerce_float(parsed.get("price_sar")),
            "hs_code": None,
            "price_comparison": {
                "same_ingredient": [
                    {
                        "trade_name": str(c.get("trade_name") or "").strip(),
                        "strength": str(c.get("strength") or "").strip(),
                        "price": _coerce_float(c.get("price_sar")),
                        "currency": "SAR",
                    }
                    for c in (parsed.get("competitors") or [])
                    if isinstance(c, dict) and _coerce_float(c.get("price_sar"))
                ],
                "competitors": [],
            },
        }
        return data
    except Exception as exc:
        logger.warning("PDF → report_data 변환 실패: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 상위 오케스트레이터
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_overrides(overrides_raw: Any) -> dict[str, dict]:
    """사용자가 시나리오별로 수정 가능한 파라미터만 허용."""
    if isinstance(overrides_raw, str):
        try:
            overrides_raw = json.loads(overrides_raw)
        except Exception:
            return {}
    if not isinstance(overrides_raw, dict):
        return {}
    out: dict[str, dict] = {}
    for scen in SCENARIO_ORDER:
        raw = overrides_raw.get(scen) or {}
        if not isinstance(raw, dict):
            continue
        entry: dict[str, float] = {}
        for key in ("agent_commission_pct", "freight_multiplier"):
            val = raw.get(key)
            if val is None:
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            if key == "agent_commission_pct" and not (0.0 <= fv <= 0.5):
                continue
            if key == "freight_multiplier" and not (0.0 <= fv <= 5.0):
                continue
            entry[key] = fv
        if entry:
            out[scen] = entry
    return out


def run_private_pipeline(
    *,
    report_data: Optional[dict] = None,
    pdf_bytes: Optional[bytes] = None,
    overrides: Any = None,
    exchange_rates: Optional[dict] = None,
    llm: Any = None,
) -> dict:
    """민간 시장 FOB 역산 파이프라인 오케스트레이터.

    Returns: product/classification/competitor_stats/exchange_rates/scenarios/regulatory_cost/notes.
    """
    notes: list[str] = []

    # 1) 입력 우선순위: report_data → pdf_bytes
    if not report_data and pdf_bytes:
        text = extract_pdf_text(pdf_bytes)
        if text:
            guessed = _pdf_text_to_report_data(text, llm)
            if guessed:
                report_data = guessed
                notes.append("PDF 텍스트에서 report_data를 추정했습니다. 경쟁가 표본이 적을 수 있습니다.")
            else:
                notes.append("PDF 텍스트 추출은 되었으나 구조화 실패 — Claude 키를 확인하세요.")
        else:
            notes.append("PDF 추출 실패 — pdfplumber 설치 여부와 파일 포맷을 확인하세요.")

    if not isinstance(report_data, dict) or not report_data:
        return {
            "ok": False,
            "detail": "민간 시장 분석에는 저장된 1공정 report_data 또는 해석 가능한 PDF가 필요합니다.",
            "notes": notes,
        }

    # 2) 품목 분류
    classification = classify_product(report_data, llm=llm)

    # HS 코드 경고
    hs = str(report_data.get("hs_code") or "").strip().upper()
    vat_pct = VAT_PCT_HS3004
    if hs and "3004" not in hs:
        notes.append(f"HS 코드 {hs}은(는) 3004가 아닙니다 — VAT 0%/관세 0% 가정 재확인이 필요합니다.")
    elif not hs:
        notes.append("HS 코드가 미확정입니다 — 일단 HS 3004(면세) 가정으로 계산합니다.")

    # 3) 경쟁가 풀 / 통계
    pool = normalize_competitor_pool(report_data)
    stats = compute_competitor_stats(pool)
    if stats["count"] == 0:
        return {
            "ok": False,
            "detail": "경쟁가 표본이 없습니다. 1공정 보고서에 SAR 가격이 포함되는지 확인하세요.",
            "product": {
                "trade_name": report_data.get("trade_name"),
                "ingredient": report_data.get("ingredient") or report_data.get("inn"),
            },
            "notes": notes,
        }
    if stats["count"] < 3:
        notes.append(f"경쟁가 표본이 {stats['count']}건으로 적습니다 — 시나리오 범위가 좁을 수 있습니다.")

    # 4) 환율 / 오버라이드
    fx = dict(exchange_rates or {})
    for k, v in _FX_FALLBACK.items():
        fx.setdefault(k, v)

    ov_norm = _normalize_overrides(overrides)

    # 5) 시나리오별 계산
    scenarios: list[dict] = []
    for name in SCENARIO_ORDER:
        params = dict(SCENARIO_PARAMS[name])
        ov = ov_norm.get(name, {})
        agent_pct = ov.get("agent_commission_pct", params["agent_commission_pct"])
        freight_mult = ov.get("freight_multiplier", params["freight_multiplier"])

        retail_sar = _retail_base_for(stats, params["retail_base"])
        result = compute_scenario(
            scenario_name=name,
            retail_sar=retail_sar or 0.0,
            classification=classification,
            freight_base_sar=classification.freight_base_sar_per_unit,
            freight_mult=freight_mult,
            agent_commission_pct=agent_pct,
            vat_pct=vat_pct,
        )

        sar_usd = float(fx.get("sar_usd") or _FX_FALLBACK["sar_usd"])
        sar_krw = float(fx.get("sar_krw") or _FX_FALLBACK["sar_krw"])
        fob_sar = result.get("fob_sar")
        fob_pkg = None
        if fob_sar is not None:
            fob_pkg = {
                "sar": round(fob_sar, 2),
                "usd": round(fob_sar * sar_usd, 2),
                "krw": round(fob_sar * sar_krw),
            }

        scenarios.append({
            "scenario": name,
            "label": SCENARIO_LABELS[name],
            "rank": params["rank"],
            "retail_base_key": params["retail_base"],
            "retail_sar": round(result["retail_sar"], 2) if result.get("retail_sar") else None,
            "agent_commission_pct": round(agent_pct, 4),
            "freight_multiplier": round(freight_mult, 4),
            "freight_base_sar": round(classification.freight_base_sar_per_unit, 4),
            "steps": [_serialize_step(s, fx) for s in result.get("steps", [])],
            "fob": fob_pkg,
            "port_fee_sar": PORT_FEE_SAR,
            "error": result.get("error"),
        })

    # 6) 제품/경쟁가 직렬화
    product_out = {
        "trade_name": str(report_data.get("trade_name") or "").strip(),
        "ingredient": str(report_data.get("ingredient") or report_data.get("inn") or "").strip(),
        "strength": str(report_data.get("strength") or "").strip(),
        "dosage_form": str(report_data.get("dosage_form") or "").strip(),
        "hs_code": hs or None,
    }
    competitor_stats = {
        "count": stats["count"],
        "mode": stats["mode"],
        "p25_sar": round(stats["p25"], 2) if stats["p25"] is not None else None,
        "median_sar": round(stats["median"], 2) if stats["median"] is not None else None,
        "p75_sar": round(stats["p75"], 2) if stats["p75"] is not None else None,
        "samples": [
            {
                "trade_name": p["trade_name"],
                "strength": p["strength"],
                "price_sar": round(float(p["price_sar"]), 2),
                "origin": p["origin"],
            }
            for p in pool[:15]
        ],
    }

    return {
        "ok": True,
        "product": product_out,
        "classification": classification.to_dict(),
        "competitor_stats": competitor_stats,
        "exchange_rates": {
            "sar_usd": fx.get("sar_usd"),
            "sar_krw": fx.get("sar_krw"),
            "usd_krw": fx.get("usd_krw"),
            "source": fx.get("source", "unknown"),
        },
        "scenarios": scenarios,
        "regulatory_cost": _regulatory_cost(fx),
        "notes": notes + list(classification.warnings),
    }
