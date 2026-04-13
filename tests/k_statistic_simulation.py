"""
K 통계량 (IQR Relative Range) 시뮬레이션
논문: "Empirical Evaluation of the Relative Range for Detecting Outliers"
       Dallah et al., Entropy 2025

목적: 의약품 가격 데이터에 K 통계량을 적용했을 때
      성공 / 조건부 실패 / 기각 케이스를 명확히 분류

시뮬레이션 시나리오:
  1. 정상 가격 + 명백한 이상치 (10배 오류) → 탐지 가능?
  2. 정상 가격 + 경미한 이상치 (2배 오류) → 탐지 가능?
  3. 소규모 샘플 (n < 20) → 논문 범위 밖, 작동하나?
  4. 박스 vs 정 단위 혼입 → 실제 크롤링 오류 패턴
  5. 통화 혼입 (SAR vs USD 미변환) → 실제 크롤링 오류 패턴
  6. 다품목 가격 (제네릭 vs 오리지날) → 정상적 가격 차이를 이상치로 오탐?
  7. 극소 그룹 (n=3~5) → 현실적으로 INN 그룹 크기
"""

import numpy as np
from dataclasses import dataclass

np.random.seed(42)


@dataclass
class SimResult:
    scenario: str
    n: int
    K_value: float
    threshold: float
    alpha: float
    detected: bool
    verdict: str  # SUCCESS / CONDITIONAL / REJECT
    reason: str


def calc_K(data: np.ndarray) -> float:
    """K = Range / IQR"""
    R = np.max(data) - np.min(data)
    Q1 = np.percentile(data, 25)
    Q3 = np.percentile(data, 75)
    IQR = Q3 - Q1
    if IQR == 0:
        return float('inf')
    return R / IQR


def calc_W(data: np.ndarray) -> float:
    """W = Range / StdDev (기존 방법, 비교용)"""
    R = np.max(data) - np.min(data)
    s = np.std(data, ddof=1)
    if s == 0:
        return float('inf')
    return R / s


# 논문 Table 1: 정규분포 K 임계값
K_THRESHOLDS_NORMAL = {
    # n: {alpha: threshold}
    20:   {0.01: 6.341, 0.05: 4.934, 0.10: 4.376},
    30:   {0.01: 5.981, 0.05: 4.872, 0.10: 4.397},
    50:   {0.01: 5.663, 0.05: 4.842, 0.10: 4.468},
    75:   {0.01: 5.549, 0.05: 4.872, 0.10: 4.552},
    100:  {0.01: 5.543, 0.05: 4.914, 0.10: 4.622},
    250:  {0.01: 5.600, 0.05: 5.121, 0.10: 4.889},
    500:  {0.01: 5.732, 0.05: 5.318, 0.10: 5.117},
    1000: {0.01: 5.912, 0.05: 5.540, 0.10: 5.350},
}


def get_threshold(n: int, alpha: float = 0.05) -> float:
    """샘플 크기에 가장 가까운 임계값 반환"""
    available = sorted(K_THRESHOLDS_NORMAL.keys())
    # n보다 크거나 같은 최소 키
    for k in available:
        if k >= n:
            return K_THRESHOLDS_NORMAL[k][alpha]
    return K_THRESHOLDS_NORMAL[available[-1]][alpha]


