"""통합 1단계: normalizer + outlier_detector 연결 시뮬레이션"""
import copy
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from normalizer import normalize_record
from outlier_detector import flag_record


def test_integrate_stage1_normalizer_outlier_chain(capsys):
    """map_to_schema -> normalize_record -> flag_record 흐름 회귀."""
    np.random.seed(42)
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}  {detail}")

    print("=== 통합 1단계: normalizer + outlier_detector ===\n")

    existing = np.random.uniform(5, 15, size=30).tolist()
    raw_record = {
        "trade_name": "Panadol Extra",
        "scientific_name": "Paracetamol",
        "strength": "500mg",
        "dosage_form": "Film-Coated Tablet",
        "price_sar": "SAR 10.50",
        "price_local": 10.50,
        "confidence": 0.92,
        "source_url": "https://sfda.gov.sa/drug/123",
        "source_tier": 1,
        "source_name": "sfda_api",
    }
    normalized = normalize_record(raw_record)
    flagged = flag_record(normalized, existing)

    check(
        "1-1 정상: 정규화 유지",
        flagged["strength"] == "500 mg" and flagged["dosage_form"] == "tablet",
    )
    check(
        "1-1 정상: 플래그 없음",
        flagged["outlier_flagged"] is False and flagged["anomaly_reason"] is None,
    )

    raw_box = dict(raw_record, price_local=280.0)
    normalized_box = normalize_record(raw_box)
    flagged_box = flag_record(normalized_box, existing)

    check("1-2 박스가격: 플래그 있음", flagged_box["outlier_flagged"] is True)
    check(
        "1-2 박스가격: reason 있음",
        flagged_box["anomaly_reason"] is not None
        and "K=" in flagged_box["anomaly_reason"],
    )

    raw_no_price = dict(raw_record)
    raw_no_price.pop("price_local")
    raw_no_price["price_local"] = None
    normalized_none = normalize_record(raw_no_price)
    flagged_none = flag_record(normalized_none, existing)

    check("1-3 가격없음: 플래그 없음", flagged_none["outlier_flagged"] is False)

    original = dict(raw_record, price_local=10.0)
    original_copy = copy.deepcopy(original)
    normalized = normalize_record(original)
    flagged = flag_record(normalized, existing)
    check("1-4 원본 불변", original == original_copy)
    check("1-4 사본 반환", flagged is not normalized)

    # 정규화가 문자열 가격을 None으로 만들면 price_sar가 남아 이상치로 갈 수 있음.
    # flag_record의 비숫자 가드는 normalize 이후에도 주입된 문자열로 검증한다.
    raw_str_price = dict(raw_record, price_local="not_a_number")
    normalized_str = normalize_record(raw_str_price)
    normalized_str = dict(normalized_str, price_local="not_a_number")
    flagged_str = flag_record(normalized_str, existing)
    check("1-5 문자열 가격: 플래그", flagged_str["outlier_flagged"] is True)
    _ar = flagged_str.get("anomaly_reason") or ""
    check("1-5 문자열 가격: reason", "not numeric" in _ar)

    flagged_first = flag_record(normalize_record(raw_record), [])
    check("1-6 첫 레코드: skip", flagged_first["outlier_flagged"] is False)

    rec = normalize_record({
        "strength": "500mg",
        "dosage_form": "SOFT GELATIN CAPSULE",
        "price_sar": "125,50",
        "confidence": 0.92,
        "price_local": 125.5,
    })
    check("1-7 strength 정규화", rec["strength"] == "500 mg")
    check("1-7 dosage_form 정규화", rec["dosage_form"] == "soft_capsule")
    check("1-7 price_sar 정규화", rec["price_sar"] == 125.50)

    print(f"\n=== 통합 1단계 결과: {passed} passed, {failed} failed ===")
    assert failed == 0, f"통합 1단계 실패: {failed}건 (통과 {passed})"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
