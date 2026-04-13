"""통합 4단계: 전체 e2e + 전 단계 회귀

12개국 동시 크롤링 시나리오. 각 나라가 같은 products 테이블에 INSERT하는 상황 재현.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record, scan_group, check_outlier

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


def make_record(country, currency, trade_name, price, source_name):
    return {
        "country": country,
        "currency": currency,
        "product_id": f"{country}_{np.random.randint(10000, 99999)}",
        "trade_name": trade_name,
        "market_segment": "retail",
        "confidence": 0.92,
        "source_url": f"https://regulator.{country.lower()}/drug",
        "source_tier": 1,
        "source_name": source_name,
        "scientific_name": "Paracetamol",
        "strength": "500mg",
        "dosage_form": "Film-Coated Tablet",
        "price_local": price,
        "raw_payload": {},
    }


print("=== 통합 4단계: 12개국 e2e 시뮬레이션 ===\n")

# ── 12개국 가격 시뮬레이션 ──
countries = {
    "SA": ("SAR", "sfda_api",  8, 15),
    "SG": ("SGD", "hsa_api",   2, 8),
    "VN": ("VND", "dav_api",   5000, 15000),
    "EG": ("EGP", "eda_api",   20, 60),
    "JO": ("JOD", "jfda_api",  3, 10),
    "KW": ("KWD", "moh_kw",    1, 5),
    "AE": ("AED", "mohap_api", 10, 25),
    "BH": ("BHD", "nhra_api",  1, 4),
    "OM": ("OMR", "moh_om",    1, 5),
    "QA": ("QAR", "moph_api",  8, 20),
    "LB": ("LBP", "moph_lb",   50000, 200000),
    "IQ": ("IQD", "moh_iq",    5000, 20000),
}

# 각 나라별 existing_prices (나라별로 독립)
existing_by_country: dict[str, list[float]] = {c: [] for c in countries}
all_records: list[dict] = []
outlier_counts: dict[str, int] = {c: 0 for c in countries}

print("-- 각 나라 정상 크롤링 (30건씩) --")
for country, (currency, source, low, high) in countries.items():
    for _ in range(30):
        price = round(float(np.random.uniform(low, high)), 2)
        rec = make_record(country, currency, "Panadol Extra", price, source)
        rec = normalize_record(rec)
        rec = _inn.normalize_record(rec)
        rec = flag_record(rec, existing_by_country[country])

        all_records.append(rec)
        if rec.get("outlier_flagged"):
            outlier_counts[country] += 1
        else:
            existing_by_country[country].append(float(rec["price_local"]))

# 4-1. 12 x 30 = 360건 처리
check("4-1 전체 360건", len(all_records) == 360)

# 4-2. 정상 데이터 오탐 0
total_outliers = sum(outlier_counts.values())
check("4-2 정상 오탐 0건", total_outliers == 0,
      f"outliers by country: {outlier_counts}")

# 4-3. 각 나라 existing 30건씩
for c in countries:
    check(f"4-3 {c} existing=30", len(existing_by_country[c]) == 30,
          f"got {len(existing_by_country[c])}")

print("\n-- 각 나라에 이상치 1건씩 추가 --")
outlier_prices = {
    "SA": 500.0,    # 50x
    "SG": 200.0,    # 40x
    "VN": 1.0,      # 1/10000x
    "EG": 2000.0,   # 50x
    "JO": 300.0,    # 50x
    "KW": 200.0,    # 60x
    "AE": 800.0,    # 40x
    "BH": 150.0,    # 50x
    "OM": 200.0,    # 50x
    "QA": 600.0,    # 40x
    "LB": 5.0,      # 1/40000x
    "IQ": 1.0,      # 1/10000x
}

detected = {}
for country, outlier_price in outlier_prices.items():
    currency, source, _, _ = countries[country]
    rec = make_record(country, currency, "Panadol Extra", outlier_price, source)
    rec = normalize_record(rec)
    rec = _inn.normalize_record(rec)
    rec = flag_record(rec, existing_by_country[country])
    detected[country] = rec.get("outlier_flagged", False)

# 4-4. 12개국 전부 이상치 탐지
for country, was_detected in detected.items():
    check(f"4-4 {country} 이상치 탐지", was_detected,
          f"price={outlier_prices[country]}")

print("\n-- scan_group: 기존 데이터 배치 정리 --")
# SA에 오염 데이터 3건 추가 후 scan
sa_dirty = existing_by_country["SA"] + [500.0, 800.0, 1200.0]
outliers = scan_group(sa_dirty)
removed = [v for v, _ in outliers]

check("4-5 SA scan 3건 탐지", len(outliers) == 3,
      f"found {len(outliers)}: {removed}")
check("4-6 1200 제거", 1200.0 in removed)
check("4-7 800 제거", 800.0 in removed)
check("4-8 500 제거", 500.0 in removed)

print("\n-- 교차 오염 방지: 나라 간 독립성 --")
# SA에 이상치 추가해도 SG에 영향 없어야 함
existing_by_country["SA"].append(500.0)  # SA에 오염
sg_rec = make_record("SG", "SGD", "Panadol", 5.0, "hsa_api")
sg_rec = normalize_record(sg_rec)
sg_rec = flag_record(sg_rec, existing_by_country["SG"])
check("4-9 SG 독립성", not sg_rec.get("outlier_flagged"),
      "SA 오염이 SG에 영향 줘서는 안 됨")

print("\n-- 정규화 회귀 --")
check("4-10 strength 정규화", all_records[0]["strength"] == "500 mg")
check("4-11 dosage_form 정규화", all_records[0]["dosage_form"] == "tablet")

# INN 매칭
inn_matched = sum(1 for r in all_records if r.get("inn_name") == "paracetamol")
check("4-12 INN 매칭", inn_matched == 360, f"matched {inn_matched}/360")

print(f"\n=== 통합 4단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("전 단계 완료.")
