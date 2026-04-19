import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from frontend.fob_private import (
    apply_combo_premium,
    apply_dosage_adjustment,
    apply_generic_cap,
    compute_port_fee,
    compute_regulatory_amortization,
    reverse_retail_to_cif,
    run_private_pipeline,
    select_tier_by_retail,
)


STATIC_RATES = {"sar_usd": 1 / 3.75, "usd_krw": 1472.0, "source": "test"}


def _sample_report_data():
    return {
        "trade_name": "SampleTab",
        "inn": "Samplepril",
        "dosage_form": "tablet",
        "strength": "500 mg",
        "price_sar": 210.0,
        "hs_code": "HS 3004",
        "price_comparison": {
            "same_ingredient": [
                {"trade_name": "SampleTab", "strength": "500 mg", "price": 210.0, "source": "selected"},
                {"trade_name": "Comp A", "strength": "500 mg", "price": 160.0, "source": "same"},
                {"trade_name": "Comp B", "strength": "500 mg", "price": 240.0, "source": "same"},
            ],
            "competitors": [
                {"trade_name": "Comp C", "strength": "500 mg", "price": 120.0, "source": "competitor"},
                {"trade_name": "Comp D", "strength": "500 mg", "price": 300.0, "source": "competitor"},
            ],
        },
    }


def test_select_tier_by_retail_thresholds():
    assert select_tier_by_retail(69.0) == 1
    assert select_tier_by_retail(69.01) == 2
    assert select_tier_by_retail(253.0) == 2
    assert select_tier_by_retail(253.01) == 3


def test_reverse_retail_to_cif_matches_sfda_example():
    cif, tier, margins = reverse_retail_to_cif(51.75)
    assert tier == 1
    assert margins == pytest.approx((0.15, 0.20))
    assert cif == pytest.approx(37.5, abs=1e-6)


def test_generic_cap_dosage_adjustment_and_combo_premium():
    assert apply_generic_cap(100.0, "generic", 2) == pytest.approx(65.0)
    assert apply_generic_cap(100.0, "biosimilar", 3) == pytest.approx(55.0)
    assert apply_generic_cap(100.0, "innovative", 1) == pytest.approx(100.0)

    assert apply_dosage_adjustment(100.0, 2.0) == pytest.approx(164.0)
    assert apply_dosage_adjustment(100.0, 3.0) == pytest.approx(228.0)
    assert apply_dosage_adjustment(100.0, 4.0) == pytest.approx(280.0)

    assert apply_combo_premium(100.0, 0.2) == pytest.approx(120.0)


def test_port_fee_and_regulatory_amortization():
    assert compute_port_fee(1_000.0) == pytest.approx(15.0)
    assert compute_port_fee(50_000.0) == pytest.approx(75.0)
    assert compute_port_fee(200_000.0) == pytest.approx(130.0)

    reg = compute_regulatory_amortization(False, monthly_shipments=2, annual_units=10_000)
    assert reg["sfda_registration_sar"] == 48_000.0
    assert reg["per_unit_amortization_sar"] == pytest.approx(6.06)


def test_run_private_pipeline_returns_three_scenarios_and_fallback_warning():
    result = run_private_pipeline(
        report_data=_sample_report_data(),
        pdf_bytes=None,
        overrides=None,
        exchange_rates=STATIC_RATES,
        llm=None,
    )

    assert result["ok"] is True
    assert set(result["scenarios"].keys()) == {"aggressive", "average", "conservative"}
    assert result["classification"]["product_kind"] == "generic"
    assert any("Claude 미설정" in warning for warning in result["classification"]["warnings"])
    assert result["scenarios"]["aggressive"]["retail_sar"] >= result["scenarios"]["average"]["retail_sar"]
    assert result["scenarios"]["average"]["retail_sar"] >= result["scenarios"]["conservative"]["retail_sar"]
    assert result["scenarios"]["aggressive"]["fob_sar"] is not None


def test_run_private_pipeline_handles_negative_fob():
    report = {
        "trade_name": "LowPrice",
        "inn": "Lowpril",
        "dosage_form": "tablet",
        "strength": "10 mg",
        "price_sar": 5.0,
        "hs_code": "HS 3004",
        "price_comparison": {"same_ingredient": [{"trade_name": "LowPrice", "price": 5.0}], "competitors": []},
    }
    overrides = {
        "scenarios": {
            "aggressive": {"freight_multiplier": 2.0, "agent_commission_pct": 0.10},
            "average": {"freight_multiplier": 2.0, "agent_commission_pct": 0.10},
            "conservative": {"freight_multiplier": 2.0, "agent_commission_pct": 0.10},
        }
    }

    result = run_private_pipeline(
        report_data=report,
        pdf_bytes=None,
        overrides=overrides,
        exchange_rates=STATIC_RATES,
        llm=None,
    )

    for scenario in result["scenarios"].values():
        assert scenario["fob_sar"] is None
        assert scenario["error"]
