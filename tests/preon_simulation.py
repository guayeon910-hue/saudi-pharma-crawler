"""
preon 시뮬레이션 - 사우디 약품 크롤러 INN 정규화 테스트

목적:
1. preon의 3단계 매칭(exact -> partial -> fuzzy) 성능을 사우디 맥락에서 검증
2. 다국어(영어/아랍어 음역/브랜드명/오타) 케이스 분류
3. 매칭 속도 측정

preon 작동 원리:
- PrecisionOncologyNormalizer().fit(names, ids) 로 참조 DB 구축
- .query(name) → (matched_names, matched_ids, {match_type})
- match_type: exact / partial / fuzzy / none
"""

import time
import json
from dataclasses import dataclass, asdict
from typing import Optional

from preon.normalization import PrecisionOncologyNormalizer


# ─── WHO INN 참조 데이터셋 (사우디 크롤러 핵심 약물 50종) ────────
# 실제 프로덕션에서는 WHO INN 전체 목록(~12,000종)을 로드하지만
# 시뮬레이션에서는 사우디 시장 핵심 약물로 테스트한다.
INN_REFERENCE = [
    # (INN name, ID) - ID는 ATC 코드 또는 자체 식별자
    ("paracetamol", "N02BE01"),
    ("acetaminophen", "N02BE01_alt"),     # paracetamol의 미국식 이름
    ("ibuprofen", "M01AE01"),
    ("amoxicillin", "J01CA04"),
    ("amoxicillin trihydrate", "J01CA04_tri"),
    ("metformin", "A10BA02"),
    ("metformin hydrochloride", "A10BA02_hcl"),
    ("atorvastatin", "C10AA05"),
    ("atorvastatin calcium", "C10AA05_ca"),
    ("omeprazole", "A02BC01"),
    ("esomeprazole", "A02BC05"),
    ("amlodipine", "C08CA01"),
    ("amlodipine besylate", "C08CA01_bes"),
    ("losartan", "C09CA01"),
    ("losartan potassium", "C09CA01_k"),
    ("clopidogrel", "B01AC04"),
    ("aspirin", "N02BA01"),
    ("acetylsalicylic acid", "N02BA01_inn"),
    ("ciprofloxacin", "J01MA02"),
    ("azithromycin", "J01FA10"),
    ("doxycycline", "J01AA02"),
    ("prednisolone", "H02AB06"),
    ("prednisone", "H02AB07"),
    ("dexamethasone", "H02AB02"),
    ("insulin glargine", "A10AE04"),
    ("insulin lispro", "A10AB04"),
    ("levothyroxine", "H03AA01"),
    ("warfarin", "B01AA03"),
    ("enoxaparin", "B01AB05"),
    ("pantoprazole", "A02BC02"),
    ("lansoprazole", "A02BC03"),
    ("rosuvastatin", "C10AA07"),
    ("simvastatin", "C10AA01"),
    ("lisinopril", "C09AA03"),
    ("enalapril", "C09AA02"),
    ("ramipril", "C09AA05"),
    ("valsartan", "C09CA03"),
    ("telmisartan", "C09CA07"),
    ("bisoprolol", "C07AB07"),
    ("carvedilol", "C07AG02"),
    ("furosemide", "C03CA01"),
    ("hydrochlorothiazide", "C03AA03"),
    ("spironolactone", "C03DA01"),
    ("gabapentin", "N03AX12"),
    ("pregabalin", "N03AX16"),
    ("tramadol", "N02AX02"),
    ("diclofenac", "M01AB05"),
    ("celecoxib", "M01AH01"),
    ("montelukast", "R03DC03"),
    ("salbutamol", "R03AC02"),
    ("fluticasone", "R03BA05"),
]

INN_NAMES = [name for name, _ in INN_REFERENCE]
INN_IDS = [id_ for _, id_ in INN_REFERENCE]


# ─── 테스트 케이스: 사우디 크롤러가 실제 만날 변형들 ────────────
@dataclass
class TestCase:
    input_name: str           # 크롤링된 원본 이름
    expected_inn: str         # 매칭되어야 할 INN
    source: str               # 어느 소스에서 올 수 있는지
    category: str             # exact / brand / variant / typo / arabic / combo / unmatched
    description: str


