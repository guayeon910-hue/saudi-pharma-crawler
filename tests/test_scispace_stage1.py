"""
=============================================================================
1단계 시뮬레이션: Anti-bot 탐지 + 적응형 대응 + UA 회전
=============================================================================
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from antibot import (
    AntiBotType, detect, get_countermeasure, pick_ua,
    UA_POOL, COUNTERMEASURES,
)

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

# ─── A. detect() 정확도 테스트 ────────────────────────────
print("=== A. Anti-bot 탐지 정확도 (20 시나리오) ===\n")

# A1. 정상 응답
check("A1-1", "200 정상 HTML → NONE",
      detect(200, "<html><body>Drug List</body></html>") == AntiBotType.NONE)
check("A1-2", "200 빈 JSON → NONE",
      detect(200, "{}") == AntiBotType.NONE)
check("A1-3", "200 SFDA 실제 데이터 → NONE",
      detect(200, '{"results":[{"tradeName":"Paracetamol"}]}') == AntiBotType.NONE)

# A2. Cloudflare 시나리오
check("A2-1", "200 + Cloudflare body → CLOUDFLARE",
      detect(200, "Checking your browser before accessing the site... Cloudflare") == AntiBotType.CLOUDFLARE)
check("A2-2", "403 + Cloudflare body → CLOUDFLARE (CF 우선)",
      detect(403, "Performance & Security by Cloudflare. Ray ID: abc123") == AntiBotType.CLOUDFLARE)
check("A2-3", "200 + cf-ray header → CLOUDFLARE",
      detect(200, "<html>normal</html>", {"server": "cloudflare", "cf-ray": "abc"}) == AntiBotType.CLOUDFLARE)
check("A2-4", "503 + Cloudflare body → CLOUDFLARE",
      detect(503, "Cloudflare is checking your browser") == AntiBotType.CLOUDFLARE)

# A3. Rate Limit
check("A3-1", "429 → RATE_LIMIT",
      detect(429, "Too Many Requests") == AntiBotType.RATE_LIMIT)
check("A3-2", "429 + Retry-After header → RATE_LIMIT",
      detect(429, "", {"Retry-After": "30"}) == AntiBotType.RATE_LIMIT)

# A4. CAPTCHA
check("A4-1", "403 + reCAPTCHA → RECAPTCHA",
      detect(403, '<div class="g-recaptcha" data-sitekey="abc"></div>') == AntiBotType.RECAPTCHA)
check("A4-2", "403 + hCaptcha → RECAPTCHA",
      detect(403, '<div class="hcaptcha">Please verify</div>') == AntiBotType.RECAPTCHA)

# A5. IP Block (403, CAPTCHA 아닌)
check("A5-1", "403 Access Denied → IP_BLOCK",
      detect(403, "Access Denied") == AntiBotType.IP_BLOCK)
check("A5-2", "403 Forbidden → IP_BLOCK",
      detect(403, "Forbidden") == AntiBotType.IP_BLOCK)

# A6. WAF Generic (특수 5xx)
check("A6-1", "520 → WAF_GENERIC",
      detect(520, "Web server is returning an unknown error") == AntiBotType.WAF_GENERIC)
check("A6-2", "522 Connection timed out → WAF_GENERIC",
      detect(522, "") == AntiBotType.WAF_GENERIC)
check("A6-3", "524 A Timeout Occurred → WAF_GENERIC",
      detect(524, "") == AntiBotType.WAF_GENERIC)

# A7. 일반 서버 오류 (반봇 아님)
check("A7-1", "500 Internal Server Error → NONE (backoff_retry가 처리)",
      detect(500, "Internal Server Error") == AntiBotType.NONE)
check("A7-2", "502 Bad Gateway → NONE",
      detect(502, "") == AntiBotType.NONE)
check("A7-3", "503 Service Unavailable (CF 아님) → NONE",
      detect(503, "Service temporarily unavailable") == AntiBotType.NONE)

# ─── B. 대응 규칙 테스트 ────────────────────────────────
print("\n=== B. 적응형 대응 규칙 (6 시나리오) ===\n")

cm_cf = get_countermeasure(AntiBotType.CLOUDFLARE)
check("B1-1", f"Cloudflare: delay x{cm_cf.delay_multiplier}, circuit={cm_cf.should_circuit_break}",
      cm_cf.delay_multiplier == 3.0 and not cm_cf.should_circuit_break)
check("B1-2", "Cloudflare: Accept-Language 헤더 추가",
      cm_cf.extra_headers is not None and "Accept-Language" in cm_cf.extra_headers)

cm_rl = get_countermeasure(AntiBotType.RATE_LIMIT)
check("B2-1", f"Rate Limit: delay x{cm_rl.delay_multiplier}, action={cm_rl.action}",
      cm_rl.delay_multiplier == 2.0 and cm_rl.action == "respect_retry_after")

cm_ip = get_countermeasure(AntiBotType.IP_BLOCK)
check("B3-1", f"IP Block: circuit_break={cm_ip.should_circuit_break}",
      cm_ip.should_circuit_break is True)

cm_cap = get_countermeasure(AntiBotType.RECAPTCHA)
check("B3-2", f"CAPTCHA: circuit_break={cm_cap.should_circuit_break}",
      cm_cap.should_circuit_break is True)

cm_waf = get_countermeasure(AntiBotType.WAF_GENERIC)
check("B4-1", f"WAF Generic: delay x{cm_waf.delay_multiplier}",
      cm_waf.delay_multiplier == 5.0)

cm_none = get_countermeasure(AntiBotType.NONE)
check("B5-1", f"NONE: delay x{cm_none.delay_multiplier}, action={cm_none.action}",
      cm_none.delay_multiplier == 1.0 and cm_none.action == "none")

# ─── C. User-Agent 회전 테스트 ────────────────────────────
print("\n=== C. User-Agent 회전 (5 시나리오) ===\n")

check("C1-1", f"UA 풀 크기: {len(UA_POOL)}개",
      len(UA_POOL) >= 5)

# 100회 뽑아서 분포 확인
ua_distribution = {}
for _ in range(600):
    ua = pick_ua()
    ua_distribution[ua] = ua_distribution.get(ua, 0) + 1

check("C1-2", f"UA 분포 균등성: {len(ua_distribution)}개 UA 사용됨",
      len(ua_distribution) == len(UA_POOL))

min_count = min(ua_distribution.values())
max_count = max(ua_distribution.values())
check("C1-3", f"UA 분포 편차: min={min_count}, max={max_count} (600회 중)",
      max_count - min_count < 100)  # 합리적 분포

# 모든 UA가 유효한 형식인지
check("C1-4", "모든 UA가 'Mozilla/5.0'으로 시작",
      all(ua.startswith("Mozilla/5.0") for ua in UA_POOL))

check("C1-5", "매 호출마다 다른 UA 반환 가능",
      len(set(pick_ua() for _ in range(20))) > 1)

# ─── D. backoff_retry.py 통합 시뮬레이션 ────────────────
print("\n=== D. backoff_retry.py 통합 시뮬레이션 ===\n")

# 시나리오: Cloudflare 응답 시 detect → countermeasure → delay 조정
import httpx
from unittest.mock import MagicMock

# Mock HTTP 응답
def simulate_response(status, body, headers=None):
    """실제 backoff_retry 워크플로 시뮬레이션"""
    ab_type = detect(status, body, headers or {})
    cm = get_countermeasure(ab_type)

    base_wait = 3.0  # backoff_retry 기본 대기
    adjusted_wait = base_wait * cm.delay_multiplier

    return {
        "antibot_type": ab_type,
        "action": cm.action,
        "adjusted_wait": adjusted_wait,
        "should_circuit_break": cm.should_circuit_break,
    }

# D1. Cloudflare 응답 → delay 3배
r1 = simulate_response(200, "Checking your browser... Cloudflare")
check("D1-1", f"CF 탐지 후 wait: {r1['adjusted_wait']}s (기본 3s × 3.0)",
      r1["adjusted_wait"] == 9.0)

# D2. Rate Limit → delay 2배
r2 = simulate_response(429, "Too Many Requests")
check("D2-1", f"429 탐지 후 wait: {r2['adjusted_wait']}s (기본 3s × 2.0)",
      r2["adjusted_wait"] == 6.0)

# D3. WAF → delay 5배
r3 = simulate_response(520, "Unknown error")
check("D3-1", f"WAF 탐지 후 wait: {r3['adjusted_wait']}s (기본 3s × 5.0)",
      r3["adjusted_wait"] == 15.0)

# D4. IP Block → circuit break
r4 = simulate_response(403, "Access Denied")
check("D4-1", f"403 → circuit_break={r4['should_circuit_break']}",
      r4["should_circuit_break"] is True)

# D5. 정상 → 변경 없음
r5 = simulate_response(200, '{"results":[]}')
check("D5-1", f"정상 → wait: {r5['adjusted_wait']}s (변경 없음)",
      r5["adjusted_wait"] == 3.0)

# ─── E. main.py 통합 시뮬레이션 ─────────────────────────
print("\n=== E. main.py 통합 시뮬레이션 ===\n")

# 시나리오: 크롤러가 httpx.HTTPStatusError를 던질 때 detect() 결과로 분기

def simulate_main_error_handling(status_code, response_body):
    """main.py의 except httpx.HTTPStatusError 블록 시뮬레이션"""
    # 기존: status == 403 → force_open
    # 개선: detect() → countermeasure 확인 후 분기
    ab_type = detect(status_code, response_body)
    cm = get_countermeasure(ab_type)

    actions_taken = []

    if cm.should_circuit_break:
        actions_taken.append("force_open")

    if ab_type == AntiBotType.CLOUDFLARE:
        actions_taken.append("delay_x3")
        actions_taken.append("rotate_ua")

    if ab_type == AntiBotType.RATE_LIMIT:
        actions_taken.append("delay_x2")

    if ab_type == AntiBotType.WAF_GENERIC:
        actions_taken.append("delay_x5")

    if ab_type == AntiBotType.NONE:
        actions_taken.append("standard_backoff")

    return ab_type, actions_taken

t1, a1 = simulate_main_error_handling(403, "Access Denied")
check("E1-1", f"403 단순 → {t1.value}: {a1}",
      "force_open" in a1)

t2, a2 = simulate_main_error_handling(403, "Cloudflare challenge")
check("E1-2", f"403 + CF → {t2.value}: {a2}",
      t2 == AntiBotType.CLOUDFLARE and "delay_x3" in a2)

t3, a3 = simulate_main_error_handling(429, "Too Many Requests")
check("E1-3", f"429 → {t3.value}: {a3}",
      "delay_x2" in a3)

t4, a4 = simulate_main_error_handling(500, "Internal Server Error")
check("E1-4", f"500 → {t4.value}: {a4}",
      "standard_backoff" in a4)


# ─── 최종 결과 ────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"1단계 결과: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("1단계 통과. 2단계 진행 가능.")
else:
    print("1단계 실패. 수정 필요.")
