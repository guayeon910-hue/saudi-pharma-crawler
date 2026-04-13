"""
outlier_detector.py -- K 통계량(IQR Relative Range) 기반 가격 이상치 탐지

논문: "Empirical Evaluation of the Relative Range for Detecting Outliers"
      Dallah et al., Entropy 2025, Vol.27, No.731

적용 범위:
  - products 테이블 INSERT 직전, 같은 (inn_name, country, dosage_form) 그룹 내
    신규 가격이 이상치인지 판정
  - 플래그만 달고 INSERT는 수행 (삭제 안 함)

적용 제한:
  - n < 5: K 불안정 -> median 대비 배수 룰로 대체
  - 2배 이하 차이: K로 탐지 불가, 비즈니스 룰 별도
  - 통화 혼입: 크롤러 단에서 currency 필드로 처리
"""

from __future__ import annotations

import numpy as np
from typing import NamedTuple


# ── 논문 Table 1: 정규분포 K 임계값 ─────────────────────────
K_THRESHOLDS_NORMAL: dict[int, dict[float, float]] = {
    20:   {0.01: 6.341, 0.05: 4.934, 0.10: 4.376},
    30:   {0.01: 5.981, 0.05: 4.872, 0.10: 4.397},
    50:   {0.01: 5.663, 0.05: 4.842, 0.10: 4.468},
    75:   {0.01: 5.549, 0.05: 4.872, 0.10: 4.552},
    100:  {0.01: 5.543, 0.05: 4.914, 0.10: 4.622},
    250:  {0.01: 5.600, 0.05: 5.121, 0.10: 4.889},
    500:  {0.01: 5.732, 0.05: 5.318, 0.10: 5.117},
    1000: {0.01: 5.912, 0.05: 5.540, 0.10: 5.350},
}

_SORTED_N = sorted(K_THRESHOLDS_NORMAL.keys())


class OutlierResult(NamedTuple):
    flagged: bool
    method: str        # "k_statistic" | "median_ratio" | "iqr_zero" | "skip"
    k_value: float     # K 통계량 (해당 없으면 0.0)
    threshold: float   # 사용한 임계값
    reason: str        # 사람이 읽을 수 있는 설명


def calc_k(data: np.ndarray) -> float:
    """K = Range / IQR. IQR=0이면 inf 반환."""
    r = float(np.max(data) - np.min(data))
    q1 = float(np.percentile(data, 25))
    q3 = float(np.percentile(data, 75))
    iqr = q3 - q1
    if iqr == 0:
        return float("inf")
    return r / iqr


def get_threshold(n: int, alpha: float = 0.05) -> float:
    """샘플 크기 n에 가장 가까운(이상) 임계값 반환."""
    for k in _SORTED_N:
        if k >= n:
            return K_THRESHOLDS_NORMAL[k][alpha]
    return K_THRESHOLDS_NORMAL[_SORTED_N[-1]][alpha]


