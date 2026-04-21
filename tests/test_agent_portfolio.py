"""Phase 3-1 단위 테스트: 에이전트 포트폴리오 + 빈틈 분석."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics.agent_portfolio import (
    _normalize_agent_name,
    _atc_level3,
    build_agent_portfolio,
    find_white_space,
    resolve_atc_from_inn,
    analyze_white_space_for_inn,
)


# ─── 유틸 ────────────────────────────────────────────────────────

class TestNormalizeAgentName:
    def test_strips_suffixes(self):
        assert _normalize_agent_name("Tabuk Pharmaceuticals Co., Ltd.") == "tabuk pharmaceuticals"
        assert _normalize_agent_name("SPIMACO  ADDWAEIH ").lower() == "spimaco addwaeih"

    def test_empty(self):
        assert _normalize_agent_name("") == ""
        assert _normalize_agent_name(None) == ""

    def test_case_insensitive(self):
        assert _normalize_agent_name("tabuk pharma") == _normalize_agent_name("TABUK PHARMA")


class TestATCLevel3:
    def test_takes_first_four_chars(self):
        assert _atc_level3("A10BK01") == "A10B"
        assert _atc_level3("c09aa05") == "C09A"

    def test_short_atc(self):
        """4글자 미만이면 None 반환 (level3 가 되지 못함)."""
        assert _atc_level3("A10") is None

    def test_none(self):
        assert _atc_level3(None) is None
        assert _atc_level3("") is None


# ─── 포트폴리오 빌드 (fuzzy merge) ───────────────────────────────

class TestBuildAgentPortfolio:
    def test_fuzzy_merges_tabuk_variants(self):
        """Tabuk Pharmaceuticals Co. + Tabuk Pharma Ltd. → single bucket."""
        products = [
            {"trade_name": "Dapexa", "inn_name": "dapagliflozin",
             "agent_or_supplier": "Tabuk Pharmaceuticals Co., Ltd.", "atc_code": "A10BK01"},
            {"trade_name": "Dapexa 10", "inn_name": "dapagliflozin",
             "agent_or_supplier": "Tabuk Pharmaceuticals Co., Ltd.", "atc_code": "A10BK01"},
            {"trade_name": "Glucotab", "inn_name": "metformin",
             "agent_or_supplier": "Tabuk Pharma Ltd.", "atc_code": "A10BA02"},
            {"trade_name": "Forxa", "inn_name": "dapagliflozin",
             "agent_or_supplier": "AstraZeneca KSA", "atc_code": "A10BK01"},
        ]
        portfolio, unmatched = build_agent_portfolio(products)

        tabuk_keys = [k for k in portfolio if "tabuk" in k]
        assert len(tabuk_keys) == 1, f"Expected single Tabuk bucket, got {tabuk_keys}"
        tabuk = portfolio[tabuk_keys[0]]
        assert tabuk.total_products == 3  # 2 Dapexa + 1 Glucotab

    def test_atc_aggregation(self):
        products = [
            {"trade_name": "A", "inn_name": "x", "agent_or_supplier": "Agent1", "atc_code": "A10BK01"},
            {"trade_name": "B", "inn_name": "y", "agent_or_supplier": "Agent1", "atc_code": "A10BH05"},
            {"trade_name": "C", "inn_name": "z", "agent_or_supplier": "Agent1", "atc_code": "C09AA05"},
        ]
        portfolio, _ = build_agent_portfolio(products)
        agent = portfolio["agent1"]
        assert agent.atc_level3_counts["A10B"] == 2
        assert agent.atc_level3_counts["C09A"] == 1

    def test_empty_products(self):
        portfolio, unmatched = build_agent_portfolio([])
        assert portfolio == {}
        assert unmatched == []


# ─── INN → ATC 해결 ──────────────────────────────────────────────

class TestResolveATCFromINN:
    def test_modern_sglt2(self):
        atc = resolve_atc_from_inn("dapagliflozin")
        assert atc is not None
        assert atc.startswith("A10")

    def test_glp1_agonist(self):
        atc = resolve_atc_from_inn("semaglutide")
        assert atc is not None
        assert atc.startswith("A10")

    def test_unknown_returns_none_or_empty(self):
        atc = resolve_atc_from_inn("xyznonexistent123")
        assert atc is None or atc == ""


# ─── 빈틈 분석 ────────────────────────────────────────────────────

class TestFindWhiteSpace:
    def test_detects_missing_ingredient(self):
        """Tabuk 는 A10B 에 2개 있으나 dapagliflozin 은 없음 → 빈틈."""
        products = [
            # Tabuk: A10B 에 metformin + glimepiride (dapagliflozin 없음)
            {"trade_name": "Glucotab", "inn_name": "metformin",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BA02"},
            {"trade_name": "Amaryl-T", "inn_name": "glimepiride",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BB12"},
            {"trade_name": "Januvia-T", "inn_name": "sitagliptin",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BH01"},
            # AstraZeneca: dapagliflozin 있음 (경쟁사)
            {"trade_name": "Forxiga", "inn_name": "dapagliflozin",
             "agent_or_supplier": "AstraZeneca KSA", "atc_code": "A10BK01"},
        ]
        portfolio, _ = build_agent_portfolio(products)
        candidates = find_white_space(portfolio, "A10B", "dapagliflozin", min_atc_products=2)

        assert len(candidates) >= 1
        tabuk_cand = next((c for c in candidates if "tabuk" in c.agent_name.lower()), None)
        assert tabuk_cand is not None
        assert tabuk_cand.missing_ingredient is True
        assert tabuk_cand.product_count_in_atc >= 2
        assert "dapagliflozin" in tabuk_cand.sales_pitch.lower()

    def test_filters_below_min_products(self):
        products = [
            {"trade_name": "X", "inn_name": "y", "agent_or_supplier": "SmallCorp", "atc_code": "A10BA02"},
        ]
        portfolio, _ = build_agent_portfolio(products)
        candidates = find_white_space(portfolio, "A10B", "dapagliflozin", min_atc_products=3)
        assert len(candidates) == 0


class TestAnalyzeWhiteSpaceForINN:
    def test_end_to_end_dapagliflozin(self):
        products = [
            {"trade_name": "Glucotab", "inn_name": "metformin",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BA02"},
            {"trade_name": "Amaryl-T", "inn_name": "glimepiride",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BB12"},
            {"trade_name": "Januvia-T", "inn_name": "sitagliptin",
             "agent_or_supplier": "Tabuk Pharma", "atc_code": "A10BH01"},
            {"trade_name": "Forxiga", "inn_name": "dapagliflozin",
             "agent_or_supplier": "AstraZeneca KSA", "atc_code": "A10BK01"},
        ]
        result = analyze_white_space_for_inn(products, "dapagliflozin", min_atc_products=2)

        assert result["target_inn"] == "dapagliflozin"
        # ATC 가 A10B (대체 매칭 허용)
        assert result["target_atc_level3"] and result["target_atc_level3"].startswith("A10")
        assert result["total_agents"] >= 2
        assert len(result["candidates"]) >= 1

    def test_atc_fallback_when_inn_unknown(self):
        """INN 을 해결 못해도 target_atc_level3 가 주어지면 동작."""
        products = [
            {"trade_name": "A", "inn_name": "x", "agent_or_supplier": "Agent1", "atc_code": "A10BA02"},
            {"trade_name": "B", "inn_name": "y", "agent_or_supplier": "Agent1", "atc_code": "A10BB12"},
        ]
        result = analyze_white_space_for_inn(
            products, "totallyunknownxyz",
            target_atc_level3="A10B", min_atc_products=1,
        )
        assert result["target_atc_level3"] == "A10B"
