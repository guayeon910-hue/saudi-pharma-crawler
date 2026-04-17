"""
test_fob_private.py — 민간 시장 FOB 역산 계산 모듈 단위 테스트
"""

from __future__ import annotations

import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from frontend.fob_private import (
    PHARMACY_MARGIN_HIGH,
    PHARMACY_MARGIN_LOW,
    PHARMACY_MARGIN_MID,
    THRESHOLD_HIGH_SAR,
    THRESHOLD_LOW_SAR,
    ProductClassification,
    apply_combination_premium,
    apply_dosage_adjustment,
    apply_generic_cap,
    compute_competitor_stats,
    compute_scenario,
    deduct_agent_commission,
    deduct_freight,
    normalize_competitor_pool,
    pharmacy_margin_for,
    reverse_margins_to_cif,
    run_private_pipeline,
)


class TestPharmacyMarginThresholds:
    """69/253 SAR 임계값 — 약국 실효 마진이 구간별로 바뀌는지."""

    def test_below_low_threshold(self):
        assert pharmacy_margin_for(50.0) == PHARMACY_MARGIN_LOW

    def test_at_low_boundary(self):
        # 경계값 69는 "< 69"가 False이므로 MID 구간
        assert pharmacy_margin_for(THRESHOLD_LOW_SAR) == PHARMACY_MARGIN_MID

    def test_between_thresholds(self):
        assert pharmacy_margin_for(150.0) == PHARMACY_MARGIN_MID

    def test_at_high_boundary(self):
        assert pharmacy_margin_for(THRESHOLD_HIGH_SAR) == PHARMACY_MARGIN_HIGH

    def test_above_high(self):
        assert pharmacy_margin_for(1000.0) == PHARMACY_MARGIN_HIGH


class TestReverseMargins:
    """복리 마진 역산."""

    def test_retail_to_cif_compound(self):
        # retail 50 SAR (LOW 구간, pm=20%), wholesaler 10%, vat 0
        # CIF = 50 / (1.1 * 1.2 * 1.0) = 50 / 1.32 ≈ 37.88
        cif = reverse_margins_to_cif(50.0, vat_pct=0.0)
        assert cif == pytest.approx(50.0 / 1.32, rel=1e-6)

    def test_zero_retail_returns_zero(self):
        assert reverse_margins_to_cif(0.0) == 0.0
        assert reverse_margins_to_cif(-10.0) == 0.0

    def test_vat_applied(self):
        base = reverse_margins_to_cif(100.0, vat_pct=0.0)
        with_vat = reverse_margins_to_cif(100.0, vat_pct=0.15)
        assert with_vat < base

    def test_high_retail_uses_low_pharmacy_margin(self):
        # retail 500 SAR — HIGH 구간(pm=10%), 동일 retail이라도 더 높은 CIF
        cif_high = reverse_margins_to_cif(500.0)
        # 같은 가격 range에 LOW 구간 마진이 강제로 적용되면 더 낮은 CIF
        cif_low_forced = 500.0 / ((1.0 + 0.10) * (1.0 + PHARMACY_MARGIN_LOW))
        assert cif_high > cif_low_forced


class TestGenericCap:
    def test_generic_discount(self):
        assert apply_generic_cap(100.0, "generic") == pytest.approx(70.0)

    def test_biosimilar_discount(self):
        assert apply_generic_cap(100.0, "biosimilar") == pytest.approx(80.0)

    def test_originator_unchanged(self):
        assert apply_generic_cap(100.0, "originator") == 100.0

    def test_unknown_kind_unchanged(self):
        assert apply_generic_cap(100.0, "") == 100.0
        assert apply_generic_cap(100.0, "XYZ") == 100.0


class TestDosageAdjustment:
    def test_half_dose(self):
        assert apply_dosage_adjustment(100.0, 0.5) == pytest.approx(50.0)

    def test_double_dose(self):
        assert apply_dosage_adjustment(100.0, 2.0) == pytest.approx(200.0)

    def test_identity(self):
        assert apply_dosage_adjustment(100.0, 1.0) == 100.0

    def test_clipped_extremes(self):
        # ratio 0.1 → clip to 0.25
        assert apply_dosage_adjustment(100.0, 0.1) == pytest.approx(25.0)
        # ratio 10 → clip to 4.0
        assert apply_dosage_adjustment(100.0, 10.0) == pytest.approx(400.0)


