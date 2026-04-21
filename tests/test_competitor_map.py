"""Phase 5-1 단위 테스트: Competitor Agent Map."""

from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.competitor_map import (
    CompetitorAgent, CompetitorBrand, _normalize_brand_for_compare,
    build_competitor_map, enrich_with_tender_data, extract_competitor_agents,
)
from analytics.tender_power import TenderRecord


# ─── 유틸 ────────────────────────────────────────────────────────

class TestNormalizeBrandForCompare:
    def test_lowercase_and_alphanumeric(self):
        assert _normalize_brand_for_compare("Forxiga® 10mg") == "forxiga 10mg"

    def test_strip_punctuation(self):
        assert _normalize_brand_for_compare("Jardiance-XR") == "jardiance xr"

    def test_empty(self):
        assert _normalize_brand_for_compare("") == ""
        assert _normalize_brand_for_compare(None) == ""


# ─── extract_competitor_agents ───────────────────────────────────

class TestExtractCompetitorAgents:
    def test_basic_extraction(self):
        """간단한 SFDA 매칭 → 에이전트 3개 추출."""
        matches = [
            {"trade_name": "Forxiga", "agent_or_supplier": "AstraZeneca KSA",
             "price_sar": 120, "regulatory_id": "R1"},
            {"trade_name": "Xigduo", "agent_or_supplier": "AstraZeneca KSA",
             "price_sar": 150, "regulatory_id": "R2"},
            {"trade_name": "Jardiance", "agent_or_supplier": "Boehringer Ingelheim",
             "price_sar": 130, "regulatory_id": "R3"},
            {"trade_name": "Invokana", "agent_or_supplier": "Janssen KSA",
             "price_sar": 140, "regulatory_id": "R4"},
        ]
        agents = extract_competitor_agents(matches)
        assert len(agents) == 3
        az = next(a for a in agents if "astrazeneca" in a.normalized_name)
        assert az.brand_count == 2
        assert az.avg_price_sar == 135.0

    def test_self_brand_excluded(self):
        """target_brand 은 경쟁 목록에서 제외되어야 함."""
        matches = [
            {"trade_name": "Rosumeg", "agent_or_supplier": "MegaPharma KSA", "price_sar": 100},
            {"trade_name": "Forxiga", "agent_or_supplier": "AstraZeneca KSA", "price_sar": 120},
        ]
        agents = extract_competitor_agents(matches, target_brand="Rosumeg")
        # Rosumeg 는 제외 → MegaPharma 는 브랜드 0 → min_brand_count=1 필터링 → drop
        assert all("megapharma" not in a.normalized_name for a in agents)
        assert any("astrazeneca" in a.normalized_name for a in agents)

    def test_self_agent_excluded(self):
        """target_agent 은 맵에서 제외."""
        matches = [
            {"trade_name": "OurBrand", "agent_or_supplier": "Our Pharma", "price_sar": 100},
            {"trade_name": "Forxiga", "agent_or_supplier": "AstraZeneca KSA", "price_sar": 120},
        ]
        agents = extract_competitor_agents(matches, target_agent="Our Pharma")
        assert all("our pharma" not in a.normalized_name for a in agents)

    def test_empty_agent_rows_dropped(self):
        """agent_or_supplier 가 빈 문자열인 행은 추적 불가 → drop."""
        matches = [
            {"trade_name": "NoAgent", "agent_or_supplier": "", "price_sar": 100},
            {"trade_name": "Forxiga", "agent_or_supplier": "AstraZeneca", "price_sar": 120},
        ]
        agents = extract_competitor_agents(matches)
        assert len(agents) == 1

    def test_fuzzy_merges_similar_agents(self):
        """Tabuk Pharmaceuticals Co., Ltd. ↔ Tabuk Pharma LLC → 단일 버킷."""
        matches = [
            {"trade_name": "A", "agent_or_supplier": "Tabuk Pharmaceuticals Co., Ltd.", "price_sar": 100},
            {"trade_name": "B", "agent_or_supplier": "Tabuk Pharmaceuticals Co., Ltd.", "price_sar": 110},
            {"trade_name": "C", "agent_or_supplier": "Tabuk Pharma LLC", "price_sar": 120},
        ]
        agents = extract_competitor_agents(matches)
        tabuk = [a for a in agents if "tabuk" in a.normalized_name]
        assert len(tabuk) == 1, f"Expected single Tabuk entry, got {[t.display_name for t in tabuk]}"
        assert tabuk[0].brand_count == 3

    def test_share_normalization_sums_to_one(self):
        """market_share_est 의 합은 ~1.0."""
        matches = [
            {"trade_name": "A", "agent_or_supplier": "AgentA", "price_sar": 100},
            {"trade_name": "B", "agent_or_supplier": "AgentA", "price_sar": 110},
            {"trade_name": "C", "agent_or_supplier": "AgentB", "price_sar": 120},
            {"trade_name": "D", "agent_or_supplier": "AgentC", "price_sar": 150},
        ]
        agents = extract_competitor_agents(matches)
        total = sum(a.market_share_est for a in agents)
        assert abs(total - 1.0) < 0.01

    def test_min_brand_count_filter(self):
        """min_brand_count 이하 에이전트는 drop."""
        matches = [
            {"trade_name": "A", "agent_or_supplier": "AgentA", "price_sar": 100},
            {"trade_name": "B", "agent_or_supplier": "AgentA", "price_sar": 110},
            {"trade_name": "C", "agent_or_supplier": "AgentB", "price_sar": 120},  # 1개만
        ]
        agents = extract_competitor_agents(matches, min_brand_count=2)
        assert len(agents) == 1
        assert agents[0].brand_count == 2

    def test_empty_matches(self):
        agents = extract_competitor_agents([])
        assert agents == []


