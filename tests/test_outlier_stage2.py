"""2단계 시뮬레이션: 대규모 그룹 탐지 (n>=20)"""
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


print("=== 2단계: 대규모 그룹 탐지 (n>=20) ===\n")

# 2-1. 명백한 이상치 10배 (n=30+1=31)
for trial in range(5):
    prices = np.random.uniform(5, 15, size=30).tolist()
    r = check_outlier(prices, 150.0)
    check(f"10x 이상치 trial{trial}", r.flagged and r.method == "k_statistic",
          f"flagged={r.flagged}, K={r.k_value:.2f}, thresh={r.threshold:.2f}")

# 2-2. 박스 가격 혼입 (28배)
prices = np.random.uniform(8, 12, size=25).tolist()
r = check_outlier(prices, 280.0)
check("박스가격 28x", r.flagged, f"K={r.k_value:.2f}")

# 2-3. 정상 데이터 -> 오탐 없어야 함
for trial in range(5):
    prices = np.random.uniform(5, 15, size=30).tolist()
    new_p = np.random.uniform(5, 15)
    r = check_outlier(prices, float(new_p))
    check(f"정상가격 오탐없음 trial{trial}", not r.flagged,
          f"flagged={r.flagged}, K={r.k_value:.2f}, new={new_p:.2f}")

# 2-4. 제네릭 vs 오리지날 (정상 분포 내)
prices_mixed = (np.random.uniform(8, 15, size=15).tolist() +
                np.random.uniform(40, 60, size=10).tolist())
r = check_outlier(prices_mixed, 50.0)
check("제네릭+오리지날 오탐없음", not r.flagged,
      f"K={r.k_value:.2f}, flagged={r.flagged}")

# 2-5. 이상치가 max도 min도 아닌 경우 -> 플래그 안 달아야 함
prices = np.random.uniform(5, 15, size=29).tolist()
prices.append(200.0)  # 기존에 이미 이상치가 있음
r = check_outlier(prices, 10.0)  # 신규값은 정상
check("신규값 정상 + 기존 이상치", not r.flagged,
      f"K={r.k_value:.2f}, reason={r.reason}")

# 2-6. n < 20 -> 3단계 로직 (k_statistic+median 이중검증)
prices_small = [10.0, 11.0, 12.0, 13.0, 14.0]
r = check_outlier(prices_small, 150.0)
check("n<20 -> 이중검증", r.method in ("k_statistic+median", "median_ratio"),
      f"method={r.method}")

# 2-7. 음수 가격 -> invalid
r = check_outlier([10.0, 11.0], -5.0)
check("음수 가격 -> flagged", r.flagged and r.method == "invalid")

# 2-8. 0 가격 -> invalid
r = check_outlier([10.0, 11.0], 0.0)
check("0 가격 -> flagged", r.flagged and r.method == "invalid")

# 2-9. 대규모 (n=100) 정상 데이터
prices_100 = np.random.uniform(20, 40, size=99).tolist()
r = check_outlier(prices_100, 30.0)
check("n=100 정상", not r.flagged, f"K={r.k_value:.2f}")

# 2-10. 대규모 (n=100) + 이상치
r = check_outlier(prices_100, 500.0)
check("n=100 이상치", r.flagged, f"K={r.k_value:.2f}")

print(f"\n=== 2단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("2단계 통과. 3단계 진행 가능.")
