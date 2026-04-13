"""1단계 시뮬레이션: 코어 함수 검증 (calc_k, get_threshold)"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from outlier_detector import calc_k, get_threshold, K_THRESHOLDS_NORMAL

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


print("=== 1단계: 코어 함수 검증 ===\n")

# 1-1. calc_k 기본 계산
data = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
k = calc_k(data)
# R=9, Q1=3.25, Q3=7.75, IQR=4.5, K=9/4.5=2.0
check("calc_k 기본", abs(k - 2.0) < 0.01, f"got {k}")

# 1-2. IQR=0 -> inf
data_same = np.array([5.0, 5.0, 5.0, 5.0])
k_inf = calc_k(data_same)
check("calc_k IQR=0 -> inf", k_inf == float("inf"))

# 1-3. 이상치 포함 시 K 증가
data_normal = np.random.uniform(5, 15, size=30)
k_normal = calc_k(data_normal)
data_outlier = np.append(data_normal, 150.0)
k_outlier = calc_k(data_outlier)
check("이상치 추가시 K 증가", k_outlier > k_normal,
      f"normal={k_normal:.2f}, outlier={k_outlier:.2f}")

# 1-4. get_threshold 정확도
for n_key, alphas in K_THRESHOLDS_NORMAL.items():
    t = get_threshold(n_key, 0.05)
    check(f"threshold n={n_key}", abs(t - alphas[0.05]) < 0.001)

# 1-5. get_threshold 보간 (n=25 -> n=30 임계값)
t25 = get_threshold(25, 0.05)
t30 = get_threshold(30, 0.05)
check("n=25 -> n=30 임계값 사용", abs(t25 - t30) < 0.001)

# 1-6. get_threshold 범위 초과 (n=2000 -> n=1000 임계값)
t2000 = get_threshold(2000, 0.05)
t1000 = get_threshold(1000, 0.05)
check("n=2000 -> n=1000 임계값 사용", abs(t2000 - t1000) < 0.001)

# 1-7. alpha별 임계값 순서: 0.01 > 0.05 > 0.10
for n_key in [20, 50, 100]:
    t01 = get_threshold(n_key, 0.01)
    t05 = get_threshold(n_key, 0.05)
    t10 = get_threshold(n_key, 0.10)
    check(f"alpha 순서 n={n_key}", t01 > t05 > t10,
          f"0.01={t01}, 0.05={t05}, 0.10={t10}")

print(f"\n=== 1단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("1단계 통과. 2단계 진행 가능.")
