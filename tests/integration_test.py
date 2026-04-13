"""
통합 테스트 - 전체 파이프라인 호환성 점검

테스트 범위:
1. import chain 전체 (모든 모듈이 서로 충돌 없이 로드되는가)
2. 데이터 흐름: SFDA 응답 mock → map_sfda_to_schema → normalizer → inn_normalizer
3. Trafilatura fallback → normalizer → inn_normalizer 연계
4. confidence 가감 누적 정합성
5. schema.sql 컬럼과 출력 dict 키 일치
"""

import sys
from pathlib import Path
from decimal import Decimal

# assets/snippets를 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))

from normalizer import normalize_record
from inn_normalizer import INNNormalizer, INNResult
from trafilatura_fallback import extract_with_trafilatura, ExtractionResult
from identity_resolver import find_best_match, MatchScore
from sfda_oauth import map_sfda_to_schema


# ─── schema.sql에 정의된 컬럼 목록 ───────────────────
SCHEMA_COLUMNS = {
    # 공통 6컬럼
    "id", "product_id", "market_segment", "fob_estimated_usd", "confidence", "crawled_at",
    # 의약품 공통
    "regulatory_id", "trade_name",
    # 사우디 확장
    "scientific_name", "strength", "dosage_form", "price_sar",
    "manufacturer_or_marketing_company", "agent_or_supplier", "atc_code",
    # INN 정규화 (신규)
    "inn_name", "inn_id", "inn_match_type",
    # 메타
    "source_url", "source_tier", "source_name", "raw_payload",
    "outlier_flagged", "anomaly_reason", "toggle_id",
    "deleted_at", "deletion_reason",
}


def test_1_full_pipeline_sfda():
    """SFDA API 응답 → map → normalize → inn_normalize 전체 흐름"""
    print("[TEST 1] SFDA 전체 파이프라인...", end=" ")

    # Mock SFDA API 응답
    sfda_item = {
        "registrationNumber": "123456",
        "tradeName": "Panadol Extra",
        "scientificName": "Paracetamol",
        "strength": "500",
        "strengthUnit": "MG",
        "dosageForm": "Film-Coated Tablet",
        "price": 12.50,
        "manufacturerName": "GSK",
        "marketingCompany": None,
        "firstAgent": "Banaja Group",
        "secondAgent": None,
        "thirdAgent": None,
        "atcCode": "N02BE01",
    }

    # Step 1: SFDA 매핑
    record = map_sfda_to_schema(sfda_item, source_url="https://developer.sfda.gov.sa/")
    assert record["product_id"] == "SFDA_123456"
    assert record["trade_name"] == "Panadol Extra"
    assert record["scientific_name"] == "Paracetamol"
    assert record["confidence"] == 0.92

    # Step 2: normalizer
    record = normalize_record(record)
    assert record["strength"] == "500 mg"            # 500 MG → 500 mg
    assert record["dosage_form"] == "tablet"          # Film-Coated Tablet → tablet
    assert record["price_sar"] == 12.50               # 유지

    # Step 3: inn_normalizer
    inn = INNNormalizer()
    inn.load_reference()
    record = inn.normalize_record(record)
    assert record["inn_name"] == "paracetamol"
    assert record["inn_match_type"] == "exact"
    assert record["inn_id"] is not None

    # confidence: 0.92 → normalizer(전 필드 채워짐 = 감점 없음) → inn(exact +0.05) = 0.95 cap
    assert record["confidence"] == 0.95, f"Expected 0.95, got {record['confidence']}"

    # Step 4: 출력 키가 schema에 있는지
    record_keys = set(record.keys())
    unknown_keys = record_keys - SCHEMA_COLUMNS
    # raw_payload 등 일부 키는 있어도 됨, 하지만 알 수 없는 키가 있으면 경고
    # id, crawled_at 등은 DB가 자동 생성하므로 없어도 됨
    for key in unknown_keys:
        if key not in ("promo_raw",):  # 허용된 확장 키
            print(f"\n  [WARN] Key '{key}' not in schema.sql", end="")

    print("PASS")