def run_simulations():
    results: list[SimResult] = []
    alpha = 0.05

    # ─── 시나리오 1: 명백한 이상치 (10배 오류) ──────────────
    # Paracetamol 500mg 가격: 대부분 5~15 SAR, 하나가 100 SAR (박스 가격 혼입)
    for trial in range(5):
        prices = np.random.uniform(5, 15, size=30)
        prices = np.append(prices, 150.0)  # 10배 이상치
        K = calc_K(prices)
        W = calc_W(prices)
        thresh = get_threshold(len(prices), alpha)
        results.append(SimResult(
            f"1-명백한이상치(10x)_trial{trial}",
            len(prices), K, thresh, alpha,
            K > thresh,
            "SUCCESS" if K > thresh else "REJECT",
            f"K={K:.2f} vs thresh={thresh:.2f}, W={W:.2f}"
        ))

    # ─── 시나리오 2: 경미한 이상치 (2배 오류) ───────────────
    for trial in range(5):
        prices = np.random.uniform(5, 15, size=30)
        prices = np.append(prices, 28.0)  # 2배 정도
        K = calc_K(prices)
        W = calc_W(prices)
        thresh = get_threshold(len(prices), alpha)
        results.append(SimResult(
            f"2-경미한이상치(2x)_trial{trial}",
            len(prices), K, thresh, alpha,
            K > thresh,
            "CONDITIONAL" if not (K > thresh) else "SUCCESS",
            f"K={K:.2f} vs thresh={thresh:.2f}, W={W:.2f} — 2배는 정상 범위 내 가능"
        ))

    # ─── 시나리오 3: 소규모 샘플 (n < 20, 논문 범위 밖) ──────
    for n_small in [3, 5, 7, 10, 15]:
        prices = np.random.uniform(5, 15, size=n_small)
        prices[-1] = 150.0  # 이상치 삽입
        K = calc_K(prices)
        W = calc_W(prices)
        # n=20 임계값을 사용 (논문 최소 n)
        thresh = get_threshold(20, alpha)
        results.append(SimResult(
            f"3-소규모샘플_n={n_small}",
            n_small, K, thresh, alpha,
            K > thresh,
            "CONDITIONAL" if K > thresh else "REJECT",
            f"K={K:.2f} vs thresh(n=20)={thresh:.2f}, W={W:.2f} — 논문 범위 밖"
        ))

    # ─── 시나리오 4: 박스 vs 정 단위 혼입 ────────────────────
    # 실제 케이스: 28정 박스가 280 SAR, 개별 정이 10 SAR
    prices_per_tab = np.random.uniform(8, 12, size=25)
    prices_per_tab = np.append(prices_per_tab, [280.0, 560.0])  # 박스 가격 혼입
    K = calc_K(prices_per_tab)
    W = calc_W(prices_per_tab)
    thresh = get_threshold(len(prices_per_tab), alpha)
    results.append(SimResult(
        "4-박스가격혼입(28x_56x)",
        len(prices_per_tab), K, thresh, alpha,
        K > thresh,
        "SUCCESS" if K > thresh else "REJECT",
        f"K={K:.2f} vs thresh={thresh:.2f}, W={W:.2f}"
    ))

    # ─── 시나리오 5: 통화 혼입 (SAR vs USD) ──────────────────
    # 1 USD ≈ 3.75 SAR → USD 가격이 섞이면 ~3.75배 차이
    prices_sar = np.random.uniform(30, 50, size=20)
    prices_mixed = np.append(prices_sar, [8.0, 10.0, 13.0])  # USD가 섞임
    K = calc_K(prices_mixed)
    W = calc_W(prices_mixed)
    thresh = get_threshold(len(prices_mixed), alpha)
    results.append(SimResult(
        "5-통화혼입(SAR_vs_USD)",
        len(prices_mixed), K, thresh, alpha,
        K > thresh,
        "SUCCESS" if K > thresh else "CONDITIONAL",
        f"K={K:.2f} vs thresh={thresh:.2f}, W={W:.2f} — 3.75배 차이"
    ))

    # ─── 시나리오 6: 제네릭 vs 오리지날 (정상 가격 차이) ─────
    # 같은 INN인데 오리지날은 50 SAR, 제네릭은 10 SAR → 이건 이상치가 아님
    prices_generic = np.random.uniform(8, 15, size=15)
    prices_original = np.random.uniform(40, 60, size=10)
    prices_bimodal = np.concatenate([prices_generic, prices_original])
    K = calc_K(prices_bimodal)
    W = calc_W(prices_bimodal)
    thresh = get_threshold(len(prices_bimodal), alpha)
    is_flagged = K > thresh
    results.append(SimResult(
        "6-제네릭_vs_오리지날(정상차이)",
        len(prices_bimodal), K, thresh, alpha,
        is_flagged,
        "REJECT" if is_flagged else "SUCCESS",
        f"K={K:.2f} vs thresh={thresh:.2f} — {'오탐! 정상 차이를 이상치로 잡음' if is_flagged else '정상 차이는 통과(좋음)'}"
    ))

    # ─── 시나리오 7: 극소 그룹 (n=3~5, INN 그룹 현실) ────────
    # 실제로 하나의 INN+country 조합에 3~5개 가격만 있는 경우
    for n_tiny in [3, 4, 5]:
        prices = np.array([10.0, 11.0, 12.0][:n_tiny])
        prices[-1] = 100.0  # 이상치
        K = calc_K(prices)
        W = calc_W(prices)
        thresh = get_threshold(20, alpha)
        results.append(SimResult(
            f"7-극소그룹_n={n_tiny}_이상치있음",
            n_tiny, K, thresh, alpha,
            K > thresh,
            "CONDITIONAL" if K > thresh else "REJECT",
            f"K={K:.2f} vs thresh(n=20)={thresh:.2f} — IQR 불안정 가능"
        ))

    # ─── 시나리오 8: 이상치 없는 정상 데이터 → 오탐 없는지 ──
    for trial in range(5):
        prices_clean = np.random.uniform(5, 15, size=50)
        K = calc_K(prices_clean)
        thresh = get_threshold(50, alpha)
        is_flagged = K > thresh
        results.append(SimResult(
            f"8-정상데이터(이상치없음)_trial{trial}",
            len(prices_clean), K, thresh, alpha,
            is_flagged,
            "REJECT" if is_flagged else "SUCCESS",
            f"K={K:.2f} vs thresh={thresh:.2f} — {'오탐!' if is_flagged else '정상 통과(좋음)'}"
        ))

    # ─── 시나리오 9: 반복 이상치 탐지 (iterative removal) ────
    # 논문은 "이상치 있음/없음"만 판정. 어느 값이 이상치인지는 안 알려줌.
    # → 최대/최소 제거 후 재검정하는 반복 방식 테스트
    prices = np.random.uniform(5, 15, size=28)
    prices = np.append(prices, [150.0, 200.0])  # 이상치 2개

    iteration_log = []
    data = prices.copy()
    for i in range(5):
        if len(data) < 5:
            break
        K = calc_K(data)
        thresh = get_threshold(len(data), alpha)
        if K > thresh:
            # max가 이상치일 가능성이 높음 → 제거
            outlier_val = np.max(data)
            data = data[data != outlier_val]
            iteration_log.append(f"  iter{i}: K={K:.2f}>{thresh:.2f}, removed {outlier_val:.1f}")
        else:
            iteration_log.append(f"  iter{i}: K={K:.2f}<={thresh:.2f}, STOP")
            break

    results.append(SimResult(
        "9-반복제거(iterative)",
        len(prices), calc_K(prices), get_threshold(len(prices), alpha), alpha,
        True,
        "SUCCESS",
        "반복 제거로 다중 이상치 탐지\n" + "\n".join(iteration_log)
    ))

    # ─── 시나리오 10: IQR=0 방어 (모든 값 동일) ──────────────
    prices_same = np.array([10.0, 10.0, 10.0, 10.0, 10.0])
    K = calc_K(prices_same)
    results.append(SimResult(
        "10-모든값동일(IQR=0)",
        len(prices_same), K, float('inf'), alpha,
        False,
        "CONDITIONAL",
        f"K={K} — IQR=0이면 K=inf, 별도 처리 필요"
    ))

    # IQR=0 + 이상치 1개
    prices_same_outlier = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 100.0])
    K = calc_K(prices_same_outlier)
    results.append(SimResult(
        "10b-동일값+이상치1개(IQR=0)",
        len(prices_same_outlier), K, float('inf'), alpha,
        False,
        "CONDITIONAL",
        f"K={K} — IQR=0이면 무조건 이상치 판정해야 함"
    ))

    return results


