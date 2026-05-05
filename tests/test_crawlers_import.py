"""
크롤러 모듈 임포트 및 기본 구조 검증 테스트.

모든 10개 크롤러가:
  1. 정상 임포트 되는지
  2. run() 함수가 존재하는지
  3. map_*_to_schema() 함수가 존재하는지 (해당되는 경우)
  4. dry_run 모드에서 mock 데이터로 실행 가능한지
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

passed = 0
failed = 0

def check(test_id, label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {test_id} {label}")
    else:
        failed += 1
        print(f"  FAIL  {test_id} {label}")


# ─── A. 임포트 검증 ────────────────────────────────────
print("=== A. 크롤러 모듈 임포트 검증 ===\n")

crawlers_to_test = [
    "crawlers.sfda_api",
    "crawlers.sdi_sfda",
    "crawlers.sfda_drugs_list_html",
    "crawlers.sfda_companies",
    "crawlers.nupco_tenders",
    "crawlers.nahdi_web",
    "crawlers.al_dawaa_web",
    "crawlers.whites_web",
    "crawlers.tamer_group",
    "crawlers.etimad_api",
    "crawlers.noon_saudi",
]

imported_modules = {}
for i, module_name in enumerate(crawlers_to_test):
    try:
        mod = __import__(module_name, fromlist=["run"])
        imported_modules[module_name] = mod
        check(f"A1-{i+1}", f"{module_name} 임포트 성공", True)
    except Exception as e:
        check(f"A1-{i+1}", f"{module_name} 임포트 실패: {e}", False)


# ─── B. run() 함수 존재 검증 ────────────────────────────
print("\n=== B. run() 함수 존재 검증 ===\n")

for i, (module_name, mod) in enumerate(imported_modules.items()):
    has_run = hasattr(mod, "run") and callable(mod.run)
    check(f"B1-{i+1}", f"{module_name}.run() 존재", has_run)


# ─── C. sfda_companies 스키마 매핑 검증 ─────────────────
print("\n=== C. sfda_companies 스키마 매핑 검증 ===\n")

from crawlers.sfda_companies import map_company_to_schema

mock_company = {
    "companyRegister": "12345",
    "drugType": "Human",
    "country_Desc": "Saudi Arabia",
    "productionLine": "Manufacturing",
    "companY_ENG_DESC": "Test Pharma Corp",
    "companY_ADDRESS": "Riyadh, Saudi Arabia",
    "agenT_NAME": "Test Agent Co",
    "agenT_ADDRESS": "Jeddah",
}

result = map_company_to_schema(mock_company, source_url="https://www.sfda.gov.sa/en/drug-companies")
check("C1-1", f"company_id: {result['company_id']}", result["company_id"] == "SFDA_CO_12345")
check("C1-2", f"company_name: {result['company_name']}", result["company_name"] == "Test Pharma Corp")
check("C1-3", f"agent_name: {result['agent_name']}", result["agent_name"] == "Test Agent Co")
check("C1-4", f"source_name: {result['source_name']}", result["source_name"] == "sfda_companies")
check("C1-5", f"country: {result['country']}", result["country"] == "SA")
check("C1-6", f"confidence: {result['confidence']}", result["confidence"] == 0.80)
check("C1-7", "raw_payload 포함", result["raw_payload"] == mock_company)


# ─── D. nupco_tenders 파싱 검증 ─────────────────────────
print("\n=== D. nupco_tenders 파싱 검증 ===\n")

from crawlers.nupco_tenders import _parse_tender_links, _parse_tender_detail, map_tender_to_schema

mock_html = '''
<div class="tender-list">
    <a href="https://www.nupco.com/en/tenders/medical-supply-2026/">
        <h3>Medical Supply Tender 2026</h3>
    </a>
    <a href="https://www.nupco.com/en/tenders/pharma-distribution-q2/">
        Pharmaceutical Distribution Q2
    </a>
</div>
'''

tenders = _parse_tender_links(mock_html)
check("D1-1", f"텐더 링크 추출: {len(tenders)}건", len(tenders) == 2)
if tenders:
    check("D1-2", f"첫 텐더 URL", "medical-supply-2026" in tenders[0]["url"])
    check("D1-3", f"첫 텐더 제목: {tenders[0].get('title', '')[:40]}",
          "Medical Supply" in tenders[0].get("title", ""))

# 상세 페이지 파싱
mock_detail_html = '''
<html>
<h1>Medical Supply Tender 2026</h1>
<p>Posting Date: 15/03/2026</p>
<p>Closing Date: 30/04/2026</p>
<p>Tender No: NUPCO/2026/MS-001</p>
<a href="/uploads/tender_doc.pdf">Download PDF</a>
</html>
'''

detail = _parse_tender_detail(mock_detail_html, "https://www.nupco.com/en/tenders/medical-supply-2026/")
check("D2-1", f"제목: {detail.get('title', '')[:40]}", "Medical Supply" in detail.get("title", ""))
check("D2-2", f"tender_number: {detail.get('tender_number', '')}", detail.get("tender_number") is not None)

# 스키마 매핑
tender_data = {
    "url": "https://www.nupco.com/en/tenders/medical-supply-2026/",
    "title": "Medical Supply Tender 2026",
    "tender_number": "NUPCO/2026/MS-001",
}
record = map_tender_to_schema(tender_data, source_url="https://www.nupco.com/en/tenders/")
check("D3-1", f"tender_id: {record['tender_id']}", "NUPCO_" in record["tender_id"])
check("D3-2", f"market_segment: {record['market_segment']}", record["market_segment"] == "tender")
check("D3-3", f"source_tier: {record['source_tier']}", record["source_tier"] == 2)


# ─── E. nahdi_web 스키마 매핑 검증 ──────────────────────
print("\n=== E. nahdi_web 스키마 매핑 검증 ===\n")

from crawlers.nahdi_web import map_nahdi_to_schema, _parse_products_from_html

mock_product = {
    "name": "Panadol Extra 500mg Tablets",
    "price": 12.50,
    "brand": "Panadol",
    "sku": "NAH123456",
}

record = map_nahdi_to_schema(mock_product, source_url="https://www.nahdionline.com/en-sa")
check("E1-1", f"product_id: {record['product_id']}", record["product_id"] == "NAHDI_NAH123456")
check("E1-2", f"price_sar: {record['price_sar']}", record["price_sar"] == 12.50)
check("E1-3", f"source_tier: {record['source_tier']}", record["source_tier"] == 3)
check("E1-4", f"matching_required: {record.get('matching_required')}", record.get("matching_required") is True)

# JSON-LD 파싱
mock_jsonld_html = '''
<script type="application/ld+json">
{
    "@type": "Product",
    "name": "Augmentin 1g Tablets",
    "sku": "AUG1G",
    "offers": {
        "price": "45.00",
        "priceCurrency": "SAR"
    },
    "brand": {"name": "GSK"}
}
</script>
'''
products = _parse_products_from_html(mock_jsonld_html)
check("E2-1", f"JSON-LD 파싱: {len(products)}건", len(products) == 1)
if products:
    check("E2-2", f"제품명: {products[0]['name']}", products[0]["name"] == "Augmentin 1g Tablets")
    check("E2-3", f"가격: {products[0].get('price')}", products[0].get("price") == 45.0)
    check("E2-4", f"브랜드: {products[0].get('brand')}", products[0].get("brand") == "GSK")


# ─── F. main.py dispatch table 검증 ────────────────────
print("\n=== F. main.py dispatch table 검증 (stub 제거 확인) ===\n")

# main.py에서 CRAWLERS dict를 직접 검증하기 어려우므로
# 모든 소스명에 대해 import 가능한지 확인
all_sources = [
    "sfda_api", "sdi_sfda", "sfda_drugs_list_html", "sfda_companies",
    "nupco_tenders", "etimad_api", "nahdi_web",
    "al_dawaa_web", "whites_web", "tamer_group", "noon_saudi",
]

for i, name in enumerate(all_sources):
    module_name = f"crawlers.{name}"
    try:
        mod = __import__(module_name, fromlist=["run"])
        check(f"F1-{i+1}", f"{name}: run() 함수 callable", callable(mod.run))
    except Exception as e:
        check(f"F1-{i+1}", f"{name}: 임포트 실패 {e}", False)


# ─── G. etimad_api: API 키 없으면 건너뛰기 확인 ─────────
print("\n=== G. etimad_api: API 키 없을 때 동작 ===\n")

# ETIMAD_API_KEY를 제거하고 run 호출
os.environ.pop("ETIMAD_API_KEY", None)
from crawlers.etimad_api import run as etimad_run

# sb mock
class MockSB:
    def table(self, name):
        return self
    def select(self, *a, **kw):
        return self
    def eq(self, *a, **kw):
        return self
    def order(self, *a, **kw):
        return self
    def limit(self, *a, **kw):
        return self
    def execute(self):
        class R:
            data = []
        return R()

result = etimad_run(MockSB(), {"api_key": ""}, dry_run=True)
check("G1-1", "API 키 없을 때 rows_inserted=0", result["rows_inserted"] == 0)
check("G1-2", "audit_log 존재", "audit_log" in result)


print(f"\n{'='*60}")
print(f"크롤러 임포트 테스트 결과: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("전체 크롤러 임포트 및 기본 구조 검증 통과.")
else:
    print("일부 실패. 수정 필요.")
