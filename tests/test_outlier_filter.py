"""Phase 1-3 단위 테스트: IQR/K 통계량 기반 가격 이상치 필터.

검증 포커스:
    - n<5 구간: median 10배 룰 작동
    - n>=20 구간: K > threshold & extreme 판정
    - scan_group 반복 제거
    - flag_record 파이프라인 통합
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from assets.snippets.outlier_detector import (
    calc_k, check_outlier, flag_record, get_threshold, scan_group,
)


# ─── calc_k / get_threshold ───────────────────────────────────────

class TestCalcK:
    def test_uniform_iqr_zero(self):
        import numpy as np
        assert calc_k(np.array([10.0, 10.0, 10.0, 10.0])) == float("inf")

    def test_normal_range(self):
        import numpy as np
        data = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        k = calc_k(data)
        assert k > 0 and k < 10


class TestGetThreshold:
    def test_small_n(self):
        t = get_threshold(5, alpha=0.05)
        # n<20 은 20 기준 사용
        assert t == 4.934

    def test_exact_match(self):
        assert get_threshold(100, alpha=0.05) == 4.914

    def test_very_large(self):
        assert get_threshold(5000, alpha=0.05) == 5.540


# ─── check_outlier ────────────────────────────────────────────────

class TestCheckOutlier:
    def test_first_record_passes(self):
        """그룹에 가격이 없으면 skip."""
        r = check_outlier([], 100.0)
        assert r.flagged is False
        assert r.method == "skip"

    def test_median_ratio_small_n(self):
        """n<5 에서 median*10 초과는 flag."""
        r = check_outlier([50.0, 55.0, 52.0], 600.0)  # median=52, ratio~11.5x
        assert r.flagged is True
        assert r.method == "median_ratio"

    def test_normal_small_n(self):
        """n<5 에서 정상 범위는 pass."""
        r = check_outlier([50.0, 55.0, 52.0], 60.0)
        assert r.flagged is False

    def test_zero_price_invalid(self):
        r = check_outlier([50.0, 55.0], 0.0)
        assert r.flagged is True
        assert r.method == "invalid"

    def test_negative_price_invalid(self):
        r = check_outlier([50.0, 55.0], -10.0)
        assert r.flagged is True

    def test_large_group_normal_values(self):
        """n>=20 균일 분포 + 약간 큰 값 → 정상."""
        prices = [50.0 + i for i in range(25)]  # 50~74
        r = check_outlier(prices, 76.0)
        assert r.flagged is False

    def test_large_group_extreme_outlier(self):
        """n>=20 에서 median*100 이상은 flag."""
        prices = [50.0 + i for i in range(25)]
        r = check_outlier(prices, 50000.0)
        assert r.flagged is True


# ─── scan_group ───────────────────────────────────────────────────

class TestScanGroup:
    def test_small_sample_returns_empty(self):
        """n<5 는 scan 대상 아님."""
        assert scan_group([10.0, 20.0, 30.0]) == []

    def test_no_outliers_in_normal_group(self):
        """균일한 분포는 이상치 없음."""
        prices = [50.0 + i * 0.5 for i in range(30)]
        outs = scan_group(prices)
        # 완전히 균일한 등차수열에서는 0~1개 outlier 만 허용
        assert len(outs) <= 2

    def test_detects_obvious_outlier(self):
        """median 10000배 수준의 값은 반드시 감지."""
        prices = [50.0 + i for i in range(30)] + [5_000_000.0]
        outs = scan_group(prices)
        assert len(outs) >= 1
        assert outs[0][0] == 5_000_000.0


# ─── flag_record ──────────────────────────────────────────────────

class TestFlagRecord:
    def test_numeric_price_normal(self):
        rec = {"price_sar": 52.0, "inn_name": "paracetamol"}
        out = flag_record(rec, [50.0, 51.0, 53.0])
        assert out["outlier_flagged"] is False
        assert out["anomaly_reason"] is None

    def test_numeric_price_outlier(self):
        rec = {"price_sar": 600.0, "inn_name": "paracetamol"}
        out = flag_record(rec, [50.0, 52.0, 53.0])  # median=52, ratio~11.5x
        assert out["outlier_flagged"] is True
        assert out["anomaly_reason"] is not None

    def test_missing_price(self):
        rec = {"price_sar": None, "inn_name": "x"}
        out = flag_record(rec, [50.0])
        assert out["outlier_flagged"] is False

    def test_nonnumeric_price(self):
        rec = {"price_sar": "not-a-number", "inn_name": "x"}
        out = flag_record(rec, [50.0])
        assert out["outlier_flagged"] is True
        assert "not numeric" in out["anomaly_reason"]

    def test_prefers_price_local(self):
        """price_local 이 있으면 우선 사용."""
        rec = {"price_local": 100.0, "price_sar": 99.0}
        out = flag_record(rec, [50.0, 51.0, 52.0])
        # price_local=100 vs median=51 → ratio~2x → 정상
        assert out["outlier_flagged"] is False