class TestCombinationPremium:
    def test_combo_adds_premium(self):
        assert apply_combination_premium(100.0, True, 0.2) == pytest.approx(120.0)

    def test_not_combo_ignores_premium(self):
        assert apply_combination_premium(100.0, False, 0.5) == 100.0

    def test_premium_clipped(self):
        assert apply_combination_premium(100.0, True, 2.5) == pytest.approx(200.0)
        assert apply_combination_premium(100.0, True, -1.0) == 100.0


class TestFreightAndCommission:
    def test_freight_deduction(self):
        assert deduct_freight(100.0, 5.0, 1.0) == pytest.approx(95.0)
        assert deduct_freight(100.0, 5.0, 2.0) == pytest.approx(90.0)

    def test_freight_multiplier_clipped_negative(self):
        assert deduct_freight(100.0, 5.0, -1.0) == pytest.approx(100.0)

    def test_agent_commission_reverse(self):
        # pre_fob = FOB * (1 + agent_pct) 이므로 FOB = pre_fob / (1+pct)
        fob = deduct_agent_commission(105.0, 0.05)
        assert fob == pytest.approx(100.0)

    def test_agent_zero_pct(self):
        assert deduct_agent_commission(100.0, 0.0) == 100.0


class TestNegativeFobFallback:
    """음수 FOB는 fob_sar=None + error 메시지."""

    def test_negative_after_freight(self):
        cls = ProductClassification(freight_base_sar_per_unit=1000.0)
        # retail 20 SAR → CIF 약 15; freight 1000 → FOB 크게 음수
        result = compute_scenario(
            scenario_name="aggressive",
            retail_sar=20.0,
            classification=cls,
            freight_base_sar=1000.0,
            freight_mult=1.0,
            agent_commission_pct=0.03,
        )
        assert result["fob_sar"] is None
        assert result["error"] is not None


class TestCompetitorPool:
    def test_normalize_dedup(self):
        data = {
            "trade_name": "Gadvoa",
            "price_sar": 100.0,
            "price_comparison": {
                "same_ingredient": [
                    {"trade_name": "Gadovist", "strength": "604mg", "price": 120.0, "currency": "SAR"},
                    {"trade_name": "Gadovist", "strength": "604mg", "price": 120.0, "currency": "SAR"},  # dup
                    {"trade_name": "Multihance", "strength": "529mg", "price": 95.0},
                ],
                "competitors": [
                    {"trade_name": "Dotarem", "strength": "604mg", "price": 110.0},
                    {"trade_name": "NoPrice",  "strength": "0mg",    "price": 0.0},  # 0 제외
                ],
            },
        }
        pool = normalize_competitor_pool(data)
        names = sorted({p["trade_name"] for p in pool})
        assert "Gadvoa" in names
        assert "Gadovist" in names
        assert "Dotarem" in names
        assert "NoPrice" not in names
        # 중복 제거
        assert len([p for p in pool if p["trade_name"] == "Gadovist"]) == 1

    def test_stats_single_sample(self):
        pool = [{"trade_name": "x", "strength": "", "price_sar": 100.0, "origin": "report"}]
        stats = compute_competitor_stats(pool)
        assert stats["count"] == 1
        assert stats["p25"] == stats["median"] == stats["p75"] == 100.0
        assert stats["mode"] == "single"

    def test_stats_pair(self):
        pool = [
            {"trade_name": "a", "strength": "", "price_sar": 50.0, "origin": "r"},
            {"trade_name": "b", "strength": "", "price_sar": 150.0, "origin": "r"},
        ]
        stats = compute_competitor_stats(pool)
        assert stats["mode"] == "pair"
        assert stats["p25"] == 50.0
        assert stats["median"] == 100.0  # avg
        assert stats["p75"] == 150.0

    def test_stats_quartile(self):
        pool = [
            {"trade_name": f"x{i}", "strength": "", "price_sar": float(v), "origin": "r"}
            for i, v in enumerate([10, 20, 30, 40, 50])
        ]
        stats = compute_competitor_stats(pool)
        assert stats["mode"] == "quartile"
        assert stats["median"] == 30.0
        assert stats["p25"] < stats["median"] < stats["p75"]