def check_outlier(existing_prices: list[float], new_price: float,
                  alpha: float = 0.05) -> OutlierResult:
    """신규 가격 1건이 기존 그룹 대비 이상치인지 판정.

    Args:
        existing_prices: 같은 (inn_name, country, dosage_form) 그룹의 기존 가격 목록
        new_price: 신규 INSERT할 가격
        alpha: 유의수준 (0.05 권장)

    Returns:
        OutlierResult: flagged=True면 outlier_flagged=true로 INSERT
    """
    if new_price <= 0:
        return OutlierResult(True, "invalid", 0.0, 0.0,
                             f"price={new_price}: 0 이하 가격")

    all_prices = np.array(existing_prices + [new_price], dtype=float)
    n = len(all_prices)

    # ── n < 5: K/IQR 수학적 불안정 -> median 배수 룰 ──
    MEDIAN_RATIO_THRESHOLD = 10.0
    if n < 5:
        if len(existing_prices) == 0:
            return OutlierResult(False, "skip", 0.0, 0.0,
                                 "first record in group, no comparison")
        median = float(np.median(existing_prices))
        if median == 0:
            return OutlierResult(False, "skip", 0.0, 0.0,
                                 "median=0, cannot compute ratio")
        ratio = new_price / median
        if ratio > MEDIAN_RATIO_THRESHOLD or ratio < (1.0 / MEDIAN_RATIO_THRESHOLD):
            return OutlierResult(True, "median_ratio", 0.0, MEDIAN_RATIO_THRESHOLD,
                                 f"n={n}<5, median={median:.2f}, "
                                 f"ratio={ratio:.1f}x (>{MEDIAN_RATIO_THRESHOLD}x)")
        return OutlierResult(False, "median_ratio", 0.0, MEDIAN_RATIO_THRESHOLD,
                             f"n={n}<5, median={median:.2f}, ratio={ratio:.1f}x, normal")

    # ── 5 <= n < 20: 논문 범위 밖, 보수적 K + median 이중 검증 ──
    SMALL_GROUP_MEDIAN_THRESHOLD = 5.0
    if n < 20:
        median = float(np.median(existing_prices))
        if median == 0:
            return OutlierResult(False, "skip", 0.0, 0.0,
                                 "median=0, cannot compute ratio")
        ratio = new_price / median
        # K도 계산하되, n=20 임계값 사용 (보수적)
        k_val = calc_k(all_prices)
        thresh = get_threshold(20, alpha)
        # 이중 조건: K 초과 AND median 대비 5배 초과
        is_extreme = (new_price >= np.max(all_prices) or
                      new_price <= np.min(all_prices))
        if (k_val > thresh and is_extreme and
                (ratio > SMALL_GROUP_MEDIAN_THRESHOLD or
                 ratio < (1.0 / SMALL_GROUP_MEDIAN_THRESHOLD))):
            return OutlierResult(True, "k_statistic+median", k_val, thresh,
                                 f"n={n}<20, K={k_val:.2f}>{thresh:.2f}, "
                                 f"ratio={ratio:.1f}x (>{SMALL_GROUP_MEDIAN_THRESHOLD}x)")
        return OutlierResult(False, "k_statistic+median", k_val, thresh,
                             f"n={n}<20, K={k_val:.2f}, ratio={ratio:.1f}x, normal")

    # ── n >= 20: K 통계량 적용 (논문 검증 범위) ──
    k_val = calc_k(all_prices)
    thresh = get_threshold(n, alpha)

    if k_val == float("inf"):
        # IQR=0: 기존 가격이 전부 동일한데 신규 가격이 다름
        if len(existing_prices) > 0:
            ref = existing_prices[0]
            if ref != 0 and new_price != ref:
                ratio = new_price / ref
                if ratio >= MEDIAN_RATIO_THRESHOLD or ratio <= (1.0 / MEDIAN_RATIO_THRESHOLD):
                    return OutlierResult(True, "iqr_zero", k_val, 0.0,
                                         f"IQR=0, all existing={ref}, "
                                         f"new={new_price}, ratio={ratio:.1f}x")
        return OutlierResult(False, "iqr_zero", k_val, 0.0,
                             f"IQR=0, prices similar enough")

    if k_val > thresh:
        # K 초과 = 그룹 내 이상치 존재. 신규 값이 max 또는 min인지 확인
        is_extreme = (new_price >= np.max(all_prices) or
                      new_price <= np.min(all_prices))
        if is_extreme:
            return OutlierResult(True, "k_statistic", k_val, thresh,
                                 f"K={k_val:.2f}>{thresh:.2f}, "
                                 f"new_price={new_price} is extreme")
        else:
            return OutlierResult(False, "k_statistic", k_val, thresh,
                                 f"K={k_val:.2f}>{thresh:.2f}, "
                                 f"but new_price={new_price} is not extreme")

    # K 미초과지만, 넓은 분포에서 극단적 값은 median ratio로 보조 검사
    # (LBP 50000~200000 범위에서 min=5 같은 케이스)
    EXTREME_MEDIAN_RATIO = 100.0  # median 대비 100배 이상이면 무조건 이상치
    if len(existing_prices) > 0:
        median = float(np.median(existing_prices))
        if median > 0:
            ratio = new_price / median
            is_extreme = (new_price >= np.max(all_prices) or
                          new_price <= np.min(all_prices))
            if is_extreme and (ratio >= EXTREME_MEDIAN_RATIO or
                               ratio <= (1.0 / EXTREME_MEDIAN_RATIO)):
                return OutlierResult(True, "median_extreme", k_val, thresh,
                                     f"K={k_val:.2f}<={thresh:.2f} but "
                                     f"median_ratio={ratio:.2f}x (>{EXTREME_MEDIAN_RATIO}x)")

    return OutlierResult(False, "k_statistic", k_val, thresh,
                         f"K={k_val:.2f}<={thresh:.2f}, normal")


def flag_record(record: dict, existing_prices: list[float],
                alpha: float = 0.05) -> dict:
    """normalize_record() 결과에 이상치 플래그를 추가.

    크롤러 파이프라인에서의 호출 순서:
      record = map_to_schema(item)
      record = normalize_record(record)
      record = inn.normalize_record(record)
      record = flag_record(record, existing_prices)  # <-- 여기
      db.upsert(record)

    원본을 수정하지 않고 사본을 반환.
    """
    out = dict(record)
    price = out.get("price_local") or out.get("price_sar")

    if price is None:
        out["outlier_flagged"] = False
        out["anomaly_reason"] = None
        return out

    try:
        price_float = float(price)
    except (TypeError, ValueError):
        out["outlier_flagged"] = True
        out["anomaly_reason"] = f"price_local not numeric: {price}"
        return out

    result = check_outlier(existing_prices, price_float, alpha)
    out["outlier_flagged"] = result.flagged
    out["anomaly_reason"] = result.reason if result.flagged else None
    return out


def scan_group(prices: list[float], alpha: float = 0.05,
               max_iter: int = 5) -> list[tuple[float, OutlierResult]]:
    """기존 그룹 전체를 스캔하여 이상치 목록 반환 (반복 제거 방식).

    배치 정리용. INSERT 직전이 아니라 기존 데이터 점검에 사용.
    논문 Algorithm 1의 반복 적용: K > threshold이면 극단값 제거 후 재검정.

    Returns:
        [(제거된 가격, OutlierResult), ...] 이상치로 판정된 값들
    """
    if len(prices) < 5:
        return []

    data = list(prices)
    outliers: list[tuple[float, OutlierResult]] = []

    for _ in range(max_iter):
        if len(data) < 5:
            break
        arr = np.array(data, dtype=float)
        k_val = calc_k(arr)

        if k_val == float("inf"):
            break

        thresh = get_threshold(len(data), alpha) if len(data) >= 20 else get_threshold(20, alpha)

        if k_val <= thresh:
            break

        # 극단값(max) 제거 — max가 median에서 더 먼 쪽
        median = float(np.median(arr))
        max_val = float(np.max(arr))
        min_val = float(np.min(arr))
        if abs(max_val - median) >= abs(min_val - median):
            removed = max_val
        else:
            removed = min_val

        result = OutlierResult(True, "k_statistic_scan", k_val, thresh,
                               f"scan: K={k_val:.2f}>{thresh:.2f}, removed={removed}")
        outliers.append((removed, result))
        data = [x for x in data if x != removed]

    return outliers
