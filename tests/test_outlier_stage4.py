"""4단계 시뮬레이션: 엣지케이스 (IQR=0, 반복 제거, 경계 조건)"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import numpy as np
from outlier_detector import check_outlier, scan_group

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


print("=== 4단계: 엣지케이스 ===\n")

# ── IQR=0 처리 ──
print("-- IQR=0 --")

# 4-1. 모든 값 동일 + 신규도 동일 -> 정상
r = check_outlier([10.0] * 25, 10.0)
check("IQR=0, 신규=동일 -> 정상", not r.flagged, f"reason={r.reason}")

# 4-2. 모든 값 동일 + 신규 10배 -> 이상치
r = check_outlier([10.0] * 25, 100.0)
check("IQR=0, 신규=10x -> 이상치", r.flagged and r.method == "iqr_zero",
      f"method={r.method}, reason={r.reason}")

# 4-3. 모든 값 동일 + 신규 1/20 -> 이상치
r = check_outlier([100.0] * 25, 5.0)
check("IQR=0, 신규=1/20x -> 이상치", r.flagged and r.method == "iqr_zero",
      f"reason={r.reason}")

# 4-4. 모든 값 동일 + 신규 2배 (10x 미만) -> 정상
r = check_outlier([10.0] * 25, 20.0)
check("IQR=0, 신규=2x -> 정상 (10x 미만)", not r.flagged,
      f"reason={r.reason}")

# ── scan_group 반복 제거 ──
print("\n-- scan_group 반복 제거 --")

# 4-5. 이상치 2개 포함 -> 둘 다 탐지
prices = np.random.uniform(5, 15, size=28).tolist() + [150.0, 200.0]
outliers = scan_group(prices)
removed_vals = [v for v, _ in outliers]
check("이상치 2개 탐지", len(outliers) == 2,
      f"found {len(outliers)}: {removed_vals}")
check("200 먼저 제거", removed_vals[0] == 200.0, f"first={removed_vals[0]}")
check("150 다음 제거", removed_vals[1] == 150.0, f"second={removed_vals[1]}")

# 4-6. 정상 데이터 -> 이상치 0개
prices_clean = np.random.uniform(5, 15, size=30).tolist()
outliers = scan_group(prices_clean)
check("정상 데이터 -> 0개", len(outliers) == 0, f"found {len(outliers)}")

# 4-7. 이상치 3개
prices = np.random.uniform(10, 20, size=27).tolist() + [300.0, 400.0, 500.0]
outliers = scan_group(prices)
removed_vals = [v for v, _ in outliers]
check("이상치 3개 탐지", len(outliers) == 3,
      f"found {len(outliers)}: {removed_vals}")

# 4-8. n < 5 -> 빈 리스트
outliers = scan_group([10.0, 11.0, 12.0])
check("n<5 -> empty", len(outliers) == 0)

# 4-9. 이상치 1개만 (max)
prices = np.random.uniform(10, 20, size=29).tolist() + [500.0]
outliers = scan_group(prices)
check("이상치 1개만", len(outliers) == 1 and outliers[0][0] == 500.0,
      f"found {len(outliers)}")

# 4-10. 이상치가 min 쪽 (매우 낮은 가격, 좁은 범위)
prices = np.random.uniform(100, 110, size=29).tolist() + [1.0]
outliers = scan_group(prices)
removed_vals = [v for v, _ in outliers]
check("min 쪽 이상치", 1.0 in removed_vals, f"removed={removed_vals}")

# ── 회귀 테스트 ──
print("\n-- 회귀 테스트 --")

# 4-11. n>=20 기본
prices_30 = np.random.uniform(5, 15, size=30).tolist()
r = check_outlier(prices_30, 150.0)
check("n>=20 이상치 (회귀)", r.flagged)

r = check_outlier(prices_30, 10.0)
check("n>=20 정상 (회귀)", not r.flagged)

# 4-12. n<5 median
r = check_outlier([10.0, 11.0], 150.0)
check("n<5 이상치 (회귀)", r.flagged)

# 4-13. 5<=n<20 이중검증
prices_10 = np.random.uniform(5, 15, size=9).tolist()
r = check_outlier(prices_10, 150.0)
check("5<=n<20 이상치 (회귀)", r.flagged)

print(f"\n=== 4단계 결과: {passed} passed, {failed} failed ===")
if failed > 0:
    sys.exit(1)
print("4단계 통과. 5단계 진행 가능.")