class TestPipelineEndToEnd:
    """run_private_pipeline — 가장 현실적 E2E 시나리오 (LLM 없이 휴리스틱 경로)."""

    def test_end_to_end_happy(self):
        report_data = {
            "trade_name": "Atmeg Combigel",
            "ingredient": "Atorvastatin + Omega-3",
            "strength": "20mg",
            "dosage_form": "capsule",
            "hs_code": "HS 3004",
            "price_sar": 120.0,
            "price_comparison": {
                "same_ingredient": [
                    {"trade_name": "Lipitor",   "strength": "20mg", "price": 150.0, "currency": "SAR"},
                    {"trade_name": "Crestor",   "strength": "10mg", "price": 130.0, "currency": "SAR"},
                    {"trade_name": "RivaGen",   "strength": "20mg", "price": 90.0,  "currency": "SAR"},
                    {"trade_name": "BudgetGen", "strength": "20mg", "price": 60.0,  "currency": "SAR"},
                ],
                "competitors": [],
            },
        }
        fx = {"sar_krw": 390.0, "sar_usd": 0.267, "usd_krw": 1460.0, "source": "test"}
        out = run_private_pipeline(report_data=report_data, exchange_rates=fx, llm=None)
        assert out["ok"] is True
        assert out["product"]["trade_name"] == "Atmeg Combigel"
        assert out["classification"]["product_kind"] == "generic"
        assert out["competitor_stats"]["count"] >= 4
        assert len(out["scenarios"]) == 3
        ranks = [s["rank"] for s in out["scenarios"]]
        assert ranks == [1, 2, 3]
        # 공격적 < 보수적: retail base가 p75인 공격적 시나리오가 FOB가 더 커야 한다
        fobs = {s["scenario"]: (s.get("fob") or {}).get("sar") for s in out["scenarios"]}
        if fobs["aggressive"] and fobs["conservative"]:
            assert fobs["aggressive"] > fobs["conservative"]
        # 규제비 패널이 존재
        assert "sfda_amort_per_year_krw" in out["regulatory_cost"]
        # 시나리오에 port_fee가 포함되지만 FOB 값 자체에는 이미 차감되지 않았음
        for s in out["scenarios"]:
            assert s["port_fee_sar"] > 0

    def test_missing_report_data(self):
        out = run_private_pipeline(report_data=None, pdf_bytes=None, exchange_rates={}, llm=None)
        assert out["ok"] is False

    def test_empty_competitor_pool(self):
        out = run_private_pipeline(
            report_data={"trade_name": "X", "ingredient": "Y", "price_comparison": {"same_ingredient": []}},
            exchange_rates={},
            llm=None,
        )
        assert out["ok"] is False

    def test_overrides_applied(self):
        report_data = {
            "trade_name": "X",
            "ingredient": "Y",
            "strength": "100mg",
            "dosage_form": "tablet",
            "price_comparison": {
                "same_ingredient": [
                    {"trade_name": f"c{i}", "strength": "100mg", "price": float(50 + i * 10)}
                    for i in range(6)
                ],
            },
        }
        # 에이전트 수수료를 크게 올리면 FOB가 떨어져야 한다
        base = run_private_pipeline(report_data=report_data, exchange_rates={"sar_usd": 0.27, "sar_krw": 390}, llm=None)
        custom = run_private_pipeline(
            report_data=report_data,
            overrides={"aggressive": {"agent_commission_pct": 0.30}},
            exchange_rates={"sar_usd": 0.27, "sar_krw": 390},
            llm=None,
        )
        base_agg = next(s for s in base["scenarios"] if s["scenario"] == "aggressive")
        cust_agg = next(s for s in custom["scenarios"] if s["scenario"] == "aggressive")
        if base_agg["fob"] and cust_agg["fob"]:
            assert cust_agg["fob"]["sar"] < base_agg["fob"]["sar"]