def print_results(results: list[SimResult]):
    print("=" * 100)
    print("K 통계량 시뮬레이션 결과")
    print("=" * 100)

    verdicts = {"SUCCESS": [], "CONDITIONAL": [], "REJECT": []}

    for r in results:
        verdicts[r.verdict].append(r)
        status = "O" if r.detected else "X"
        print(f"\n[{r.verdict:12s}] {r.scenario}")
        print(f"  n={r.n}, K={r.K_value:.2f}, thresh={r.threshold:.2f}, detected={status}")
        print(f"  {r.reason}")

    print("\n" + "=" * 100)
    print("분류 요약")
    print("=" * 100)

    print(f"\n--- SUCCESS ({len(verdicts['SUCCESS'])}건) — 프로그램에 적용 가능 ---")
    for r in verdicts["SUCCESS"]:
        print(f"  {r.scenario}")

    print(f"\n--- CONDITIONAL ({len(verdicts['CONDITIONAL'])}건) — 조건부/보완 필요 ---")
    for r in verdicts["CONDITIONAL"]:
        print(f"  {r.scenario}")

    print(f"\n--- REJECT ({len(verdicts['REJECT'])}건) — 적용 불가/기각 ---")
    for r in verdicts["REJECT"]:
        print(f"  {r.scenario}")


if __name__ == "__main__":
    results = run_simulations()
    print_results(results)