TEST_CASES = [
    # ── Category 1: EXACT — 정확히 INN과 일치 ──
    TestCase("paracetamol", "paracetamol", "sfda_api", "exact",
             "INN 그대로"),
    TestCase("ibuprofen", "ibuprofen", "sfda_api", "exact",
             "INN 그대로"),
    TestCase("metformin", "metformin", "sfda_api", "exact",
             "INN 그대로"),
    TestCase("omeprazole", "omeprazole", "sfda_api", "exact",
             "INN 그대로"),

    # ── Category 2: BRAND → INN — 브랜드명에서 INN 매칭 ──
    TestCase("Panadol", "paracetamol", "nahdi_web", "brand",
             "브랜드명 Panadol → paracetamol"),
    TestCase("Lipitor", "atorvastatin", "al_dawaa_web", "brand",
             "브랜드명 Lipitor → atorvastatin"),
    TestCase("Nexium", "esomeprazole", "nahdi_web", "brand",
             "브랜드명 Nexium → esomeprazole"),
    TestCase("Plavix", "clopidogrel", "whites_web", "brand",
             "브랜드명 Plavix → clopidogrel"),
    TestCase("Glucophage", "metformin", "nahdi_web", "brand",
             "브랜드명 Glucophage → metformin"),
    TestCase("Ventolin", "salbutamol", "nahdi_web", "brand",
             "브랜드명 Ventolin → salbutamol"),

    # ── Category 3: VARIANT — 대소문자, 염, 수화물 변형 ──
    TestCase("PARACETAMOL", "paracetamol", "sfda_api", "variant",
             "전부 대문자"),
    TestCase("Paracetamol", "paracetamol", "nahdi_web", "variant",
             "첫글자 대문자"),
    TestCase("amoxicillin trihydrate", "amoxicillin", "sfda_api", "variant",
             "수화물 포함"),
    TestCase("Atorvastatin Calcium", "atorvastatin", "sfda_api", "variant",
             "염 포함 (Calcium)"),
    TestCase("Losartan Potassium 50mg", "losartan", "nahdi_web", "variant",
             "염 + 함량 포함"),
    TestCase("metformin hcl", "metformin", "al_dawaa_web", "variant",
             "HCL 약어"),
    TestCase("amlodipine besylate", "amlodipine", "sfda_api", "variant",
             "besylate 염"),
    TestCase("Acetylsalicylic Acid", "acetylsalicylic acid", "sfda_api", "variant",
             "aspirin의 INN (대소문자 변형)"),

    # ── Category 4: TYPO — 오타/철자 오류 ──
    TestCase("paracetamole", "paracetamol", "nahdi_web", "typo",
             "끝에 e 추가 (흔한 오타)"),
    TestCase("amoxicilin", "amoxicillin", "al_dawaa_web", "typo",
             "l 하나 빠짐"),
    TestCase("ibuprofin", "ibuprofen", "whites_web", "typo",
             "e→i 오타"),
    TestCase("atorvastain", "atorvastatin", "nahdi_web", "typo",
             "t 빠짐"),
    TestCase("omeprazol", "omeprazole", "al_dawaa_web", "typo",
             "끝 e 빠짐"),
    TestCase("ciprofloxacine", "ciprofloxacin", "nahdi_web", "typo",
             "끝에 e 추가"),

    # ── Category 5: ARABIC TRANSLITERATION — 아랍어 음역 ──
    TestCase("barasitamol", "paracetamol", "nahdi_web", "arabic",
             "아랍어 음역 باراسيتامول → barasitamol"),
    TestCase("amoksisilin", "amoxicillin", "al_dawaa_web", "arabic",
             "아랍어 음역 أموكسيسيلين"),
    TestCase("ibubrofin", "ibuprofen", "whites_web", "arabic",
             "아랍어 음역 إيبوبروفين"),
    TestCase("metformine", "metformin", "nahdi_web", "arabic",
             "프랑스어/아랍어 변형"),
    TestCase("asitaminofen", "acetaminophen", "nahdi_web", "arabic",
             "아랍어 음역 أسيتامينوفين → acetaminophen"),

    # ── Category 6: COMBINATION — 복합제 ──
    TestCase("amoxicillin clavulanate", "amoxicillin", "sfda_api", "combo",
             "복합제 (amox + clavulanic acid)"),
    TestCase("losartan hydrochlorothiazide", "losartan", "sfda_api", "combo",
             "복합제 (losartan + HCTZ)"),
    TestCase("amlodipine valsartan", "amlodipine", "sfda_api", "combo",
             "복합제"),

    # ── Category 7: UNMATCHED — 매칭 불가 (preon이 거부해야 하는 것) ──
    TestCase("Vitamin D3", "NONE", "nahdi_web", "unmatched",
             "비타민 — INN 아님"),
    TestCase("Omega 3 Fish Oil", "NONE", "whites_web", "unmatched",
             "건강기능식품 — INN 아님"),
    TestCase("hand sanitizer", "NONE", "nahdi_web", "unmatched",
             "의약품 아님"),
    TestCase("facial cream SPF50", "NONE", "whites_web", "unmatched",
             "화장품 — INN 아님"),
]


@dataclass
class SimResult:
    input_name: str
    expected_inn: str
    source: str
    category: str
    description: str
    # preon results
    matched_names: list = None
    matched_ids: list = None
    match_type: str = "none"
    # analysis
    verdict: str = "FAIL"       # SUCCESS / PARTIAL / FAIL / TRUE_NEGATIVE
    elapsed_ms: float = 0.0
    notes: str = ""


