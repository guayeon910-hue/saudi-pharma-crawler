"""
normalizer.py — 원시 의약품 데이터 정규화

소스별로 포맷이 제각각인 strength/dosage_form/price_sar/scientific_name을
saudi_products 테이블에 넣기 전에 표준화한다.

핵심 규칙 (schema.md 6절 기반):
- 함량: 숫자+단위 사이 공백 1개, 단위 소문자, 복합제는 " + " 구분
- 제형: 사전 기반 매핑 (15종)
- 가격: 통화기호 제거, 소수점 `.` 통일
- 성분명: 아랍 숫자 → 서아라비아 숫자, NFKC 정규화
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation


# ─── PII 경량 마스킹 (지정된 3개 필드에만 적용) ─────────
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b",
)
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)?\d{3,4}[\s-]?\d{4}(?![A-Za-z0-9-])",
)
_IQAMA_RE = re.compile(r"\b[12]\d{9}\b")


def _redact_pii(text: str | None) -> str | None:
    if not text:
        return text
    out = _EMAIL_RE.sub("[REDACTED]", text)
    out = _PHONE_RE.sub("[REDACTED]", out)
    out = _IQAMA_RE.sub("[REDACTED]", out)
    return out


# ─── Completeness(채움률) 기반 confidence 감점 ─────────
def _is_missing_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _apply_completeness_penalty(record: dict, base_confidence: float) -> float:
    """핵심 필드 결측 시 confidence 감점 (하한 0.30 방어)."""
    penalty_per_field = 0.08  # 0.05 ~ 0.10 범위 내 고정값
    floor = 0.30

    missing = 0
    if _is_missing_value(record.get("price_sar")):
        missing += 1
    if _is_missing_value(record.get("strength")):
        missing += 1
    if _is_missing_value(record.get("dosage_form")):
        missing += 1

    adjusted = base_confidence - (missing * penalty_per_field)
    if adjusted < floor:
        return floor
    return adjusted


# ─── 1. 함량 정규화 ────────────────────────────────────
_UNIT_ALIASES: dict[str, str] = {
    "mg": "mg", "MG": "mg", "Mg": "mg", "milligram": "mg", "milligrams": "mg",
    "g":  "g",  "G":  "g",  "gram": "g",  "grams": "g",
    "kg": "kg",
    "mcg": "mcg", "µg": "mcg", "ug": "mcg", "microgram": "mcg",
    "ml": "ml",  "mL": "ml",  "ML": "ml",  "milliliter": "ml",
    "l":  "l",   "L":  "l",
    "iu": "iu",  "IU": "iu",  "U": "iu", "units": "iu",
    "%":  "%",
}

# 예: "500mg", "500 mg", "٥٠٠ مجم"
_STRENGTH_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-zA-Zµ%]+)",
    re.UNICODE,
)

# 복합제 구분자: +, /, ,, "and", "&"
_COMBO_SPLIT_RE = re.compile(r"\s*(?:[+/,]|\band\b|&)\s*", re.IGNORECASE)


def _arabic_digits_to_ascii(text: str) -> str:
    """아랍 숫자 ٠١٢٣٤٥٦٧٨٩ → 0123456789"""
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return text.translate(trans)


def _clean_number(num_raw: str) -> str:
    """'500.00' → '500', '0.50' → '0.5', '500' → '500'"""
    try:
        d = Decimal(num_raw)
    except InvalidOperation:
        return num_raw
    # 정수면 integer string
    if d == d.to_integral_value():
        return str(int(d))
    # 소수면 trailing 0만 제거 (정수 부분은 보존)
    s = f"{d:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def normalize_strength(raw: str | None) -> str | None:
    """'500mg' → '500 mg', '٥٠٠ مجم' → '500 mg', '500/125mg' → '500 mg + 125 mg'

    복합제 파싱 규칙:
    - '500/125mg' 처럼 앞 토큰에 단위가 없으면 뒤 토큰의 단위를 전파한다
    - '500mg/125mg' 처럼 각자 단위가 있으면 그대로
    """
    if not raw:
        return None

    text = unicodedata.normalize("NFKC", raw).strip()
    text = _arabic_digits_to_ascii(text)

    parts = [p.strip() for p in _COMBO_SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return None

    # 각 파트 파싱: (num_str_or_None, unit_or_None, original)
    parsed: list[tuple[str | None, str | None, str]] = []
    _NUMBER_ONLY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
    for part in parts:
        match = _STRENGTH_RE.search(part)
        if match:
            num_str = _clean_number(match.group("num").replace(",", "."))
            unit_raw = match.group("unit")
            unit = _UNIT_ALIASES.get(unit_raw, unit_raw.lower())
            parsed.append((num_str, unit, part))
            continue
        # 숫자만 있는 파트 (예: '500/125mg' 의 '500') — 단위는 뒤에서 전파
        num_only = _NUMBER_ONLY_RE.match(part)
        if num_only:
            num_str = _clean_number(num_only.group(1).replace(",", "."))
            parsed.append((num_str, None, part))
            continue
        parsed.append((None, None, part))

    # 단위 전파: 뒤에서 앞으로 — 앞 토큰에 단위 없으면 뒤 토큰 단위 차용
    last_unit: str | None = None
    for i in range(len(parsed) - 1, -1, -1):
        num, unit, orig = parsed[i]
        if unit:
            last_unit = unit
        elif num and last_unit:
            parsed[i] = (num, last_unit, orig)

    # 포맷
    rendered: list[str] = []
    for num, unit, orig in parsed:
        if num and unit:
            rendered.append(f"{num} {unit}")
        else:
            rendered.append(orig)

    return " + ".join(rendered)


# ─── 2. 제형 정규화 ────────────────────────────────────
_DOSAGE_FORM_MAP: dict[str, str] = {
    # tablet 계열
    "tablet": "tablet",
    "tablets": "tablet",
    "tab": "tablet",
    "tab.": "tablet",
    "film-coated tablet": "tablet",
    "film coated tablet": "tablet",
    "sugar-coated tablet": "tablet",
    "chewable tablet": "tablet",
    "effervescent tablet": "tablet",
    "orodispersible tablet": "tablet",
    # capsule
    "capsule": "capsule",
    "capsules": "capsule",
    "cap": "capsule",
    "cap.": "capsule",
    "hard capsule": "capsule",
    "soft capsule": "soft_capsule",
    "soft gelatin capsule": "soft_capsule",
    "softgel": "soft_capsule",
    # 주사/주입
    "injection": "injection",
    "injection solution": "injection",
    "inj": "injection",
    "inj.": "injection",
    "lyophilized powder for injection": "injection",
    "powder for injection": "injection",
    "infusion": "infusion",
    "solution for infusion": "infusion",
    "pfs": "prefilled_syringe",
    "prefilled syringe": "prefilled_syringe",
    "pre-filled syringe": "prefilled_syringe",
    # 경구 액제
    "syrup": "syrup",
    "oral solution": "solution",
    "oral suspension": "suspension",
    "suspension": "suspension",
    # 외용
    "cream": "cream",
    "ointment": "ointment",
    "gel": "gel",
    "lotion": "lotion",
    # 점적제
    "eye drops": "drops",
    "ear drops": "drops",
    "nasal drops": "drops",
    "drops": "drops",
    # 흡입/패치/좌제/기타
    "inhaler": "inhaler",
    "metered-dose inhaler": "inhaler",
    "dry powder inhaler": "inhaler",
    "pressurised inhalation": "inhaler",
    "pressurized inhalation": "inhaler",
    "inhalation powder": "inhaler",
    "inhalation solution": "inhaler",
    "inhalation": "inhaler",
    "patch": "patch",
    "transdermal patch": "patch",
    "suppository": "suppository",
    "powder": "powder",
    "pouch": "pouch",
    "sachet": "pouch",
    "solution": "solution",
}

_VALID_FORMS = set(_DOSAGE_FORM_MAP.values())
_VALID_FORMS.add("prefilled_syringe")
_VALID_FORMS.add("pouch")


def normalize_dosage_form(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().lower()
    text = re.sub(r"\s+", " ", text)

    # 정확 매칭 먼저
    if text in _DOSAGE_FORM_MAP:
        return _DOSAGE_FORM_MAP[text]

    # 부분 매칭 (긴 키부터)
    for key in sorted(_DOSAGE_FORM_MAP.keys(), key=len, reverse=True):
        if key in text:
            return _DOSAGE_FORM_MAP[key]

    # 마지막 수단: 이미 정규화된 값인지 확인
    compact = text.replace(" ", "_")
    if compact in _VALID_FORMS:
        return compact

    return None  # 매칭 실패 시 null — Normalizer 레이어에서 로그


# ─── 3. 가격 정규화 ────────────────────────────────────
_PRICE_CLEAN_RE = re.compile(r"[^\d.,]")


def normalize_price_sar(raw: str | float | int | None) -> Decimal | None:
    """통화 기호 제거, 소수점 통일, Decimal 반환.

    - 'SAR 125.50', '125,50 ر.س', '﷼125.5' → Decimal('125.50')
    - None, 빈 문자열, 0, 음수는 None 반환 (음수는 비정상)
    """
    if raw is None or raw == "":
        return None

    if isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        text = str(raw).strip()
        text = _arabic_digits_to_ascii(text)
        text = _PRICE_CLEAN_RE.sub("", text)

        # 유럽식 ',' 소수점 처리
        # (1,234.56 → 1234.56, 1.234,56 → 1234.56)
        if "," in text and "." in text:
            # 둘 다 있으면 뒤쪽이 소수점
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            # 콤마만 → 소수점으로 해석 (SAR는 보통 . 쓰지만 보수적)
            text = text.replace(",", ".")

        if not text:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None

    if value <= 0:
        return None
    return value.quantize(Decimal("0.01"))


# ─── 4. 성분명 정규화 ──────────────────────────────────
def normalize_scientific_name(raw: str | None) -> str | None:
    """NFKC + 아랍 숫자 변환 + 앞뒤 공백 제거. 대소문자는 원문 유지."""
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", raw)
    text = _arabic_digits_to_ascii(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# ─── 5. 통합 엔트리포인트 ──────────────────────────────
def normalize_record(record: dict) -> dict:
    """saudi_products 삽입 전 마지막 정규화.

    원본을 수정하지 않고 사본을 반환.
    """
    out = dict(record)
    if "strength" in out:
        out["strength"] = normalize_strength(out.get("strength"))
    if "dosage_form" in out:
        out["dosage_form"] = normalize_dosage_form(out.get("dosage_form"))
    if "price_sar" in out:
        price = normalize_price_sar(out.get("price_sar"))
        out["price_sar"] = float(price) if price is not None else None
    if "price_local" in out:
        price_local = normalize_price_sar(out.get("price_local"))
        out["price_local"] = float(price_local) if price_local is not None else None
    if "scientific_name" in out:
        out["scientific_name"] = normalize_scientific_name(out.get("scientific_name"))

    # PII 마스킹: raw_payload/전체 record 순회 금지. 지정된 3개 필드만 처리.
    for key in ("manufacturer_or_marketing_company", "agent_or_supplier", "promo_raw"):
        if key in out and isinstance(out[key], str):
            out[key] = _redact_pii(out[key])

    # completeness 기반 confidence 감점 (identity_resolver 독립)
    if "confidence" in out:
        try:
            base_conf = float(out.get("confidence") or 0.0)
        except (TypeError, ValueError):
            base_conf = 0.0
        out["confidence"] = _apply_completeness_penalty(out, base_conf)

    return out


# ─── 자가 테스트 ───────────────────────────────────────
if __name__ == "__main__":
    import copy
    import math

    # ─── 기존 테스트: strength ─────────────────────────
    assert normalize_strength("500mg") == "500 mg"
    assert normalize_strength("500 MG") == "500 mg"
    assert normalize_strength("500/125mg") == "500 mg + 125 mg"
    assert normalize_strength("٥٠٠ mg") == "500 mg"
    assert normalize_strength("") is None
    assert normalize_strength(None) is None

    # ─── 기존 테스트: dosage_form ───────────────────────
    assert normalize_dosage_form("Film-Coated Tablet") == "tablet"
    assert normalize_dosage_form("SOFT GELATIN CAPSULE") == "soft_capsule"
    assert normalize_dosage_form("lyophilized powder for injection") == "injection"
    assert normalize_dosage_form("unknown form xyz") is None
    assert normalize_dosage_form("") is None
    assert normalize_dosage_form(None) is None

    # ─── 기존 테스트: price_sar ─────────────────────────
    assert normalize_price_sar("SAR 125.50") == Decimal("125.50")
    assert normalize_price_sar("125,50") == Decimal("125.50")
    assert normalize_price_sar("1,234.56") == Decimal("1234.56")
    assert normalize_price_sar("0") is None
    assert normalize_price_sar(-5) is None
    assert normalize_price_sar("") is None
    assert normalize_price_sar(None) is None

    # ─── Deep Immutability(원본 불변) 검증 ──────────────
    original = {
        "trade_name": "TestDrug",
        "scientific_name": "Paracetamol",
        "strength": "500mg",
        "dosage_form": "Film-Coated Tablet",
        "price_sar": "SAR 125.50",
        "confidence": 0.80,
        "raw_payload": {
            "nested": {"a": 1, "b": [1, 2, 3]},
            "text": "leave me",
        },
        "manufacturer_or_marketing_company": "ACME contact test@acme.com",
        "agent_or_supplier": "Agent iqama 1234567890",
        "promo_raw": "Call +966 55 123 4567",
    }
    original_before = copy.deepcopy(original)
    normalized = normalize_record(original)
    assert original == original_before, "normalize_record() must not mutate the original (deep)"
    assert normalized is not original
    assert normalized.get("raw_payload") == original_before.get("raw_payload")

    # ─── Completeness penalty: 다양한 결측 형태 ─────────
    # 1) None 결측
    rec_none = {
        "confidence": 0.80,
        "price_sar": None,
        "strength": "500 mg",
        "dosage_form": "tablet",
    }
    out_none = normalize_record(rec_none)
    assert math.isclose(float(out_none["confidence"]), 0.72, rel_tol=0.0, abs_tol=1e-9)

    # 2) 빈 문자열 결측
    rec_empty = {
        "confidence": 0.80,
        "price_sar": 125.5,
        "strength": "",
        "dosage_form": "tablet",
    }
    out_empty = normalize_record(rec_empty)
    assert math.isclose(float(out_empty["confidence"]), 0.72, rel_tol=0.0, abs_tol=1e-9)

    # 3) 키 자체 없음(결측) — price_sar/strength/dosage_form 전부 absent
    rec_missing_keys = {"confidence": 0.80, "trade_name": "X"}
    out_missing_keys = normalize_record(rec_missing_keys)
    assert math.isclose(float(out_missing_keys["confidence"]), 0.56, rel_tol=0.0, abs_tol=1e-9)

    # 하한선 0.30 방어
    rec_floor = {"confidence": 0.35, "trade_name": "X"}
    out_floor = normalize_record(rec_floor)
    assert math.isclose(float(out_floor["confidence"]), 0.30, rel_tol=0.0, abs_tol=1e-9)

    # ─── PII Boundary bypass 검증 ───────────────────────
    # 단독 10자리(1로 시작) → 마스킹
    pii_record = {
        "manufacturer_or_marketing_company": "Iqama 1234567890",
        "agent_or_supplier": "Email a@b.co",
        "promo_raw": "Call +1 555 123 4567",
    }
    pii_out = normalize_record(pii_record)
    assert "[REDACTED]" in pii_out["manufacturer_or_marketing_company"]
    assert "[REDACTED]" in pii_out["agent_or_supplier"]
    assert "[REDACTED]" in pii_out["promo_raw"]

    # 문자 결합(앞에 ID) → \b 경계로 인해 제외(원문 보존)
    bypass_record_1 = {"agent_or_supplier": "ID1234567890"}
    bypass_out_1 = normalize_record(bypass_record_1)
    assert bypass_out_1["agent_or_supplier"] == "ID1234567890"

    # 하이픈 등으로 끊긴 숫자 → 연속 10자리 조건 불만족으로 제외(원문 보존)
    bypass_record_2 = {"agent_or_supplier": "1234-5678-90"}
    bypass_out_2 = normalize_record(bypass_record_2)
    assert bypass_out_2["agent_or_supplier"] == "1234-5678-90"

    print("normalizer self-tests passed (extended cases)")
