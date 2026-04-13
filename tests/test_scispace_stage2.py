"""
=============================================================================
2단계 시뮬레이션: 소스 신뢰도 점수 (EMA Reputation)
=============================================================================
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from supabase_state import SourceReputation

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

print("=== A. EMA 기본 동작 ===\n")

rep = SourceReputation(alpha=0.1)

# A1. 초기값
check("A1-1", f"초기값: {rep.get('sfda_web')}", rep.get("sfda_web") == 0.5)

# A2. 연속 성공
for _ in range(10):
    s = rep.update("sfda_web", True)
check("A2-1", f"10회 연속 성공 후: {s:.4f}", s > 0.8)

# A3. 연속 실패
rep2 = SourceReputation(alpha=0.1)
for _ in range(10):
    s2 = rep2.update("bad_src", False)
check("A3-1", f"10회 연속 실패 후: {s2:.4f}", s2 < 0.2)

# A4. 회복 시나리오 (5실패 → 5성공)
rep3 = SourceReputation(alpha=0.1)
for _ in range(5):
    rep3.update("mixed_src", False)
mid = rep3.get("mixed_src")
for _ in range(5):
    s3 = rep3.update("mixed_src", True)
check("A4-1", f"5실패({mid:.3f}) → 5성공({s3:.3f})", mid < 0.5 and s3 > mid)

# A5. 범위 보장
rep4 = SourceReputation(alpha=0.5)  # 극단적 alpha
for _ in range(100):
    rep4.update("extreme", True)
check("A5-1", f"극단 alpha=0.5, 100회 성공 후: {rep4.get('extreme'):.4f} <= 1.0",
      rep4.get("extreme") <= 1.0)
for _ in range(100):
    rep4.update("extreme", False)
check("A5-2", f"100회 실패 후: {rep4.get('extreme'):.4f} >= 0.0",
      rep4.get("extreme") >= 0.0)

print("\n=== B. confidence 보정 ===\n")

# B1. 신뢰 소스
rep5 = SourceReputation(alpha=0.1)
for _ in range(20):
    rep5.update("good", True)
bonus = rep5.confidence_bonus("good")
check("B1-1", f"신뢰 소스 bonus: {bonus:+.4f} (범위 +0.00 ~ +0.05)",
      0.0 < bonus <= 0.05)

# B2. 불신 소스
for _ in range(20):
    rep5.update("bad", False)
penalty = rep5.confidence_bonus("bad")
check("B2-1", f"불신 소스 bonus: {penalty:+.4f} (범위 -0.05 ~ -0.00)",
      -0.05 <= penalty < 0.0)

# B3. 미등록 소스
unknown_bonus = rep5.confidence_bonus("unknown")
check("B3-1", f"미등록 소스 bonus: {unknown_bonus:+.4f} (= 0.0)",
      unknown_bonus == 0.0)

# B4. 실제 파이프라인 통합 시뮬레이션
base_confidence = 0.92
adjusted = base_confidence + rep5.confidence_bonus("good")
check("B4-1", f"SFDA 최종 confidence: {base_confidence} + {bonus:+.4f} = {adjusted:.4f}",
      0.92 < adjusted <= 0.97)

adjusted_bad = base_confidence + rep5.confidence_bonus("bad")
check("B4-2", f"불신 소스 confidence: {base_confidence} + {penalty:+.4f} = {adjusted_bad:.4f}",
      0.87 <= adjusted_bad < 0.92)

print("\n=== C. 다수 소스 독립성 ===\n")

rep6 = SourceReputation(alpha=0.1)
sources = ["sfda_web", "hsa_sg", "dav_vn", "eda_eg", "jfda_jo"]
for src in sources:
    for _ in range(5):
        rep6.update(src, True)

# 각 소스 독립적으로 관리
scores = {src: rep6.get(src) for src in sources}
check("C1-1", f"5개 소스 독립 관리: {len(scores)}개",
      len(scores) == 5)
check("C1-2", "모든 소스 동일 점수 (동일 이력)",
      len(set(f"{v:.4f}" for v in scores.values())) == 1)

# 한 소스만 실패
for _ in range(5):
    rep6.update("dav_vn", False)
check("C1-3", f"dav_vn만 실패: sfda={rep6.get('sfda_web'):.3f}, dav={rep6.get('dav_vn'):.3f}",
      rep6.get("sfda_web") > rep6.get("dav_vn"))

print("\n=== D. alpha 감도 분석 ===\n")

# D1. alpha=0.05 (보수적)
rep_slow = SourceReputation(alpha=0.05)
for _ in range(10):
    rep_slow.update("slow", True)
slow_score = rep_slow.get("slow")

# D2. alpha=0.2 (반응적)
rep_fast = SourceReputation(alpha=0.2)
for _ in range(10):
    rep_fast.update("fast", True)
fast_score = rep_fast.get("fast")

check("D1-1", f"alpha=0.05, 10회 성공: {slow_score:.4f} (보수적 = 더 느린 상승)",
      slow_score < fast_score)
check("D2-1", f"alpha=0.20, 10회 성공: {fast_score:.4f} (반응적 = 더 빠른 상승)",
      fast_score > 0.8)

# D3. 단발 실패 영향
rep_resilient = SourceReputation(alpha=0.05)
for _ in range(20):
    rep_resilient.update("stable", True)
before = rep_resilient.get("stable")
rep_resilient.update("stable", False)  # 1회 실패
after = rep_resilient.get("stable")
check("D3-1", f"단발 실패 영향: {before:.4f} → {after:.4f} (차이 {before-after:.4f})",
      before - after < 0.1)  # alpha=0.05이므로 영향 작음


print(f"\n{'='*60}")
print(f"2단계 결과: {passed} passed, {failed} failed")
print(f"{'='*60}")
if failed == 0:
    print("2단계 통과. 3단계 진행 가능.")
else:
    print("2단계 실패. 수정 필요.")