def run_simulation():
    print("=" * 70)
    print("  PREON SIMULATION - Saudi Pharma INN Normalizer")
    print(f"  Reference DB: {len(INN_NAMES)} INN entries")
    print(f"  Test cases: {len(TEST_CASES)}")
    print("=" * 70)

    # ── Step 1: preon 모델 학습 ──
    print("\n>>> Fitting preon normalizer...")
    fit_start = time.time()
    normalizer = PrecisionOncologyNormalizer(enable_warnings=False)
    normalizer.fit(INN_NAMES, INN_IDS)
    fit_time = time.time() - fit_start
    print(f"    Fit completed in {fit_time:.2f}s")

    # ── Step 2: 전체 테스트 실행 ──
    results: list[SimResult] = []

    for i, tc in enumerate(TEST_CASES):
        r = SimResult(
            input_name=tc.input_name,
            expected_inn=tc.expected_inn,
            source=tc.source,
            category=tc.category,
            description=tc.description,
        )

        start = time.time()
        try:
            matched_names, matched_ids, info = normalizer.query(
                tc.input_name,
                match_type="all",
                threshold=0.3,     # fuzzy threshold
                n_grams=2,
            )
            r.matched_names = matched_names if matched_names else []
            r.matched_ids = matched_ids if matched_ids else []
            r.match_type = info.get("match_type", "none") if info else "none"
        except Exception as e:
            r.notes = f"Error: {type(e).__name__}: {str(e)[:100]}"
            r.match_type = "error"

        r.elapsed_ms = (time.time() - start) * 1000

        # ── 판정 ──
        if tc.category == "unmatched":
            # 매칭 안 되어야 정상
            if not r.matched_names or r.match_type == "none":
                r.verdict = "TRUE_NEGATIVE"
            else:
                r.verdict = "FALSE_POSITIVE"
                r.notes = f"Should not match but got: {r.matched_names}"
        else:
            if r.matched_names:
                # 매칭된 이름 중 expected_inn이 포함되는지
                matched_lower = [n.lower() for n in r.matched_names]
                expected_lower = tc.expected_inn.lower()

                if expected_lower in matched_lower:
                    r.verdict = "SUCCESS"
                elif any(expected_lower in m for m in matched_lower):
                    r.verdict = "SUCCESS"
                    r.notes = "Partial name match within result"
                elif any(m in expected_lower for m in matched_lower):
                    r.verdict = "SUCCESS"
                    r.notes = "Result is substring of expected"
                else:
                    r.verdict = "PARTIAL"
                    r.notes = f"Matched {r.matched_names} instead of {tc.expected_inn}"
            else:
                r.verdict = "FAIL"
                r.notes = "No match found"

        results.append(r)

    # ── Step 3: 결과 출력 ──
    categories = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = []
        categories[r.category].append(r)

    for cat_name, cat_results in categories.items():
        print(f"\n{'='*70}")
        print(f"  Category: {cat_name.upper()}")
        print(f"{'='*70}")
        for r in cat_results:
            icon = {
                "SUCCESS": "+", "PARTIAL": "~",
                "FAIL": "X", "TRUE_NEGATIVE": "O",
                "FALSE_POSITIVE": "!",
            }.get(r.verdict, "?")
            print(f"  [{icon}] {r.verdict:14s} | {r.input_name:35s} -> {r.match_type:8s} | {r.matched_names or []}")
            if r.notes:
                print(f"       {r.notes}")

    # ── Step 4: 요약 ──
    total = len(results)
    successes = [r for r in results if r.verdict == "SUCCESS"]
    partials = [r for r in results if r.verdict == "PARTIAL"]
    fails = [r for r in results if r.verdict == "FAIL"]
    true_negs = [r for r in results if r.verdict == "TRUE_NEGATIVE"]
    false_pos = [r for r in results if r.verdict == "FALSE_POSITIVE"]

    avg_ms = sum(r.elapsed_ms for r in results) / total if total else 0

    print(f"\n{'='*70}")
    print(f"  SIMULATION SUMMARY")
    print(f"{'='*70}")
    print(f"  + SUCCESS        : {len(successes):3d}/{total}")
    print(f"  ~ PARTIAL        : {len(partials):3d}/{total}")
    print(f"  X FAIL           : {len(fails):3d}/{total}")
    print(f"  O TRUE_NEGATIVE  : {len(true_negs):3d}/{total}")
    print(f"  ! FALSE_POSITIVE : {len(false_pos):3d}/{total}")
    print(f"  Avg latency      : {avg_ms:.1f}ms per query")
    print(f"  Fit time         : {fit_time:.2f}s")

    # 카테고리별 성공률
    print(f"\n  -- Category Breakdown --")
    for cat_name in ["exact", "brand", "variant", "typo", "arabic", "combo", "unmatched"]:
        cat_rs = categories.get(cat_name, [])
        if not cat_rs:
            continue
        good = sum(1 for r in cat_rs if r.verdict in ("SUCCESS", "TRUE_NEGATIVE"))
        print(f"  {cat_name:12s}: {good}/{len(cat_rs)} ({100*good/len(cat_rs):.0f}%)")

    # JSON 저장
    json_results = [asdict(r) for r in results]
    output_path = "tests/preon_simulation_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(json_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Full results saved to: {output_path}")

    return results


if __name__ == "__main__":
    run_simulation()