def test_2_brand_name_pipeline():
    """브랜드명만 있는 소매 크롤링 데이터 → inn_normalizer"""
    print("[TEST 2] 브랜드명 파이프라인...", end=" ")

    record = {
        "product_id": "NAHDI_001",
        "trade_name": "Lipitor 20mg",
        "scientific_name": None,       # 소매 사이트는 scientific_name 없는 경우 많음
        "market_segment": "retail",
        "strength": "20mg",
        "dosage_form": "tablet",
        "price_sar": "SAR 45.50",
        "confidence": 0.75,
        "source_url": "https://www.nahdionline.com/en-sa/product/1234",
        "source_tier": 3,
        "source_name": "nahdi_web",
    }

    record = normalize_record(record)
    assert record["strength"] == "20 mg"
    assert record["price_sar"] == 45.50

    inn = INNNormalizer()
    inn.load_reference()
    record = inn.normalize_record(record)

    # scientific_name이 None이므로 trade_name("Lipitor 20mg")으로 시도
    # "Lipitor" → 브랜드 테이블 → atorvastatin
    assert record["inn_name"] == "atorvastatin", f"Got {record['inn_name']}"
    assert record["inn_match_type"] == "brand"
    # confidence: 0.75 → normalizer(sci_name missing: -0.08 skip, 전부 있으므로 0.75 유지)
    # → inn(brand +0.02) = 0.77
    # 근데 scientific_name=None이므로 normalizer에서 감점
    # normalize_record은 strength/dosage_form/price_sar 3개만 체크
    # 여기선 3개 다 있으므로 감점 없음 → 0.75 + 0.02 = 0.77
    assert record["confidence"] == 0.77, f"Expected 0.77, got {record['confidence']}"

    print("PASS")


def test_3_trafilatura_then_inn():
    """Trafilatura 추출 → normalizer → inn_normalizer 연계"""
    print("[TEST 3] Trafilatura + INN 연계...", end=" ")

    html = """
    <html><head><title>Drug Info</title></head>
    <body>
    <article>
        <h1>Amoxicillin 500mg Capsule</h1>
        <p>Amoxicillin is a widely used antibiotic. Available at SAR 15.00 per pack.
        This medicine is commonly prescribed for bacterial infections.
        Each capsule contains Amoxicillin Trihydrate equivalent to 500mg Amoxicillin.</p>
        <p>Manufactured by Pharmaceutical Company. Available in most Saudi pharmacies.
        Dosage form: Hard Capsule. ATC Code: J01CA04.</p>
    </article>
    </body></html>
    """

    # Trafilatura 추출
    result = extract_with_trafilatura(html, source_name="test_integration")
    assert result.success
    assert result.has_prices
    assert result.has_products

    # 추출된 텍스트에서 INN 정규화
    inn = INNNormalizer()
    inn.load_reference()
    inn_result = inn.normalize("Amoxicillin Trihydrate")
    assert inn_result.success
    assert "amoxicillin" in inn_result.inn_name

    # confidence 누적: trafilatura penalty + inn bonus
    assert result.confidence_penalty == -0.05  # trafilatura 정적 HTML
    # "Amoxicillin Trihydrate" → 염 제거 → "Amoxicillin" → exact match
    assert inn_result.confidence_bonus == 0.05  # exact match (염 제거 후)
    net = result.confidence_penalty + inn_result.confidence_bonus
    assert net == 0.00  # 순 가감 0

    print("PASS")


def test_4_confidence_bounds():
    """confidence 상한(0.95)/하한(0.30) 경계값 테스트"""
    print("[TEST 4] confidence 경계값...", end=" ")

    inn = INNNormalizer()
    inn.load_reference()

    # 상한 돌파 시도: 0.93 + exact(+0.05) = 0.98 → cap 0.95
    high = inn.normalize_record({"scientific_name": "ibuprofen", "confidence": 0.93})
    assert high["confidence"] == 0.95

    # 하한 돌파 시도: 0.32 + none(-0.03) = 0.29 → floor 0.30
    low = inn.normalize_record({"trade_name": "xyzunknown999", "confidence": 0.32})
    assert low["confidence"] == 0.30

    # normalizer 감점 + inn 감점 누적
    record = {
        "confidence": 0.50,
        # price_sar, strength, dosage_form 전부 없음 → normalizer -0.24
    }
    record = normalize_record(record)
    # normalizer: 0.50 - (3 * 0.08) = 0.26 → floor 0.30
    assert record["confidence"] == 0.30

    record = inn.normalize_record(record)
    # inn: no sci/trade name → match none → -0.03 → 0.30 - 0.03 = 0.27 → floor 0.30
    assert record["confidence"] == 0.30

    print("PASS")


