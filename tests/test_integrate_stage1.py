"""통합 1단계: normalizer + outlier_detector 연결 시뮬레이션"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from normalizer import normalize_record
from outlier_detector import flag_record

np.random.seed(42)
passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


print("=== 통합 1단계: normalizer + outlier_detector ===\n")

# ── 실제 파이프라인 흐름 재현 ──
# map_to_schema -> normalize_record -> flag_record -> DB

# 1-1. 정상 가격 INSERT
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

check("1-1 정상: 정규화 유지",
      flagged["strength"] == "500 mg" and flagged["dosage_form"] == "tablet")
check("1-1 정상: 플래그 없음",
      flagged["outlier_flagged"] == False and flagged["anomaly_reason"] is None)

# 1-2. 박스 가격 혼입
raw_box = dict(raw_record, price_local=280.0)
normalized_box = normalize_record(raw_box)
flagged_box = flag_record(normalized_box, existing)

check("1-2 박스가격: 플래그 있음", flagged_box["outlier_flagged"] == True)
check("1-2 박스가격: reason 있음",
      flagged_box["anomaly_reason"] is not None and "K=" in flagged_box["anomaly_reason"])

# 1-3. price_local이 None (가격 없는 레코드)
raw_no_price = dict(raw_record)
raw_no_price.pop("price_local")
raw_no_price["price_local"] = None
normalized_none = normalize_record(raw_no_price)
flagged_none = flag_record(normalized_none, existing)

check("1-3 가격없음: 플래그 없음", flagged_none["outlier_flagged"] == False)

# 1-4. 원본 불변 검증
import copy
original = dict(raw_record, price_local=10.0)
original_copy = copy.deepcopy(original)
normalized = normalize_record(original)
flagged = flag_record(normalized, existing)
check("1-4 원본 불변", original == original_copy)
check("1-4 사본 반환", flagged is not normalized)

# 1-5. price_local이 문자열 (비정상)
raw_str_price = dict(raw_record, price_local="not_a_number")
normalized_str = normalize_record(raw_str_price)
flagged_str = flag_record(normalized_str, existing)
check("1-5 문자열 가격: 플래그", flagged_str["outlier_flagged"] == True)
check("1-5 문자열 가격: reason", "not numeric" in flagged_str["anomaly_reason"])

# 1-6. 첫 레코드 (기존 가격 없음)
flagged_first = flag_record(normalize_record(raw_record), [])
check("1-6 첫 레코드: skip", flagged_first["outlier_flagged"] == False)

# 1-7. 기존 정규화 기능 회귀 확인
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
if failed > 0:
    sys.exit(1)
print("통합 1단계 통과. 2단계 진행 가능.")
