"""3단계 시뮬레이션: 중규모/소규모 그룹 + median 배수 룰"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from outlier_detector import check_outlier

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


print("=== 3단계: 중규모/소규모 그룹 ===\n")

# ── n < 5: median 배수 룰 ──
print("-- n < 5: median 배수 룰 --")

# 3-1. n=1 (첫 레코드) -> skip
r = check_outlier([], 100.0)
check("첫 레코드 -> skip", not r.flagged and r.method == "skip")

# 3-2. n=2, 정상 (2번째 가격이 비슷)
r = check_outlier([10.0], 12.0)
check("n=2 정상", not r.flagged and r.method == "median_ratio",
      f"reason={r.reason}")

# 3-3. n=2, 이상치 (10배 초과)
r = check_outlier([10.0], 150.0)
check("n=2 이상치 15x", r.flagged and r.method == "median_ratio",
      f"reason={r.reason}")

# 3-4. n=3, 이상치 (역방향, 매우 낮은 가격)
r = check_outlier([100.0, 110.0], 5.0)
check("n=3 역방향 이상치 (1/20x)", r.flagged,
      f"flagged={r.flagged}, reason={r.reason}")

# 3-5. n=4, 정상 (3배 차이는 통과)
r = check_outlier([10.0, 11.0, 12.0], 30.0)
check("n=4 3배차이 정상", not r.flagged, f"reason={r.reason}")

# 3-6. n=4, 이상치 (15배)
r = check_outlier([10.0, 11.0, 12.0], 165.0)
check("n=4 15배 이상치", r.flagged, f"reason={r.reason}")

# ── 5 <= n < 20: K + median 이중 검증 ──
print("\n-- 5 <= n < 20: 이중 검증 --")

# 3-7. n=10, 명백한 이상치 (10배)
prices_10 = np.random.uniform(5, 15, size=9).tolist()
r = check_outlier(prices_10, 150.0)
check("n=10 10x 이상치", r.flagged and "k_statistic" in r.method,
      f"method={r.method}, reason={r.reason}")

# 3-8. n=10, 정상 가격
prices_10 = np.random.uniform(5, 15, size=9).tolist()
r = check_outlier(prices_10, 10.0)
check("n=10 정상", not r.flagged, f"reason={r.reason}")

# 3-9. n=10, 2배 차이 (K 초과해도 median 5x 미만이면 통과)
prices_10 = np.random.uniform(5, 15, size=9).tolist()
r = check_outlier(prices_10, 25.0)
check("n=10 2.5x 차이 -> 통과 (이중검증)", not r.flagged,
      f"reason={r.reason}")

# 3-10. n=15, 이상치 50배
prices_15 = np.random.uniform(10, 20, size=14).tolist()
r = check_outlier(prices_15, 750.0)
check("n=15 50x 이상치", r.flagged, f"reason={r.reason}")

# 3-11. n=15, 정상
prices_15 = np.random.uniform(10, 20, size=14).tolist()
r = check_outlier(prices_15, 15.0)
check("n=15 정상", not r.flagged, f"reason={r.reason}")

# ── 기존 2단계 회귀 테스트 ──
print("\n-- 회귀 테스트: 2단계 케이스 --")

# 3-12. n>=20 명백한 이상치 여전히 잡음
prices_30 = np.random.uniform(5, 15, size=30).tolist()
r = check_outlier(prices_30, 150.0)
check("n=31 10x (회귀)", r.flagged and r.method == "k_statistic",
      f"method={r.method}")

# 3-13. n>=20 정상 여전히 통과
prices_30 = np.random.uniform(5, 15, size=30).tolist()
r = check_outlier(prices_30, 10.0)
check("n=31 정상 (회귀)", not r.flagged)

# 3-14. 음수 여전히 잡음
r = check_outlier([10.0], -5.0)
check("음수 (회귀)", r.flagged and r.method == "invalid")

print(f"\n=== 3단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("3단계 통과. 4단계 진행 가능.")