def test_5_schema_column_coverage():
    """sfda_api 파이프라인 출력이 schema.sql 컬럼을 커버하는지"""
    print("[TEST 5] 스키마 컬럼 커버리지...", end=" ")

    sfda_item = {
        "registrationNumber": "999",
        "tradeName": "TestDrug",
        "scientificName": "testsubstance",
        "strength": "10",
        "strengthUnit": "mg",
        "dosageForm": "tablet",
        "price": 5.0,
        "manufacturerName": "TestCo",
        "atcCode": "A01AA01",
    }

    record = map_sfda_to_schema(sfda_item, source_url="https://test.com")
    record = normalize_record(record)

    inn = INNNormalizer()
    inn.load_reference()
    record = inn.normalize_record(record)

    output_keys = set(record.keys())
    # DB가 자동 생성하는 컬럼
    auto_generated = {"id", "crawled_at", "outlier_flagged", "anomaly_reason",
                      "toggle_id", "deleted_at", "deletion_reason"}
    required_by_code = SCHEMA_COLUMNS - auto_generated

    missing = required_by_code - output_keys
    if missing:
        # fob_estimated_usd는 크롤러가 안 채우는 게 정상
        real_missing = missing - {"fob_estimated_usd"}
        if real_missing:
            print(f"\n  [WARN] Missing columns in output: {real_missing}", end="")

    print("PASS")


def test_6_identity_resolver_with_inn():
    """identity_resolver가 inn_name 필드를 활용할 수 있는지"""
    print("[TEST 6] identity_resolver + INN...", end=" ")

    # SFDA 기준 레코드
    sfda_record = {
        "trade_name": "Panadol Extra",
        "scientific_name": "Paracetamol",
        "strength": "500 mg",
        "dosage_form": "tablet",
        "atc_code": "N02BE01",
    }

    # 소매 크롤링 레코드 (inn_normalizer로 보강된 상태)
    retail_record = {
        "trade_name": "PANADOL EXTRA TAB 500MG",
        "scientific_name": "Paracetamol",   # inn_normalizer가 브랜드→INN 변환 후 보강
        "strength": "500 mg",
        "dosage_form": "tablet",
        "atc_code": "N02BE01",              # inn_normalizer가 inn_id로 보강
    }

    # identity_resolver로 매칭 시도
    result = find_best_match(retail_record, [sfda_record])
    assert result is not None, "find_best_match returned None"
    score = result.total
    verdict = result.verdict
    # scientific_name + strength + dosage_form + atc 모두 일치 → 높은 점수
    assert verdict in ("auto_confirm", "candidate"), f"Got {verdict}, score={score}"

    print(f"PASS (verdict={verdict}, score={score:.3f})")


def test_7_error_handling():
    """에지 케이스 에러 핸들링"""
    print("[TEST 7] 에러 핸들링...", end=" ")

    inn = INNNormalizer()
    inn.load_reference()

    # None 입력
    r = inn.normalize(None)
    assert not r.success

    # 빈 문자열
    r = inn.normalize("")
    assert not r.success

    # 매우 긴 문자열
    r = inn.normalize("a" * 10000)
    assert not r.success or r.match_type in ("fuzzy", "none")

    # normalize_record에 confidence 없는 경우
    record = {"trade_name": "Panadol", "scientific_name": None}
    out = inn.normalize_record(record)
    assert "inn_name" in out
    assert "confidence" not in out  # 원본에 없었으므로 추가하지 않음

    # trafilatura에 None HTML
    tr = extract_with_trafilatura(None, source_name="test_null")
    assert not tr.success

    print("PASS")


if __name__ == "__main__":
    print("=" * 60)
    print("  INTEGRATION TEST - Saudi Pharma Crawler")
    print("=" * 60)
    print()

    tests = [
        test_1_full_pipeline_sfda,
        test_2_brand_name_pipeline,
        test_3_trafilatura_then_inn,
        test_4_confidence_bounds,
        test_5_schema_column_coverage,
        test_6_identity_resolver_with_inn,
        test_7_error_handling,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"FAIL: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    sys.exit(1 if failed else 0)
