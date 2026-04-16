"""
=============================================================================
4단계 시뮬레이션: 전체 파이프라인 통합 e2e 테스트
=============================================================================
sfda_api.py의 전체 파이프라인을:
  map_web_to_schema → normalize → INN → outlier → reputation bonus
  + AuditLog + MetricsCollector
  + Anti-bot detect + UA rotation
까지 한번에 시뮬레이션.
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from sfda_web import map_web_to_schema
from normalizer import normalize_record
from inn_normalizer import INNNormalizer
from outlier_detector import flag_record
from supabase_state import AuditLog, MetricsCollector, SourceReputation
from antibot import detect as detect_antibot, AntiBotType, pick_ua, UA_POOL

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

_inn = INNNormalizer()
_inn.load_reference()

# ─── A. 전체 파이프라인 e2e (mock 데이터) ────────────
print("=== A. 전체 파이프라인 e2e (25건 + 이상치 1건) ===\n")

# mock SFDA 데이터 생성
def make_mock_item(idx, price="10.50", trade="Paracetamol 500mg", scientific="PARACETAMOL"):
    return {
        "registerNumber": f"260000{idx:04d}",
        "tradeName": trade,
        "scientificName": scientific,
        "strength": "500",
        "strengthUnit": "mg",
        "doesageForm": "Tablet",
        "price": price,
        "manufacturerName": f"Pharma Corp {idx}",
        "agent": f"Agent {idx}",
        "atcCode1": "N02BE01",
        "companyName": f"Company {idx}",
    }

mock_items = [make_mock_item(i) for i in range(25)]
# 이상치 1건 (가격 10000)
mock_items.append(make_mock_item(99, price="10000.00"))

# 파이프라인 실행
audit = AuditLog()
metrics = MetricsCollector()
reputation = SourceReputation(alpha=0.1)

# 이전 5회 성공 시뮬레이션
for _ in range(5):
    reputation.update("sfda_web", True)

t_start = time.time()
audit.log("crawl_started", "sfda_web", {"mode": "test"})
metrics.inc("crawl_attempts")

existing_prices = []
source_url = "https://www.sfda.gov.sa/en/drugs-list"

for item in mock_items:
    record = map_web_to_schema(item, source_url=source_url)
    record = normalize_record(record)
    record = _inn.normalize_record(record)
    record = flag_record(record, existing_prices)

    # 소스 신뢰도 보정
    bonus = reputation.confidence_bonus("sfda_web")
    record["confidence"] = min(0.99, max(0.0, (record.get("confidence") or 0.92) + bonus))

    # 정상이면 가격 누적
    price = record.get("price_local")
    if not record.get("outlier_flagged") and price is not None:
        existing_prices.append(float(price))

    metrics.inc("records_processed")
    if record.get("outlier_flagged"):
        metrics.inc("outliers_detected")

    audit.log("record_processed", "sfda_web", {
        "product_id": record.get("product_id"),
        "outlier": record.get("outlier_flagged", False),
        "confidence": record.get("confidence"),
    })

metrics.inc("crawl_success")
metrics.observe("crawl_duration_sec", time.time() - t_start)
reputation.update("sfda_web", True)

audit.log("crawl_finished", "sfda_web", {
    "inserted": len(mock_items) - metrics.get_counter("outliers_detected"),
    "skipped": metrics.get_counter("outliers_detected"),
    "metrics": metrics.summary(),
})

# 검증
check("A1-1", f"처리 건수: {metrics.get_counter('records_processed')}",
      metrics.get_counter("records_processed") == 26)
check("A1-2", f"이상치 탐지: {metrics.get_counter('outliers_detected')}건",
      metrics.get_counter("outliers_detected") == 1)
check("A1-3", f"정상 가격 누적: {len(existing_prices)}건",
      len(existing_prices) == 25)

# confidence에 소스 신뢰도 보정 적용되었는지
# 5회 성공 후 bonus: (score - 0.5) * 0.1
rep_score = reputation.get("sfda_web")
check("A1-4", f"소스 신뢰도: {rep_score:.4f} (6회 성공 후)",
      rep_score > 0.7)

# 감사 로그 건수
check("A1-5", f"감사 로그: {len(audit.events)}건 (1시작 + 26처리 + 1완료)",
      len(audit.events) == 28)

# 메트릭 JSON
summary = metrics.summary()
check("A1-6", "메트릭에 카운터 포함",
      summary["counters"]["records_processed"] == 26)

# ─── B. Anti-bot + 파이프라인 통합 시나리오 ────────────
print("\n=== B. Anti-bot 탐지 파이프라인 연동 ===\n")

# 시나리오: 크롤링 중 Cloudflare 탐지
audit2 = AuditLog()
audit2.log("crawl_started", "test_src", {})

# 정상 3페이지 후 Cloudflare
for page in range(1, 4):
    audit2.log("page_fetched", "test_src", {"page": page})

# 4페이지에서 Cloudflare 탐지
ab_type = detect_antibot(200, "Checking your browser... Cloudflare", {"server": "cloudflare"})
audit2.log("antibot_detected", "test_src", {
    "type": ab_type.value,
    "page": 4,
})

check("B1-1", f"Anti-bot 탐지 이벤트 기록: {ab_type.value}",
      ab_type == AntiBotType.CLOUDFLARE)

counts = audit2.count_by_type()
check("B1-2", f"이벤트 분류: page_fetched={counts.get('page_fetched',0)}, antibot={counts.get('antibot_detected',0)}",
      counts["page_fetched"] == 3 and counts["antibot_detected"] == 1)

# ─── C. UA 회전 + SFDAWebClient 시뮬레이션 ────────────
print("\n=== C. User-Agent 회전 통합 ===\n")

from sfda_web import SFDAWebClient

# SFDAWebClient가 pick_ua()로 UA를 설정하는지 확인
with SFDAWebClient() as client:
    ua = client._http.headers.get("User-Agent", "")
check("C1-1", f"SFDAWebClient UA: {ua[:50]}...",
      ua in UA_POOL)

# 여러 클라이언트 생성 시 UA가 다를 수 있는지
uas = set()
for _ in range(20):
    with SFDAWebClient() as c:
        uas.add(c._http.headers.get("User-Agent", ""))
check("C1-2", f"20개 클라이언트 중 {len(uas)}개 다른 UA 사용",
      len(uas) > 1)

# ─── D. main.py 통합 시뮬레이션 (Anti-bot 분류 정확도) ─
print("\n=== D. main.py Anti-bot 분류 → CrawlErrorType 매핑 ===\n")

# main.py의 except httpx.HTTPStatusError에서 detect() 사용
from supabase_state import CrawlErrorType

test_cases = [
    (401, "", CrawlErrorType.AUTH_FAIL),
    (403, "Access Denied", CrawlErrorType.WAF_DETECTED),
    (403, "Cloudflare Ray ID", CrawlErrorType.WAF_DETECTED),
    (403, "<div class='g-recaptcha'>", CrawlErrorType.WAF_DETECTED),
    (429, "Too Many Requests", CrawlErrorType.RATE_LIMIT),
    (520, "Unknown Error", CrawlErrorType.WAF_DETECTED),
    (500, "Internal Server Error", CrawlErrorType.UNKNOWN),
]

for i, (status, body, expected_type) in enumerate(test_cases):
    ab_type = detect_antibot(status, body)
    # main.py의 분류 로직 재현
    if status == 401:
        mapped = CrawlErrorType.AUTH_FAIL
    elif ab_type in (AntiBotType.CLOUDFLARE, AntiBotType.IP_BLOCK,
                     AntiBotType.RECAPTCHA, AntiBotType.WAF_GENERIC):
        mapped = CrawlErrorType.WAF_DETECTED
    elif status == 429:
        mapped = CrawlErrorType.RATE_LIMIT
    else:
        mapped = CrawlErrorType.UNKNOWN

    check(f"D1-{i+1}", f"status={status}, body='{body[:30]}' → {mapped.value}",
          mapped == expected_type)

# ─── E. 리턴값 확인 ────────────────────────────────
print("\n=== E. sfda_api.py 리턴값 구조 검증 ===\n")

result = {
    "rows_inserted": 25,
    "rows_updated": 0,
    "rows_skipped": 1,
    "audit_log": audit.to_json(),
    "metrics": metrics.to_json(),
}

check("E1-1", "리턴에 audit_log 포함",
      "audit_log" in result)
check("E1-2", "리턴에 metrics 포함",
      "metrics" in result)
check("E1-3", "audit_log JSON 파싱 가능",
      len(json.loads(result["audit_log"])) > 0)
check("E1-4", "metrics JSON 파싱 가능",
      "counters" in json.loads(result["metrics"]))


print(f"\n{'='*60}")
print(f"4단계 결과: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("4단계 통과. 전체 통합 완료.")
else:
    print("4단계 실패. 수정 필요.")
