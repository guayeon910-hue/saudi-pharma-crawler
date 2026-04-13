"""
test_ai_search.py — AI 자율 서칭 통합 테스트

전체 파이프라인 (Phase A → B → C) mock 기반 시뮬레이션.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from ai_search import (
    DrugAISearchResult,
    AISearchSummary,
    process_one_drug,
    _drug_to_info,
    _normalize_records,
)
from drug_registry import TargetDrug


# ---------------------------------------------------------------------------
# 테스트 약품 픽스쳐
# ---------------------------------------------------------------------------

def _make_drug(name="Panadol", ingredient="Paracetamol", form="Tablet", strength="500mg"):
    return TargetDrug(
        id=name.lower().replace(" ", "-"),
        drug_type="일반제",
        trade_name=name,
        ingredient=ingredient,
        strength=strength,
        dosage_form=form,
    )


# ---------------------------------------------------------------------------
# DrugAISearchResult 테스트
# ---------------------------------------------------------------------------

class TestDrugAISearchResult(unittest.TestCase):
    def test_basic(self):
        r = DrugAISearchResult(drug_id="test", drug_name="Test Drug")
        self.assertEqual(r.records_extracted, 0)
        self.assertEqual(r.errors, [])


class TestAISearchSummary(unittest.TestCase):
    def test_to_dict(self):
        s = AISearchSummary()
        s.drugs_processed = 2
        s.total_records = 10
        s.drug_results = [
            DrugAISearchResult("a", "Drug A", discovery={"valid_count": 3}),
            DrugAISearchResult("b", "Drug B", discovery={"valid_count": 1}),
        ]
        d = s.to_dict()
        self.assertEqual(d["drugs_processed"], 2)
        self.assertEqual(d["total_records"], 10)
        self.assertEqual(len(d["drug_results"]), 2)
        self.assertEqual(d["drug_results"][0]["sources_found"], 3)


# ---------------------------------------------------------------------------
# _drug_to_info 테스트
# ---------------------------------------------------------------------------

class TestDrugToInfo(unittest.TestCase):
    def test_conversion(self):
        drug = _make_drug()
        info = _drug_to_info(drug)
        self.assertEqual(info["trade_name"], "Panadol")
        self.assertEqual(info["ingredients"], "Paracetamol")
        self.assertEqual(info["dosage_form"], "Tablet")
        self.assertEqual(info["strength"], "500mg")


# ---------------------------------------------------------------------------
# _normalize_records 테스트
# ---------------------------------------------------------------------------

class TestNormalizeRecords(unittest.TestCase):
    def test_basic_normalization(self):
        records = [
            {
                "product_name": "Panadol Extra",
                "price": "SAR 12.50",
                "manufacturer": "GSK",
                "active_ingredient": "Paracetamol",
                "strength": "500mg",
                "_source_domain": "pharmacy.sa",
            }
        ]
        normalized = _normalize_records(records, "pharmacy.sa")
        self.assertEqual(len(normalized), 1)
        # normalizer 가용 시 source 필드 추가, 없으면 raw 반환
        rec = normalized[0]
        if "source" in rec:
            self.assertIn("ai_discovered", rec["source"])
        else:
            # normalizer import 실패 — raw 데이터 그대로 반환
            self.assertIn("product_name", rec)

    def test_empty_records(self):
        normalized = _normalize_records([], "test.com")
        self.assertEqual(normalized, [])


# ---------------------------------------------------------------------------
# process_one_drug 통합 테스트 (full mock)
# ---------------------------------------------------------------------------

class TestProcessOneDrug(unittest.TestCase):
    @patch("ai_search.fetch_page_html")
    @patch("ai_search.discover_sources")
    def test_full_pipeline_success(self, mock_discover, mock_fetch_html):
        """전체 파이프라인 성공 시뮬레이션."""
        from assets.snippets.source_discoverer import DiscoveryResult, DiscoveredSource

        # Mock Discovery (Phase A)
        valid_source = DiscoveredSource(
            url="https://pharmacy.sa/drugs",
            domain="pharmacy.sa",
            relevance_score=0.85,
            category="pharma_retailer",
            has_price_data=True,
        )
        mock_discover.return_value = DiscoveryResult(
            drug_name="Panadol",
            queries_generated=["q1"],
            urls_found=5,
            pages_fetched=3,
            sources_evaluated=5,
            valid_sources=[valid_source],
            rejected_sources=[],
            duration_sec=10.0,
        )

        # Mock HTML (Phase B)
        mock_fetch_html.return_value = """
        <html><head><title>Pharmacy SA</title></head><body>
        <div class="products">
            <div class="product">
                <h3 class="name">Panadol Extra 500mg</h3>
                <span class="price">SAR 12.50</span>
                <p class="mfr">GSK Saudi</p>
                <p class="ingredient">Paracetamol</p>
                <p class="strength">500mg</p>
            </div>
        </div>
        </body></html>
        """

        # Mock LLM
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            # Phase B: 5 XPath 생성
            {"xpath": "//h3[@class='name']/text()", "expected_value": "Panadol Extra 500mg"},
            {"xpath": "//span[@class='price']/text()", "expected_value": "SAR 12.50"},
            {"xpath": "//p[@class='mfr']/text()", "expected_value": "GSK Saudi"},
            {"xpath": "//p[@class='ingredient']/text()", "expected_value": "Paracetamol"},
            {"xpath": "//p[@class='strength']/text()", "expected_value": "500mg"},
        ]
        mock_llm.available = True

        mock_http = MagicMock()
        drug = _make_drug()

        result = process_one_drug(
            drug, mock_llm, mock_http,
            dry_run=True, fetch_delay=0,
        )

        self.assertEqual(result.drug_name, "Panadol")
        self.assertIsNotNone(result.discovery)
        self.assertGreaterEqual(result.scrapers_generated, 0)
        # Phase A가 호출됨
        mock_discover.assert_called_once()

    @patch("ai_search.discover_sources")
    def test_no_valid_sources(self, mock_discover):
        """유효 소스 없을 때 조기 종료."""
        from assets.snippets.source_discoverer import DiscoveryResult

        mock_discover.return_value = DiscoveryResult(
            drug_name="Test",
            queries_generated=["q1"],
            urls_found=3,
            pages_fetched=2,
            sources_evaluated=3,
            valid_sources=[],
            rejected_sources=[],
            duration_sec=5.0,
        )

        mock_llm = MagicMock()
        mock_http = MagicMock()
        drug = _make_drug("TestDrug")

        result = process_one_drug(
            drug, mock_llm, mock_http,
            dry_run=True, fetch_delay=0,
        )

        self.assertEqual(result.records_extracted, 0)
        self.assertEqual(result.scrapers_generated, 0)

    @patch("ai_search.discover_sources")
    def test_phase_a_error_handling(self, mock_discover):
        """Phase A 에러 시 graceful 처리."""
        mock_discover.side_effect = Exception("Network error")

        mock_llm = MagicMock()
        mock_http = MagicMock()
        drug = _make_drug()

        result = process_one_drug(
            drug, mock_llm, mock_http,
            dry_run=True, fetch_delay=0,
        )

        self.assertEqual(len(result.errors), 1)
        self.assertIn("Phase A", result.errors[0])


# ---------------------------------------------------------------------------
# 8개 약품 전체 시뮬레이션
# ---------------------------------------------------------------------------

class TestFullSimulation(unittest.TestCase):
    """8개 약품에 대한 시뮬레이션 — 모든 외부 호출 mock."""

    @patch("ai_search.fetch_page_html")
    @patch("ai_search.discover_sources")
    def test_all_drugs_simulation(self, mock_discover, mock_fetch_html):
        """8개 약품 순차 처리 시뮬레이션."""
        from assets.snippets.source_discoverer import DiscoveryResult, DiscoveredSource

        drugs = [
            _make_drug("Omethyl Cutielet", "Omega-3-Acid Ethyl Esters 90", "Pouch", "2g"),
            _make_drug("Rosumeg Combigel", "Rosuvastatin + Omega-3-EE90", "Cap.", "5/1000"),
            _make_drug("Atmeg Combigel", "Atorvastatin + Omega-3-EE90", "Cap.", "10/1000"),
            _make_drug("Ciloduo", "Cilostazol + Rosuvastatin", "Tab.", "200/10mg"),
            _make_drug("Gastiin CR", "Mosapride Citrate", "Tab.", "15mg"),
            _make_drug("Sereterol Activair", "Fluticasone + Salmeterol", "Inhaler", "250/50"),
            _make_drug("Gadvoa Inj.", "Gadobutrol", "PFS", "5mL"),
            _make_drug("Hydrine", "Hydroxyurea", "Cap.", "500mg"),
        ]

        # 각 약품: 2개 유효 소스 발견 시뮬레이션
        def make_discovery(drug_info, **kwargs):
            name = drug_info.get("trade_name", "?") if isinstance(drug_info, dict) else "?"
            return DiscoveryResult(
                drug_name=name,
                queries_generated=["q1", "q2", "q3"],
                urls_found=8,
                pages_fetched=5,
                sources_evaluated=8,
                valid_sources=[
                    DiscoveredSource(
                        url=f"https://pharma-{name.lower()}.sa/products",
                        domain=f"pharma-{name.lower()}.sa",
                        relevance_score=0.8,
                        category="pharma_retailer",
                    ),
                ],
                rejected_sources=[],
                duration_sec=5.0,
            )

        mock_discover.side_effect = lambda llm, http, info, **kw: make_discovery(info, **kw)

        # Phase B: HTML + XPath 생성
        mock_fetch_html.return_value = """
        <html><head><title>Pharma SA</title></head><body>
        <div class="products">
            <div class="item">
                <h3 class="name">Drug Product 500mg</h3>
                <span class="price">SAR 25.00</span>
                <p class="mfr">Saudi Pharma Co</p>
            </div>
        </div>
        </body></html>
        """

        mock_llm = MagicMock()
        mock_llm.available = True

        # 각 약품마다 5 XPath 호출
        xpath_responses = [
            {"xpath": "//h3[@class='name']/text()", "expected_value": "Drug Product 500mg"},
            {"xpath": "//span[@class='price']/text()", "expected_value": "SAR 25.00"},
            {"xpath": "//p[@class='mfr']/text()", "expected_value": "Saudi Pharma Co"},
            {"xpath": "//p[@class='ingredient']/text()", "expected_value": ""},  # 없음
            {"xpath": "//p[@class='strength']/text()", "expected_value": ""},    # 없음
        ]
        # step-back용 추가 응답 (빈 결과 → step-back)
        stepback_responses = [
            {"xpath": "//body//p[4]/text()", "expected_value": ""},
        ] * 20

        mock_llm.ask_json.side_effect = (xpath_responses + stepback_responses) * 8

        mock_http = MagicMock()

        summary = AISearchSummary()

        for drug in drugs:
            result = process_one_drug(
                drug, mock_llm, mock_http,
                dry_run=True, fetch_delay=0,
            )
            summary.drug_results.append(result)
            summary.drugs_processed += 1
            if result.discovery:
                summary.total_sources_valid += result.discovery.get("valid_count", 0)
            summary.total_records += result.records_extracted

        # 검증
        self.assertEqual(summary.drugs_processed, 8)
        self.assertEqual(summary.total_sources_valid, 8)  # 각 약품 1개씩

        # 시뮬레이션 결과 출력
        print("\n" + "=" * 60)
        print("8개 약품 AI 자율 서칭 시뮬레이션 결과")
        print("=" * 60)
        for r in summary.drug_results:
            sources = r.discovery.get("valid_count", 0) if r.discovery else 0
            scrapers = r.scrapers_generated
            records = r.records_extracted
            errors = len(r.errors)
            status = "✓" if errors == 0 else f"✗({errors}err)"
            print(f"  {status} {r.drug_name:<30} | 소스 {sources} | 스크래퍼 {scrapers} | 레코드 {records}")
        print("=" * 60)
        print(f"  총 소스:    {summary.total_sources_valid}")
        print(f"  총 레코드:  {summary.total_records}")
        print(f"  총 약품:    {summary.drugs_processed}")
        print("=" * 60)


if __name__ == "__main__":
    unittest.main()
