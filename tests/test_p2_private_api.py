import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import frontend.server as server


STATIC_RATES = {"sar_usd": 1 / 3.75, "usd_krw": 1472.0, "source": "test"}


def _sample_report_data():
    return {
        "trade_name": "ApiSample",
        "inn": "Apipril",
        "dosage_form": "tablet",
        "strength": "500 mg",
        "price_sar": 220.0,
        "hs_code": "HS 3004",
        "price_comparison": {
            "same_ingredient": [
                {"trade_name": "ApiSample", "strength": "500 mg", "price": 220.0, "source": "selected"},
                {"trade_name": "Comp A", "strength": "500 mg", "price": 180.0, "source": "same"},
                {"trade_name": "Comp B", "strength": "500 mg", "price": 260.0, "source": "same"},
            ],
            "competitors": [
                {"trade_name": "Comp C", "strength": "500 mg", "price": 120.0, "source": "competitor"},
            ],
        },
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_fetch_exchange_rates", lambda: STATIC_RATES)
    monkeypatch.setattr(server, "_get_llm", lambda: None)
    with TestClient(server.app) as test_client:
        yield test_client


def test_private_report_data_success_uses_fallback_without_claude(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={
            "input_mode": "ai",
            "market_type": "private",
            "report_id": "123",
            "report_data": json.dumps(_sample_report_data()),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["market_type"] == "private"
    assert payload["classification"]["product_kind"] == "generic"
    assert any("Claude 미설정" in warning for warning in payload["classification"]["warnings"])


def test_private_pdf_success_calls_pipeline(monkeypatch):
    captured = {}

    def fake_run_private_pipeline(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "market_type": "private",
            "product": {"trade_name": "From PDF", "inn": "", "dosage_form": "", "strength": "", "hs_code": None},
            "classification": {
                "product_kind": "generic",
                "is_combination": False,
                "is_extended_release": False,
                "premium_factor": 0.0,
                "dosage_ratio": 1.0,
                "freight_base_sar_per_unit": 3.0,
                "rationale": "test",
                "warnings": [],
            },
            "competitor_stats": {"count": 1, "min": 10.0, "max": 10.0, "avg": 10.0, "p25": 10.0, "median": 10.0, "p75": 10.0, "warning": None},
            "exchange_rates": STATIC_RATES,
            "scenarios": {
                "aggressive": {"label": "공격적", "entry_rank": 1, "retail_basis": "p75", "retail_sar": 10.0, "tier": 1, "margins": {"wholesale": 0.15, "pharmacy": 0.20}, "cif_original_sar": 7.25, "cap_factor": 0.7, "cif_after_cap_sar": 5.08, "dosage_ratio": 1.0, "dosage_adjustment_pct": 0.0, "cif_after_dosage_sar": 5.08, "combo_premium_pct": 0.0, "cif_final_sar": 5.08, "freight_multiplier": 1.0, "freight_insurance_sar": 3.0, "agent_commission_pct": 0.03, "port_fee_sar": 15.0, "steps": [], "fob_sar": 2.02, "fob_usd": 0.54, "fob_krw": 795, "error": None},
                "average": {"label": "평균", "entry_rank": 2, "retail_basis": "median", "retail_sar": 10.0, "tier": 1, "margins": {"wholesale": 0.15, "pharmacy": 0.20}, "cif_original_sar": 7.25, "cap_factor": 0.65, "cif_after_cap_sar": 4.71, "dosage_ratio": 1.0, "dosage_adjustment_pct": 0.0, "cif_after_dosage_sar": 4.71, "combo_premium_pct": 0.0, "cif_final_sar": 4.71, "freight_multiplier": 1.0, "freight_insurance_sar": 3.0, "agent_commission_pct": 0.05, "port_fee_sar": 15.0, "steps": [], "fob_sar": 1.63, "fob_usd": 0.43, "fob_krw": 634, "error": None},
                "conservative": {"label": "보수적", "entry_rank": 3, "retail_basis": "p25", "retail_sar": 10.0, "tier": 1, "margins": {"wholesale": 0.15, "pharmacy": 0.20}, "cif_original_sar": 7.25, "cap_factor": 0.6, "cif_after_cap_sar": 4.35, "dosage_ratio": 1.0, "dosage_adjustment_pct": 0.0, "cif_after_dosage_sar": 4.35, "combo_premium_pct": 0.0, "cif_final_sar": 4.35, "freight_multiplier": 1.0, "freight_insurance_sar": 3.0, "agent_commission_pct": 0.10, "port_fee_sar": 15.0, "steps": [], "fob_sar": 1.22, "fob_usd": 0.33, "fob_krw": 486, "error": None},
            },
            "regulatory_cost": {"sfda_registration_sar": 48_000.0, "saber_pcoc_annual_sar": 575.0, "saber_scoc_annual_sar": 12_000.0, "per_unit_amortization_sar": 6.06, "assumptions": {"annual_units": 10_000, "monthly_shipments": 2}},
            "notes": ["PDF test"],
        }

    monkeypatch.setattr(server, "_fetch_exchange_rates", lambda: STATIC_RATES)
    monkeypatch.setattr(server, "_get_llm", lambda: None)
    monkeypatch.setattr(server, "run_private_pipeline", fake_run_private_pipeline)

    with TestClient(server.app) as client:
        response = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "ai", "market_type": "private"},
            files={"pdf": ("sample.pdf", b"%PDF-1.4 mock", "application/pdf")},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["report_data"] is None
    assert captured["pdf_bytes"] == b"%PDF-1.4 mock"


def test_private_legacy_report_without_full_data_is_blocked(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={"input_mode": "ai", "market_type": "private", "report_id": "legacy-only"},
    )

    assert response.status_code == 400
    assert "전체 데이터" in response.json()["detail"]


def test_private_manual_mode_is_blocked(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={"input_mode": "manual", "market_type": "private", "manual_product": "ApiSample"},
    )

    assert response.status_code == 400
    assert "직접 입력" in response.json()["detail"]


def test_public_manual_without_price_data_returns_400(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={"input_mode": "manual", "market_type": "public", "manual_product": "ApiSample"},
    )

    assert response.status_code == 400
    detail = response.json().get("detail", "")
    assert "가격" in detail or "샘플" in detail


def test_public_ai_with_report_data_returns_scenarios(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={
            "input_mode": "ai",
            "market_type": "public",
            "report_id": "1",
            "report_data": json.dumps(_sample_report_data()),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["market_type"] == "public"
    assert "scenarios" in payload
    assert "aggressive" in payload["scenarios"]


def test_private_ai_agatri_without_prices_returns_benchmark(client):
    response = client.post(
        "/api/p2/price-analyze",
        data={
            "input_mode": "ai",
            "market_type": "private",
            "report_id": "agatri",
            "report_data": json.dumps(
                {
                    "trade_name": "Agatri",
                    "ingredient": "Agastache rugosa extract",
                    "drug_type": "Health functional food / inner beauty ingredient",
                    "dosage_form": "powder",
                    "price_comparison": {"same_ingredient": [], "competitors": []},
                }
            ),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["competitor_stats"]["estimated_only"] is True
    assert any(src["source"] == "health_functional_benchmark" for src in payload["price_pool_sources"])


def test_report_download_rejects_path_traversal():
    with TestClient(server.app) as client:
        response = client.get("/api/p2/report/download", params={"filename": "../.env"})

    assert response.status_code == 404
