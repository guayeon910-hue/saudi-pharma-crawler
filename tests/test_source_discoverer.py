"""
test_source_discoverer.py — source_discoverer.py 단위 테스트

API/네트워크 호출 없이 mock 기반 검증.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from assets.snippets.source_discoverer import (
    DiscoveredSource,
    DiscoveryResult,
    EXCLUDED_DOMAINS,
    generate_search_queries,
    _fallback_queries,
    _parse_ddg_results,
    evaluate_source,
    discover_sources,
    discover_sources_perplexity,
)


# ---------------------------------------------------------------------------
# DiscoveredSource 테스트
# ---------------------------------------------------------------------------

class TestDiscoveredSource(unittest.TestCase):
    def test_valid_source(self):
        s = DiscoveredSource(url="https://example.com", domain="example.com",
                             relevance_score=0.8, category="pharma_retailer")
        self.assertTrue(s.is_valid)

    def test_low_score_invalid(self):
        s = DiscoveredSource(url="https://example.com", domain="example.com",
                             relevance_score=0.3, category="other")
        self.assertFalse(s.is_valid)

    def test_rejected_invalid(self):
        s = DiscoveredSource(url="https://example.com", domain="example.com",
                             relevance_score=0.9, rejection_reason="Manual rejection")
        self.assertFalse(s.is_valid)

    def test_threshold_boundary(self):
        s1 = DiscoveredSource(url="a", domain="a", relevance_score=0.6)
        s2 = DiscoveredSource(url="b", domain="b", relevance_score=0.59)
        self.assertTrue(s1.is_valid)
        self.assertFalse(s2.is_valid)


# ---------------------------------------------------------------------------
# 검색 쿼리 생성 테스트
# ---------------------------------------------------------------------------

class TestSearchQueryGeneration(unittest.TestCase):
    def test_llm_query_generation(self):
        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = [
            "Paracetamol pharmacy Saudi Arabia",
            "باراسيتامول صيدلية السعودية",
            "Paracetamol 500mg price KSA",
            "Saudi drug price comparison Paracetamol",
            "GCC pharmaceutical database Paracetamol",
        ]

        drug_info = {
            "trade_name": "Panadol Extra",
            "ingredients": "Paracetamol + Caffeine",
            "dosage_form": "Tablet",
            "strength": "500mg/65mg",
        }

        queries = generate_search_queries(mock_llm, drug_info)
        self.assertEqual(len(queries), 5)
        self.assertIn("Paracetamol pharmacy Saudi Arabia", queries)

    def test_llm_failure_fallback(self):
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = Exception("API error")

        drug_info = {
            "trade_name": "Rosumeg",
            "ingredients": "Rosuvastatin + Omega-3",
            "dosage_form": "Capsule",
            "strength": "5/1000mg",
        }

        queries = generate_search_queries(mock_llm, drug_info)
        self.assertEqual(len(queries), 5)
        # fallback 쿼리에 Saudi 포함 확인
        saudi_count = sum(1 for q in queries if "Saudi" in q or "KSA" in q or "السعودية" in q)
        self.assertGreaterEqual(saudi_count, 3)

    def test_fallback_queries(self):
        drug_info = {
            "trade_name": "Hydrine",
            "ingredients": "Hydroxyurea",
            "dosage_form": "Capsule",
            "strength": "500mg",
        }
        queries = _fallback_queries(drug_info)
        self.assertEqual(len(queries), 5)
        self.assertTrue(any("Hydrine" in q for q in queries))
        self.assertTrue(any("Hydroxyurea" in q for q in queries))


# ---------------------------------------------------------------------------
# DuckDuckGo 결과 파싱 테스트
# ---------------------------------------------------------------------------

class TestDDGParsing(unittest.TestCase):
    def test_parse_results(self):
        # DuckDuckGo HTML 결과 시뮬레이션
        html = """
        <html><body>
        <div class="result">
            <a class="result__a" href="https://www.example-pharmacy.sa/drugs">
                Saudi Pharmacy - Drug List
            </a>
            <a class="result__snippet">Online pharmacy serving Saudi Arabia with drug prices</a>
        </div>
        <div class="result">
            <a class="result__a" href="https://pharma-ksa.com/products">
                KSA Pharma Products
            </a>
            <a class="result__snippet">Pharmaceutical products available in KSA</a>
        </div>
        <div class="result">
            <a class="result__a" href="https://www.nahdi.sa/drugs">
                Nahdi Pharmacy
            </a>
            <a class="result__snippet">Nahdi online</a>
        </div>
        </body></html>
        """
        results = _parse_ddg_results(html, max_results=10)

        # Nahdi는 EXCLUDED_DOMAINS에 있으므로 제외
        domains = [r["domain"] for r in results]
        self.assertNotIn("www.nahdi.sa", domains)

        # 유효 결과만 남음
        for r in results:
            self.assertIn("url", r)
            self.assertIn("title", r)
            self.assertIn("domain", r)

    def test_excluded_domains(self):
        # 고정 10개 소스 도메인이 제외 목록에 있는지 확인
        self.assertIn("sfda.gov.sa", EXCLUDED_DOMAINS)
        self.assertIn("www.nahdi.sa", EXCLUDED_DOMAINS)
        self.assertIn("www.aldawaa.com", EXCLUDED_DOMAINS)
        self.assertIn("noon.com", EXCLUDED_DOMAINS)
        # 일반 사이트
        self.assertIn("google.com", EXCLUDED_DOMAINS)
        self.assertIn("wikipedia.org", EXCLUDED_DOMAINS)


# ---------------------------------------------------------------------------
# LLM 사이트 평가 테스트
# ---------------------------------------------------------------------------

class TestEvaluateSource(unittest.TestCase):
    """캐시 I/O를 항상 패치하여 실제 파일 시스템과 격리."""

    @patch("assets.snippets.source_discoverer._save_domain_cache")
    @patch("assets.snippets.source_discoverer._load_domain_cache", return_value={})
    def test_high_relevance(self, _mock_load, _mock_save):
        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = {
            "relevance_score": 0.85,
            "category": "pharma_retailer",
            "has_price_data": True,
            "has_product_listing": True,
            "language": "en",
            "reason": "Saudi online pharmacy with drug prices",
        }

        source = evaluate_source(
            mock_llm,
            url="https://pharmacy.sa/drugs",
            domain="pharmacy.sa",
            title="Saudi Pharmacy",
            snippet="[PARAGRAPH] Paracetamol 500mg Tablet SAR 12.50 [/PARAGRAPH]",
        )

        self.assertTrue(source.is_valid)
        self.assertEqual(source.relevance_score, 0.85)
        self.assertEqual(source.category, "pharma_retailer")
        self.assertTrue(source.has_price_data)

    @patch("assets.snippets.source_discoverer._save_domain_cache")
    @patch("assets.snippets.source_discoverer._load_domain_cache", return_value={})
    def test_low_relevance(self, _mock_load, _mock_save):
        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = {
            "relevance_score": 0.2,
            "category": "news",
            "has_price_data": False,
            "has_product_listing": False,
            "language": "en",
            "reason": "General news article about healthcare",
        }

        source = evaluate_source(
            mock_llm,
            url="https://news.com/health",
            domain="news.com",
            title="Health News",
            snippet="[PARAGRAPH] Healthcare in Saudi Arabia [/PARAGRAPH]",
        )

        self.assertFalse(source.is_valid)
        self.assertIn("Low relevance", source.rejection_reason)

    @patch("assets.snippets.source_discoverer._save_domain_cache")
    @patch("assets.snippets.source_discoverer._load_domain_cache", return_value={})
    def test_llm_error_handling(self, _mock_load, _mock_save):
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = Exception("API timeout")

        source = evaluate_source(
            mock_llm,
            url="https://example.com",
            domain="example.com",
            title="Test",
            snippet="test",
        )

        self.assertFalse(source.is_valid)
        self.assertIn("error", source.rejection_reason.lower())


# ---------------------------------------------------------------------------
# 전체 파이프라인 테스트 (full mock)
# ---------------------------------------------------------------------------

class TestDiscoverSources(unittest.TestCase):
    @patch("assets.snippets.source_discoverer._save_domain_cache")
    @patch("assets.snippets.source_discoverer._load_domain_cache", return_value={})
    @patch("assets.snippets.source_discoverer.fetch_search_results")
    @patch("assets.snippets.source_discoverer.fetch_page_html")
    @patch("assets.snippets.source_discoverer.time")
    def test_full_pipeline(self, mock_time, mock_fetch_html, mock_fetch_search,
                           _mock_load, _mock_save):
        # time.time() 호출 순서: start, pharma.sa 캐시 저장 ts, news.com 캐시 저장 ts, end
        mock_time.time.side_effect = [0.0, 1.0, 2.0, 10.0]
        mock_time.sleep = MagicMock()

        # Mock LLM
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            # 1st call: 쿼리 생성
            ["query 1 Saudi", "query 2 KSA"],
            # 2nd call: 사이트 1 평가 (유효)
            {"relevance_score": 0.85, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True,
             "language": "en", "reason": "Saudi pharma site"},
            # 3rd call: 사이트 2 평가 (거부)
            {"relevance_score": 0.3, "category": "news",
             "has_price_data": False, "has_product_listing": False,
             "language": "en", "reason": "News site"},
        ]

        # Mock HTTP
        mock_http = MagicMock()

        # Mock 검색 결과
        mock_fetch_search.return_value = [
            {"url": "https://pharma.sa/drugs", "title": "SA Pharma", "snippet": "drugs", "domain": "pharma.sa"},
            {"url": "https://news.com/health", "title": "Health News", "snippet": "news", "domain": "news.com"},
        ]

        # Mock HTML 가져오기
        mock_fetch_html.return_value = "<html><body><p>Paracetamol 500mg SAR 12.50 pharmaceutical product</p></body></html>"

        drug_info = {
            "trade_name": "Panadol",
            "ingredients": "Paracetamol",
            "dosage_form": "Tablet",
            "strength": "500mg",
        }

        result = discover_sources(
            mock_llm, mock_http, drug_info,
            max_queries=2, max_urls_per_query=5, fetch_delay=0,
        )

        self.assertIsInstance(result, DiscoveryResult)
        self.assertEqual(result.drug_name, "Panadol")
        self.assertEqual(len(result.valid_sources), 1)
        self.assertEqual(len(result.rejected_sources), 1)
        self.assertEqual(result.valid_sources[0].domain, "pharma.sa")
        self.assertEqual(result.rejected_sources[0].domain, "news.com")

    def test_discovery_result_to_dict(self):
        result = DiscoveryResult(
            drug_name="Test",
            queries_generated=["q1", "q2"],
            urls_found=5,
            pages_fetched=3,
            sources_evaluated=3,
            valid_sources=[
                DiscoveredSource(url="https://a.com", domain="a.com",
                                 relevance_score=0.9, category="pharma_retailer",
                                 has_price_data=True),
            ],
            rejected_sources=[
                DiscoveredSource(url="https://b.com", domain="b.com",
                                 relevance_score=0.2, rejection_reason="Low"),
            ],
            duration_sec=5.5,
        )

        d = result.to_dict()
        self.assertEqual(d["drug_name"], "Test")
        self.assertEqual(d["valid_count"], 1)
        self.assertEqual(d["rejected_count"], 1)
        self.assertEqual(d["valid_sources"][0]["score"], 0.9)
        self.assertTrue(d["valid_sources"][0]["has_price"])


# ---------------------------------------------------------------------------
# Perplexity 통합 테스트
# ---------------------------------------------------------------------------

class TestDiscoverSourcesPerplexity(unittest.TestCase):
    """Perplexity 경로 + DuckDuckGo fallback 테스트."""

    def _make_pplx_client(self, sources: list[dict], available: bool = True):
        """Mock PerplexityClient 생성."""
        mock = MagicMock()
        mock.available = available
        mock.search_pharma_sources.return_value = sources
        return mock

    def test_perplexity_primary_path(self):
        """Perplexity가 결과를 반환하면 DDG를 호출하지 않음."""
        pplx = self._make_pplx_client([
            {"url": "https://pharma-ksa.com/drugs", "domain": "pharma-ksa.com",
             "title": "KSA Pharma", "description": "Saudi pharmacy",
             "relevance_score": 0.85, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True, "language": "en"},
            {"url": "https://drugstore.sa/products", "domain": "drugstore.sa",
             "title": "Drug Store SA", "description": "Online drug store",
             "relevance_score": 0.75, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True, "language": "ar"},
        ])

        mock_llm = MagicMock()
        mock_http = MagicMock()

        drug_info = {"trade_name": "Panadol", "ingredients": "Paracetamol",
                     "dosage_form": "Tablet", "strength": "500mg"}

        result = discover_sources(
            mock_llm, mock_http, drug_info,
            pplx_client=pplx,
            max_queries=5, fetch_delay=0,
        )

        # Perplexity 경로 사용
        pplx.search_pharma_sources.assert_called_once()
        self.assertEqual(len(result.valid_sources), 2)
        self.assertEqual(result.queries_generated, ["perplexity:Panadol"])
        self.assertEqual(result.valid_sources[0].search_query, "perplexity_sonar")

        # DDG 호출 안 됨 (mock_llm.ask_json 미호출)
        mock_llm.ask_json.assert_not_called()

    def test_perplexity_filters_low_relevance(self):
        """Perplexity 결과 중 relevance < 0.6은 rejected로 분류."""
        pplx = self._make_pplx_client([
            {"url": "https://good.sa/drugs", "domain": "good.sa",
             "title": "Good", "description": "Good pharmacy",
             "relevance_score": 0.80, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True, "language": "en"},
            {"url": "https://bad.com/news", "domain": "bad.com",
             "title": "Bad", "description": "News site",
             "relevance_score": 0.30, "category": "news",
             "has_price_data": False, "has_product_listing": False, "language": "en"},
        ])

        result = discover_sources_perplexity(pplx, {
            "trade_name": "TestDrug", "ingredients": "TestIng",
            "dosage_form": "Tab", "strength": "10mg",
        })

        self.assertEqual(len(result.valid_sources), 1)
        self.assertEqual(len(result.rejected_sources), 1)
        self.assertEqual(result.valid_sources[0].domain, "good.sa")
        self.assertIn("Low relevance", result.rejected_sources[0].rejection_reason)

    @patch("assets.snippets.source_discoverer.fetch_search_results")
    @patch("assets.snippets.source_discoverer.fetch_page_html")
    @patch("assets.snippets.source_discoverer.time")
    def test_perplexity_no_results_fallback(self, mock_time, mock_fetch_html, mock_fetch_search):
        """Perplexity가 유효 결과 0건이면 DuckDuckGo로 fallback."""
        mock_time.time.return_value = 0.0
        mock_time.sleep = MagicMock()

        # Perplexity: 빈 결과
        pplx = self._make_pplx_client([])

        # DuckDuckGo fallback 준비
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            ["fallback query Saudi"],  # 쿼리 생성
            {"relevance_score": 0.80, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True,
             "language": "en", "reason": "Fallback source"},
        ]
        mock_http = MagicMock()
        mock_fetch_search.return_value = [
            {"url": "https://fallback.sa/drugs", "title": "Fallback",
             "snippet": "drugs", "domain": "fallback.sa"},
        ]
        mock_fetch_html.return_value = "<html><body>drugs</body></html>"

        drug_info = {"trade_name": "TestDrug", "ingredients": "TestIng",
                     "dosage_form": "Tab", "strength": "10mg"}

        result = discover_sources(
            mock_llm, mock_http, drug_info,
            pplx_client=pplx,
            max_queries=1, fetch_delay=0,
        )

        # DDG fallback 실행됨
        pplx.search_pharma_sources.assert_called_once()
        mock_llm.ask_json.assert_called()  # DDG 경로에서 LLM 호출
        self.assertEqual(len(result.valid_sources), 1)
        self.assertEqual(result.valid_sources[0].domain, "fallback.sa")

    @patch("assets.snippets.source_discoverer.fetch_search_results")
    @patch("assets.snippets.source_discoverer.fetch_page_html")
    @patch("assets.snippets.source_discoverer.time")
    def test_perplexity_exception_fallback(self, mock_time, mock_fetch_html, mock_fetch_search):
        """Perplexity 에러 시 DuckDuckGo fallback."""
        mock_time.time.return_value = 0.0
        mock_time.sleep = MagicMock()

        # Perplexity: 에러
        pplx = MagicMock()
        pplx.available = True
        pplx.search_pharma_sources.side_effect = Exception("Perplexity API error")

        # DuckDuckGo fallback
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            ["error fallback query Saudi"],
            {"relevance_score": 0.70, "category": "distributor",
             "has_price_data": False, "has_product_listing": True,
             "language": "en", "reason": "Distributor site"},
        ]
        mock_http = MagicMock()
        mock_fetch_search.return_value = [
            {"url": "https://dist.sa/catalog", "title": "Dist",
             "snippet": "pharma", "domain": "dist.sa"},
        ]
        mock_fetch_html.return_value = "<html><body>pharma</body></html>"

        drug_info = {"trade_name": "ErrorDrug", "ingredients": "ErrorIng",
                     "dosage_form": "Cap", "strength": "100mg"}

        result = discover_sources(
            mock_llm, mock_http, drug_info,
            pplx_client=pplx,
            max_queries=1, fetch_delay=0,
        )

        # DDG fallback 실행
        self.assertEqual(len(result.valid_sources), 1)
        self.assertEqual(result.valid_sources[0].domain, "dist.sa")

    @patch("assets.snippets.source_discoverer._save_domain_cache")
    @patch("assets.snippets.source_discoverer._load_domain_cache", return_value={})
    @patch("assets.snippets.source_discoverer.fetch_search_results")
    @patch("assets.snippets.source_discoverer.fetch_page_html")
    @patch("assets.snippets.source_discoverer.time")
    def test_no_pplx_client_uses_ddg(self, mock_time, mock_fetch_html, mock_fetch_search,
                                     _mock_load, _mock_save):
        """pplx_client=None이면 기존 DDG 동작 그대로."""
        # time.time() 호출 순서: start, ddg-only.sa 캐시 저장 ts, end
        mock_time.time.side_effect = [0.0, 1.0, 5.0]
        mock_time.sleep = MagicMock()

        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            ["ddg only query Saudi"],
            {"relevance_score": 0.90, "category": "pharma_retailer",
             "has_price_data": True, "has_product_listing": True,
             "language": "en", "reason": "DDG only"},
        ]
        mock_http = MagicMock()
        mock_fetch_search.return_value = [
            {"url": "https://ddg-only.sa/drugs", "title": "DDG Only",
             "snippet": "pharma", "domain": "ddg-only.sa"},
        ]
        mock_fetch_html.return_value = "<html><body>pharma</body></html>"

        drug_info = {"trade_name": "DDGDrug", "ingredients": "DDGIng",
                     "dosage_form": "Tab", "strength": "50mg"}

        result = discover_sources(
            mock_llm, mock_http, drug_info,
            pplx_client=None,
            max_queries=1, fetch_delay=0,
        )

        self.assertEqual(len(result.valid_sources), 1)
        mock_llm.ask_json.assert_called()

    def test_perplexity_unavailable_skips(self):
        """pplx_client.available=False이면 Perplexity 건너뜀."""
        pplx = self._make_pplx_client([], available=False)

        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = ["fallback query"]
        mock_http = MagicMock()

        drug_info = {"trade_name": "SkipDrug", "ingredients": "SkipIng",
                     "dosage_form": "Tab", "strength": "5mg"}

        # pplx.available=False이므로 search_pharma_sources 호출 안 됨
        # DDG 쪽은 fetch_search_results가 mock 안 되어 빈 결과 반환
        with patch("assets.snippets.source_discoverer.fetch_search_results", return_value=[]), \
             patch("assets.snippets.source_discoverer.time") as mock_time:
            mock_time.time.side_effect = [0.0, 1.0]
            mock_time.sleep = MagicMock()

            result = discover_sources(
                mock_llm, mock_http, drug_info,
                pplx_client=pplx, max_queries=1, fetch_delay=0,
            )

        pplx.search_pharma_sources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
