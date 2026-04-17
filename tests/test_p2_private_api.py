"""
test_p2_private_api.py — /api/p2/price-analyze 엔드포인트 통합 테스트

httpx/Starlette TestClient로 FastAPI 앱을 메모리에서 부팅하고,
환율/LLM/Supabase 호출은 가볍게 monkeypatch한다.
"""

from __future__ import annotations

import io
import json
import os
import sys

# 프로젝트 루트 + assets/snippets 경로 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(request):
    """FastAPI 앱을 한 번만 부팅하고 TestClient로 감싼다.

    server.py는 import 시점에 STATIC_DIR 존재를 확인하므로, 작업 디렉토리 의존은 없다.
    """
    from frontend import server as srv

    # Supabase/Claude/환율은 모두 off 상태로 (의존 서비스 없이도 동작해야 한다)
    def _no_sb():
        return None

    def _no_llm():
        return None

    def _stub_fx():
        return {"ok": True, "sar_krw": 390.0, "sar_usd": 0.267, "usd_krw": 1460.0, "source": "test"}

    srv._get_supabase = _no_sb          # type: ignore
    srv._get_llm = _no_llm              # type: ignore
    srv._fetch_exchange_rates = _stub_fx  # type: ignore

    with TestClient(srv.app) as c:
        yield c


def _valid_report_payload() -> dict:
    return {
        "trade_name": "Atmeg Combigel",
        "ingredient": "Atorvastatin + Omega-3",
        "strength": "20mg",
        "dosage_form": "capsule",
        "hs_code": "HS 3004",
        "price_sar": 120.0,
        "price_comparison": {
            "same_ingredient": [
                {"trade_name": "Lipitor", "strength": "20mg", "price": 150.0, "currency": "SAR"},
                {"trade_name": "Crestor", "strength": "10mg", "price": 130.0, "currency": "SAR"},
                {"trade_name": "RivaGen", "strength": "20mg", "price": 90.0,  "currency": "SAR"},
                {"trade_name": "BudgetG", "strength": "20mg", "price": 60.0,  "currency": "SAR"},
            ],
            "competitors": [],
        },
    }


class TestPrivateWithReportData:
    def test_success(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "ai",
                "market_type": "private",
                "report_data": json.dumps(_valid_report_payload()),
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["market_type"] == "private"
        assert body["product"]["trade_name"] == "Atmeg Combigel"
        assert len(body["scenarios"]) == 3
        assert body["competitor_stats"]["count"] >= 4
        for s in body["scenarios"]:
            assert "steps" in s
            assert "fob" in s

    def test_overrides_are_applied(self, client):
        overrides = {"aggressive": {"agent_commission_pct": 0.25, "freight_multiplier": 2.0}}
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "ai",
                "market_type": "private",
                "report_data": json.dumps(_valid_report_payload()),
                "overrides": json.dumps(overrides),
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        agg = next(s for s in body["scenarios"] if s["scenario"] == "aggressive")
        assert agg["agent_commission_pct"] == pytest.approx(0.25, rel=1e-6)
        assert agg["freight_multiplier"] == pytest.approx(2.0, rel=1e-6)

    def test_invalid_report_data_json(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "ai",
                "market_type": "private",
                "report_data": "not a json {",
            },
        )
        assert res.status_code == 400
        assert "JSON" in res.json()["detail"]


class TestPrivateWithLegacyReportOnly:
    """report_id만 보냈지만 report_data가 빈 경우 — 거절되어야 한다."""

    def test_blocked_without_report_data(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "ai",
                "market_type": "private",
                "report_id": "12345",
            },
        )
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False
        assert "report_data" in body["detail"] or "1공정" in body["detail"]


class TestPrivateManualBlocked:
    def test_manual_private_rejected(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "manual",
                "market_type": "private",
                "manual_product": "Gadvoa Inj.",
            },
        )
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False


class TestPrivateWithPdf:
    def test_pdf_empty_bytes_falls_through(self, client):
        # 실제 PDF 파싱 없이 bytes만 첨부 → Claude 없으므로 실패 메시지
        pdf_bytes = b"%PDF-1.4\n% fake\n"
        res = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "ai", "market_type": "private"},
            files={"pdf": ("x.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        )
        # pdfplumber가 텍스트를 못 뽑거나, LLM이 없으면 notes에 안내 후 400 응답
        assert res.status_code == 400
        body = res.json()
        assert body["ok"] is False

    def test_non_pdf_rejected(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "ai", "market_type": "private"},
            files={"pdf": ("x.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert res.status_code == 400


class TestPublicStub:
    def test_public_still_returns_stub(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={
                "input_mode": "ai",
                "market_type": "public",
                "report_data": json.dumps(_valid_report_payload()),
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["market_type"] == "public"
        assert body["status"] == "stub"


class TestInputValidation:
    def test_bad_input_mode(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "xxx", "market_type": "private"},
        )
        assert res.status_code == 422

    def test_bad_market_type(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "ai", "market_type": "foo"},
        )
        assert res.status_code == 422

    def test_ai_without_anything(self, client):
        res = client.post(
            "/api/p2/price-analyze",
            data={"input_mode": "ai", "market_type": "private"},
        )
        assert res.status_code == 400
