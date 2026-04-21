"""Phase 4-2 단위 테스트: Tender Power 스코어링."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.tender_power import (
    TenderRecord, compute_score, compute_tender_power_for_agents,
    contracts_rows_to_records, nupco_awards_rows_to_records, score_band,
    _category_matches_atc,
)


# ─── TenderRecord ────────────────────────────────────────────────

class TestTenderRecord:
    def test_normalized_name(self):
        r = TenderRecord(supplier_name="Tabuk Pharmaceuticals Co., Ltd.", value_sar=1.0, date="2024-01-01")
        assert r.normalized_name == "tabuk pharmaceuticals"

    def test_is_within_date(self):
        r = TenderRecord(supplier_name="X", value_sar=1.0, date="2023-01-01")
        assert r.is_within(datetime(2022, 1, 1)) is True
        assert r.is_within(datetime(2024, 1, 1)) is False

    def test_is_within_missing_date(self):
        """date 가 None 이면 보수적으로 True."""
        r = TenderRecord(supplier_name="X", value_sar=1.0, date=None)
        assert r.is_within(datetime(2024, 1, 1)) is True


# ─── 어댑터 ──────────────────────────────────────────────────────

class TestAdapters:
    def test_contracts_adapter(self):
        rows = [
            {"supplier_name": "Tabuk Pharma", "contract_value": "5000000",
             "start_date": "2024-06-01", "category": "insulin"},
            {"supplier_name": "", "contract_value": 100, "start_date": "2024-01-01"},
        ]
        recs = contracts_rows_to_records(rows)
        assert len(recs) == 1   # empty supplier dropped
        assert recs[0].value_sar == 5_000_000.0
        assert recs[0].source == "etimad"

    def test_awards_adapter(self):
        rows = [
            {"winner_name": "Jazeera", "award_value": 3_000_000, "award_date": "2024-05-10"},
        ]
        recs = nupco_awards_rows_to_records(rows)
        assert len(recs) == 1
        assert recs[0].source == "nupco"


# ─── 단일 에이전트 점수 ────────────────────────────────────────────

class TestComputeScore:
    def test_single_contract(self):
        recs = [TenderRecord(supplier_name="A", value_sar=1_000_000, date="2025-01-01", source="etimad")]
        s = compute_score(recs, now=datetime(2026, 1, 1))
        assert s.count_last_2y == 1
        assert s.total_value_mn_sar == 1.0
        assert s.score > 0

    def test_scaling_with_volume(self):
        """계약 수/금액이 늘면 점수도 증가 (단조성)."""
        now = datetime(2026, 1, 1)
        s1 = compute_score(
            [TenderRecord(supplier_name="A", value_sar=1_000_000, date="2025-01-01", source="etimad")],
            now=now,
        )
        s10 = compute_score(
            [TenderRecord(supplier_name="A", value_sar=1_000_000, date="2025-01-01", source="etimad")
             for _ in range(10)],
            now=now,
        )
        assert s10.score > s1.score

    def test_atc_match_adds_20(self):
        recs = [TenderRecord(supplier_name="A", value_sar=1_000_000, date="2025-01-01",
                             source="etimad", category="oral antidiabetic")]
        s_no = compute_score(recs, now=datetime(2026, 1, 1))
        s_yes = compute_score(recs, target_atc_l3="A10B", now=datetime(2026, 1, 1))
        assert s_yes.score > s_no.score
        assert s_yes.has_target_atc_match is True
        assert s_no.has_target_atc_match is False

    def test_score_clamped_to_100(self):
        """아무리 크더라도 100 초과 안 함."""
        recs = [
            TenderRecord(supplier_name="A", value_sar=100_000_000, date="2025-01-01",
                         source="etimad", category="diabetes")
            for _ in range(50)
        ]
        s = compute_score(recs, target_atc_l3="A10B", now=datetime(2026, 1, 1))
        assert s.score <= 100.0

    def test_empty_records(self):
        s = compute_score([])
        assert s.score == 0.0
        assert s.count_last_2y == 0

    def test_window_filter(self):
        """2년 창 밖의 레코드는 무시."""
        old = TenderRecord(supplier_name="A", value_sar=100_000_000, date="2020-01-01", source="etimad")
        recent = TenderRecord(supplier_name="A", value_sar=1_000_000, date="2025-01-01", source="etimad")
        s = compute_score([old, recent], now=datetime(2026, 1, 1))
        assert s.count_last_2y == 1
        assert s.total_value_sar == 1_000_000


# ─── 통합 함수: compute_tender_power_for_agents ───────────────────

class TestComputeTenderPowerForAgents:
    def test_fuzzy_match(self):
        """Etimad 'Tabuk Pharmaceutical Co., Ltd.' ↔ 'Tabuk Pharmaceuticals'."""
        agents = ["Tabuk Pharmaceuticals"]
        records = [
            TenderRecord(supplier_name="Tabuk Pharmaceutical Co., Ltd.",
                         value_sar=5_000_000, date="2025-01-01", source="etimad"),
        ]
        result = compute_tender_power_for_agents(agents, records, now=datetime(2026, 1, 1))
        assert result["Tabuk Pharmaceuticals"]["count_last_2y"] == 1

    def test_unmatched_supplier_tracked(self):
        agents = ["Known Pharma"]
        records = [TenderRecord(supplier_name="CompletelyDifferentInc",
                                value_sar=1_000_000, date="2025-01-01", source="etimad")]
        result = compute_tender_power_for_agents(agents, records, now=datetime(2026, 1, 1))
        assert result["__meta__"]["unmatched_supplier_count"] == 1
        assert result["Known Pharma"]["score"] == 0.0

    def test_score_ordering(self):
        """많은 계약을 가진 에이전트가 더 높은 점수."""
        agents = ["BigAgent", "SmallAgent"]
        records = [
            TenderRecord(supplier_name="BigAgent", value_sar=10_000_000, date="2025-01-01", source="etimad")
            for _ in range(10)
        ] + [
            TenderRecord(supplier_name="SmallAgent", value_sar=1_000_000, date="2025-01-01", source="etimad"),
        ]
        result = compute_tender_power_for_agents(agents, records, now=datetime(2026, 1, 1))
        assert result["BigAgent"]["score"] > result["SmallAgent"]["score"]


# ─── ATC 카테고리 매칭 ─────────────────────────────────────────────

class TestCategoryMatchesATC:
    def test_insulin_matches_A10A(self):
        assert _category_matches_atc("Supply of insulin products", "A10A") is True

    def test_diabetes_matches_A10B(self):
        assert _category_matches_atc("Oral antidiabetic tablets", "A10B") is True

    def test_mismatch(self):
        assert _category_matches_atc("Insulin supply", "C09A") is False

    def test_empty_inputs(self):
        assert _category_matches_atc(None, "A10B") is False
        assert _category_matches_atc("insulin", None) is False


# ─── score_band ──────────────────────────────────────────────────

class TestScoreBand:
    def test_strong(self):
        assert score_band(85.0) == "strong"
        assert score_band(70.0) == "strong"

    def test_mid(self):
        assert score_band(60.0) == "mid"
        assert score_band(40.0) == "mid"

    def test_weak(self):
        assert score_band(20.0) == "weak"
        assert score_band(1.0) == "weak"

    def test_none(self):
        assert score_band(0.0) == "none"
