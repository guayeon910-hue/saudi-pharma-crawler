"""통합 2단계: crawler_example.py 파이프라인 시뮬레이션

Supabase 없이 dry_run 모드로 전체 파이프라인 흐름을 재현.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record

np.random.seed(42)
passed = 0
failed = 0
_inn = INNNormalizer()
_inn.load_reference()


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def map_to_schema_sa(item, source_url="https://sfda.gov.sa/test"):
    """사우디 크롤러 map_to_schema 시뮬레이션"""
    return {
        "country": "SA",
        "currency": "SAR",
        "product_id": f"SA_{item.get('reg_no', 'X')}",
        "trade_name": item.get("trade_name", "Unknown"),
        "market_segment": "retail",
        "confidence": 0.92,
        "source_url": source_url,
        "source_tier": 1,
        "source_name": "sfda_api",
        "regulatory_id": item.get("reg_no"),
        "scientific_name": item.get("scientific_name"),
        "strength": item.get("strength"),
        "dosage_form": item.get("dosage_form"),
        "price_local": item.get("price"),
        "raw_payload": item,
    }


print("=== 통합 2단계: 크롤러 파이프라인 시뮬레이션 ===\n")

# ── 사우디 SFDA API 응답 시뮬레이션 (30건 정상 + 2건 이상치) ──
api_items = []
for i in range(30):
    api_items.append({
        "reg_no": f"SFDA_{i:04d}",
        "trade_name": "Panadol Extra",
        "scientific_name": "Paracetamol",
        "strength": "500mg",
        "dosage_form": "Film-Coated Tablet",
        "price": round(float(np.random.uniform(8, 15)), 2),
    })

# 이상치 1: 박스 가격 혼입
api_items.append({
    "reg_no": "SFDA_BOX1",
    "trade_name": "Panadol Extra",
    "scientific_name": "Paracetamol",
    "strength": "500mg",
    "dosage_form": "Film-Coated Tablet",
    "price": 280.0,
})

# 이상치 2: 통화 오류 (USD 가격이 그대로)
api_items.append({
    "reg_no": "SFDA_USD1",
    "trade_name": "Augmentin",
    "scientific_name": "Amoxicillin",
    "strength": "1000mg",
    "dosage_form": "tablet",
    "price": 500.0,
})

# ── 파이프라인 실행 ──
existing_prices: list[float] = []
results = []
flagged_ids = []

for item in api_items:
    record = map_to_schema_sa(item)
    record = normalize_record(record)
    record = _inn.normalize_record(record)
    record = flag_record(record, existing_prices)

    results.append(record)
    if record.get("outlier_flagged"):
        flagged_ids.append(record["product_id"])
    else:
        if record.get("price_local") is not None:
            existing_prices.append(float(record["price_local"]))

# ── 검증 ──
print("-- 파이프라인 결과 검증 --")

# 2-1. 전체 32건 처리
check("2-1 전체 처리", len(results) == 32, f"got {len(results)}")

# 2-2. 정상 30건은 플래그 없음
normal_flagged = [r for r in results[:30] if r.get("outlier_flagged")]
check("2-2 정상 30건 통과", len(normal_flagged) == 0,
      f"{len(normal_flagged)}건 오탐")

# 2-3. 박스 가격 (280 SAR) 탐지
check("2-3 박스가격 탐지", "SA_SFDA_BOX1" in flagged_ids,
      f"flagged={flagged_ids}")

# 2-4. 고가 이상치 (500 SAR) 탐지
check("2-4 고가 이상치 탐지", "SA_SFDA_USD1" in flagged_ids,
      f"flagged={flagged_ids}")

# 2-5. 정규화 적용 확인
check("2-5 strength 정규화", results[0]["strength"] == "500 mg")
check("2-5 dosage_form 정규화", results[0]["dosage_form"] == "tablet")

# 2-6. INN 매칭 확인
inn_matched = [r for r in results if r.get("inn_name") is not None]
check("2-6 INN 매칭", len(inn_matched) > 0,
      f"matched {len(inn_matched)}/{len(results)}")

# 2-7. anomaly_reason 구조
box_record = [r for r in results if r["product_id"] == "SA_SFDA_BOX1"][0]
check("2-7 anomaly_reason 존재",
      box_record["anomaly_reason"] is not None and len(box_record["anomaly_reason"]) > 10)

# 2-8. 정상 레코드는 anomaly_reason=None
check("2-8 정상 anomaly=None", results[0]["anomaly_reason"] is None)

# 2-9. existing_prices 갱신 (정상만 추가)
check("2-9 existing 갱신", len(existing_prices) == 30,
      f"got {len(existing_prices)}")

# ── 연속 배치 시뮬레이션 (2회차) ──
print("\n-- 2회차 배치 (누적 검사) --")

batch2_items = []
for i in range(10):
    batch2_items.append({
        "reg_no": f"SFDA_B2_{i:04d}",
        "trade_name": "Panadol Extra",
        "scientific_name": "Paracetamol",
        "strength": "500mg",
        "dosage_form": "Film-Coated Tablet",
        "price": round(float(np.random.uniform(8, 15)), 2),
    })
batch2_items.append({
    "reg_no": "SFDA_B2_BAD",
    "trade_name": "Panadol Extra",
    "scientific_name": "Paracetamol",
    "strength": "500mg",
    "dosage_form": "Film-Coated Tablet",
    "price": 999.0,
})

batch2_flagged = 0
for item in batch2_items:
    record = map_to_schema_sa(item)
    record = normalize_record(record)
    record = _inn.normalize_record(record)
    record = flag_record(record, existing_prices)
    if record.get("outlier_flagged"):
        batch2_flagged += 1
    else:
        if record.get("price_local"):
            existing_prices.append(float(record["price_local"]))

check("2-10 2회차 이상치 1건", batch2_flagged == 1, f"got {batch2_flagged}")
check("2-11 누적 existing 증가", len(existing_prices) == 40,
      f"got {len(existing_prices)}")

print(f"\n=== 통합 2단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("통합 2단계 통과. 3단계 진행 가능.")
