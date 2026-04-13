"""
=============================================================================
3단계 시뮬레이션: 구조화 감사 로그 + 경량 메트릭
=============================================================================
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from supabase_state import AuditLog, MetricsCollector

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

# ─── A. AuditLog 기본 동작 ────────────────────────────
print("=== A. 감사 로그 기본 동작 ===\n")

audit = AuditLog()

e1 = audit.log("crawl_started", "sfda_web", {"mode": "full", "start_page": 1})
check("A1-1", "이벤트 기록: ts 존재", "ts" in e1 and e1["ts"] > 0)
check("A1-2", "이벤트 기록: event=crawl_started", e1["event"] == "crawl_started")
check("A1-3", "이벤트 기록: source=sfda_web", e1["source"] == "sfda_web")

# 여러 이벤트 기록
for i in range(10):
    audit.log("record_processed", "sfda_web", {"product_id": f"SFDA_{i}", "outlier": i == 5})
audit.log("antibot_detected", "sfda_web", {"type": "cloudflare", "status": 200})
audit.log("crawl_finished", "sfda_web", {"inserted": 9, "skipped": 1})

check("A2-1", f"총 이벤트 수: {len(audit.events)}",
      len(audit.events) == 13)

# 타입별 집계
counts = audit.count_by_type()
check("A2-2", f"crawl_started: {counts.get('crawl_started', 0)}건",
      counts["crawl_started"] == 1)
check("A2-3", f"record_processed: {counts.get('record_processed', 0)}건",
      counts["record_processed"] == 10)
check("A2-4", f"antibot_detected: {counts.get('antibot_detected', 0)}건",
      counts["antibot_detected"] == 1)
check("A2-5", f"crawl_finished: {counts.get('crawl_finished', 0)}건",
      counts["crawl_finished"] == 1)

# JSON 직렬화
json_str = audit.to_json()
parsed = json.loads(json_str)
check("A3-1", f"JSON 직렬화 성공: {len(parsed)}건",
      len(parsed) == 13)
check("A3-2", "JSON 역직렬화 가능",
      all("ts" in e and "event" in e for e in parsed))

# 이벤트 불변성
events_copy = audit.events
events_copy.append({"fake": True})
check("A3-3", "events 프로퍼티는 복사본 반환 (불변성)",
      len(audit.events) == 13)

# ─── B. MetricsCollector 기본 동작 ────────────────────
print("\n=== B. 경량 메트릭 수집 ===\n")

metrics = MetricsCollector()

# 카운터
metrics.inc("crawl_attempts")
metrics.inc("crawl_success")
metrics.inc("records_processed", 876)
metrics.inc("outliers_detected", 3)
metrics.inc("antibot_detected", 1)

check("B1-1", f"crawl_attempts: {metrics.get_counter('crawl_attempts')}",
      metrics.get_counter("crawl_attempts") == 1)
check("B1-2", f"records_processed: {metrics.get_counter('records_processed')}",
      metrics.get_counter("records_processed") == 876)
check("B1-3", f"outliers_detected: {metrics.get_counter('outliers_detected')}",
      metrics.get_counter("outliers_detected") == 3)

# 타이머
import random
for _ in range(20):
    metrics.observe("page_fetch_sec", random.uniform(0.3, 2.0))
metrics.observe("crawl_duration_sec", 45.3)
metrics.observe("crawl_duration_sec", 52.1)

summary = metrics.summary()
check("B2-1", f"카운터 요약: {len(summary['counters'])}개",
      len(summary["counters"]) == 5)
check("B2-2", "page_fetch_sec 통계 존재",
      "page_fetch_sec" in summary and "mean" in summary["page_fetch_sec"])
check("B2-3", f"page_fetch_sec count: {summary['page_fetch_sec']['count']}",
      summary["page_fetch_sec"]["count"] == 20)
check("B2-4", f"page_fetch_sec p95: {summary['page_fetch_sec']['p95']:.3f}",
      0.3 <= summary["page_fetch_sec"]["p95"] <= 2.0)

# JSON 직렬화
json_metrics = metrics.to_json()
parsed_m = json.loads(json_metrics)
check("B3-1", "메트릭 JSON 직렬화 성공",
      "counters" in parsed_m)

# ─── C. CrawlRun 통합 시뮬레이션 ────────────────────
print("\n=== C. CrawlRun + AuditLog + Metrics 통합 ===\n")

# 전체 크롤링 시나리오 시뮬레이션
audit2 = AuditLog()
metrics2 = MetricsCollector()

# 1. 시작
t_start = time.time()
audit2.log("crawl_started", "sfda_web", {"mode": "full"})
metrics2.inc("crawl_attempts")

# 2. 페이지별 처리
for page in range(1, 4):
    t_page = time.time()
    audit2.log("page_fetched", "sfda_web", {"page": page, "items": 10})

    for item in range(10):
        product_id = f"SFDA_{page}_{item}"
        is_outlier = (page == 2 and item == 5)
        audit2.log("record_processed", "sfda_web", {
            "product_id": product_id,
            "outlier": is_outlier,
            "confidence": 0.95,
        })
        metrics2.inc("records_processed")
        if is_outlier:
            metrics2.inc("outliers_detected")

    metrics2.observe("page_fetch_sec", time.time() - t_page)

# 3. 완료
metrics2.inc("crawl_success")
t_total = time.time() - t_start
metrics2.observe("crawl_duration_sec", t_total)

audit2.log("crawl_finished", "sfda_web", {
    "inserted": 29,
    "skipped": 1,
    "metrics": metrics2.summary(),
})

# 검증
check("C1-1", f"총 이벤트: {len(audit2.events)}건 (1시작 + 3페이지 + 30레코드 + 1완료)",
      len(audit2.events) == 35)
check("C1-2", f"처리 건수: {metrics2.get_counter('records_processed')}",
      metrics2.get_counter("records_processed") == 30)
check("C1-3", f"이상치: {metrics2.get_counter('outliers_detected')}건",
      metrics2.get_counter("outliers_detected") == 1)
check("C1-4", f"성공: {metrics2.get_counter('crawl_success')}",
      metrics2.get_counter("crawl_success") == 1)

final_summary = metrics2.summary()
check("C1-5", f"page_fetch 페이지 수: {final_summary['page_fetch_sec']['count']}",
      final_summary["page_fetch_sec"]["count"] == 3)

# CrawlRun.finish()에 첨부할 데이터 시뮬레이션
finish_payload = {
    "status": "success",
    "rows_inserted": 29,
    "rows_updated": 0,
    "error_summary": None,
    "audit_log": audit2.to_json(),
    "metrics": metrics2.to_json(),
}
check("C2-1", "finish payload JSON 직렬화",
      json.loads(finish_payload["audit_log"]) is not None)
check("C2-2", "finish payload metrics JSON",
      json.loads(finish_payload["metrics"]) is not None)

# 크기 제한 확인 (Supabase text 필드: ~1MB)
audit_size = len(finish_payload["audit_log"].encode("utf-8"))
metrics_size = len(finish_payload["metrics"].encode("utf-8"))
check("C2-3", f"audit_log 크기: {audit_size} bytes (30건 기준)",
      audit_size < 100_000)  # 30건이면 수 KB
check("C2-4", f"metrics 크기: {metrics_size} bytes",
      metrics_size < 10_000)


# ─── D. 대량 데이터 성능 ────────────────────────────
print("\n=== D. 대량 데이터 성능 (8,756건 시뮬레이션) ===\n")

audit3 = AuditLog()
metrics3 = MetricsCollector()

t0 = time.time()
audit3.log("crawl_started", "sfda_web", {"mode": "full"})
for i in range(8756):
    audit3.log("record_processed", "sfda_web", {"product_id": f"SFDA_{i}"})
    metrics3.inc("records_processed")
audit3.log("crawl_finished", "sfda_web", {"inserted": 8756})
elapsed = time.time() - t0

check("D1-1", f"8,756건 기록 소요: {elapsed:.3f}초",
      elapsed < 5.0)  # 5초 이내

# JSON 직렬화 성능
t1 = time.time()
big_json = audit3.to_json()
json_elapsed = time.time() - t1
check("D1-2", f"8,758 이벤트 JSON 직렬화: {json_elapsed:.3f}초",
      json_elapsed < 5.0)

big_size = len(big_json.encode("utf-8"))
check("D1-3", f"8,758 이벤트 JSON 크기: {big_size/1024:.0f} KB",
      big_size < 5_000_000)  # 5MB 이내

check("D1-4", f"메트릭 카운터: {metrics3.get_counter('records_processed')}건",
      metrics3.get_counter("records_processed") == 8756)


print(f"\n{'='*60}")
print(f"3단계 결과: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("3단계 통과. 4단계 진행 가능.")
else:
    print("3단계 실패. 수정 필요.")