# ─── enrich_with_tender_data ─────────────────────────────────────

class TestEnrichWithTenderData:
    def test_tender_match_increases_share(self):
        """tender 실적이 매칭된 에이전트는 share 가 상승."""
        matches = [
            {"trade_name": "A", "agent_or_supplier": "AstraZeneca KSA", "price_sar": 100},
            {"trade_name": "B", "agent_or_supplier": "AstraZeneca KSA", "price_sar": 110},
            {"trade_name": "C", "agent_or_supplier": "Novartis", "price_sar": 120},
        ]
        agents_before = extract_competitor_agents(matches)
        az_before = next(a for a in agents_before if "astrazeneca" in a.normalized_name)
        share_before = az_before.market_share_est

        tender_records = [
            TenderRecord(supplier_name="AstraZeneca KSA", value_sar=5_000_000,
                         date="2025-01-01", source="etimad"),
            TenderRecord(supplier_name="AstraZeneca Arabia", value_sar=3_000_000,
                         date="2025-06-01", source="nupco"),
        ]
        agents_after = enrich_with_tender_data(agents_before, tender_records)
        az_after = next(a for a in agents_after if "astrazeneca" in a.normalized_name)

        assert az_after.tender_count >= 1
        assert az_after.tender_total_mn_sar >= 3.0
        # share 재정규화 후에도 AZ 가 Novartis 보다 높아야 함
        novartis = next(a for a in agents_after if "novartis" in a.normalized_name)
        assert az_after.market_share_est > novartis.market_share_est

    def test_no_tender_match_no_change(self):
        """관련 없는 supplier 는 영향 없음."""
        matches = [{"trade_name": "A", "agent_or_supplier": "AgentA", "price_sar": 100}]
        agents = extract_competitor_agents(matches)
        before = agents[0].tender_count

        tender_records = [
            TenderRecord(supplier_name="TotallyDifferentCompany",
                         value_sar=1_000_000, date="2025-01-01"),
        ]
        enrich_with_tender_data(agents, tender_records)
        assert agents[0].tender_count == before


# ─── build_competitor_map (end-to-end) ────────────────────────────

class TestBuildCompetitorMap:
    def test_end_to_end(self):
        matches = [
            {"trade_name": "Forxiga", "agent_or_supplier": "AstraZeneca KSA",
             "price_sar": 120, "regulatory_id": "R1"},
            {"trade_name": "Xigduo", "agent_or_supplier": "AstraZeneca KSA",
             "price_sar": 150},
            {"trade_name": "Jardiance", "agent_or_supplier": "Boehringer Ingelheim",
             "price_sar": 130},
        ]
        tenders = [
            TenderRecord(supplier_name="AstraZeneca KSA", value_sar=10_000_000,
                         date="2025-01-01", source="etimad"),
        ]
        result = build_competitor_map(
            matches,
            target_brand="OurOwnBrand",
            tender_records=tenders,
            top_n=5,
        )
        assert result["total_agents"] == 2
        assert result["total_brands"] == 3
        assert result["target_brand_excluded"] is True
        assert len(result["agents"]) == 2
        # top agent 는 tender 있는 AstraZeneca
        top = result["agents"][0]
        assert "astrazeneca" in top["normalized_name"]
        assert top["tender_count"] >= 1

    def test_empty_input(self):
        result = build_competitor_map([])
        assert result["total_agents"] == 0
        assert result["total_brands"] == 0
        assert result["agents"] == []

    def test_top_n_slice(self):
        matches = [
            {"trade_name": f"Brand{i}", "agent_or_supplier": f"Agent{i}",
             "price_sar": 100 + i}
            for i in range(10)
        ]
        result = build_competitor_map(matches, top_n=3)
        assert result["total_agents"] == 10
        assert len(result["agents"]) == 3
