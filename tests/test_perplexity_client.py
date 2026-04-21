"""
test_perplexity_client.py — perplexity_client.py 단위 테스트

API 키 없이 구조 검증 + mock 응답 테스트.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from assets.snippets.perplexity_client import (
    PerplexityClient,
    MODEL_SONAR,
    MODEL_SONAR_PRO,
    DEFAULT_MODEL,
    SEARCH_SYSTEM_PROMPT,
    SEARCH_USER_TEMPLATE,
)


# ---------------------------------------------------------------------------
# 헬퍼: Perplexity API mock 응답 생성
# ---------------------------------------------------------------------------

def _make_pplx_response(
    sources_json: list[dict],
    citations: list[str] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
) -> dict:
    """Perplexity chat/completions 응답 형태."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(sources_json, ensure_ascii=False),
                    "role": "assistant",
                },
                "finish_reason": "stop",
            }
        ],
        "citations": citations or [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


SAMPLE_SOURCES = [
    {
        "url": "https://dawaya.sa/products/paracetamol",
        "title": "Dawaya Saudi Pharmacy",
        "description": "Online pharmacy with drug prices in Saudi Arabia",
        "category": "pharma_retailer",
        "has_price_data": True,
        "has_product_listing": True,
        "language": "en",
        "relevance_score": 0.85,
    },
    {
        "url": "https://saudidrugprices.com/search",
        "title": "Saudi Drug Prices Database",
        "description": "Comprehensive drug pricing database for KSA",
        "category": "price_database",
        "has_price_data": True,
        "has_product_listing": False,
        "language": "mixed",
        "relevance_score": 0.90,
    },
    {
        "url": "https://lowrelevance.example.com/page",
        "title": "Some News Site",
        "description": "Unrelated content",
        "category": "news",
        "has_price_data": False,
        "has_product_listing": False,
        "language": "en",
        "relevance_score": 0.30,
    },
]

SAMPLE_CITATIONS = [
    "https://dawaya.sa/products/paracetamol",
    "https://saudidrugprices.com/search",
    "https://extra-citation.sa/pharma",
]

DRUG_INFO = {
    "trade_name": "Paracetamol Extra",
    "ingredients": "Paracetamol",
    "dosage_form": "Tablet",
    "strength": "500mg",
}

EXCLUDED_DOMAINS = {
    "sfda.gov.sa", "nahdi.sa", "www.nahdi.sa",
    "noon.com", "www.noon.com",
}


class TestPerplexityConstants(unittest.TestCase):
    def test_model_constants(self):
        self.assertEqual(DEFAULT_MODEL, MODEL_SONAR)
        self.assertEqual(MODEL_SONAR, "sonar")
        self.assertEqual(MODEL_SONAR_PRO, "sonar-pro")

    def test_prompts_exist(self):
        self.assertIn("Saudi", SEARCH_SYSTEM_PROMPT)
        self.assertIn("{trade_name}", SEARCH_USER_TEMPLATE)
        self.assertIn("{excluded}", SEARCH_USER_TEMPLATE)


class TestPerplexityClientInit(unittest.TestCase):
    def test_available_without_key(self):
        with patch.dict(os.environ, {"PERPLEXITY_API_KEY": ""}, clear=False):
            c = PerplexityClient(api_key="")
            self.assertFalse(c.available)
            c.close()

    def test_available_with_key(self):
        c = PerplexityClient(api_key="pplx-test-key")
        self.assertTrue(c.available)
        self.assertEqual(c.model, MODEL_SONAR)
        c.close()

    def test_custom_model(self):
        c = PerplexityClient(api_key="pplx-test", model="sonar-pro")
        self.assertEqual(c.model, "sonar-pro")
        c.close()

    def test_context_manager(self):
        with PerplexityClient(api_key="pplx-test") as c:
            self.assertTrue(c.available)


class TestSearchPharmaSources(unittest.TestCase):
    @patch("httpx.Client")
    def test_success_with_citations(self, MockClient):
        """정상 응답 + citations 병합."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response(
            SAMPLE_SOURCES, SAMPLE_CITATIONS,
        )

        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        # AI 응답에서 3개 + citations에서 추가 1개 (extra-citation.sa)
        # lowrelevance.example.com도 포함 (필터는 score가 아닌 domain만)
        self.assertGreaterEqual(len(results), 3)

        # 도메인 확인
        domains = {r["domain"] for r in results}
        self.assertIn("dawaya.sa", domains)
        self.assertIn("saudidrugprices.com", domains)
        self.assertIn("extra-citation.sa", domains)

        # 토큰 추적
        summary = client.usage.summary()
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 200)
        self.assertEqual(summary["requests"], 1)

    @patch("httpx.Client")
    def test_excluded_domains_filtered(self, MockClient):
        """제외 도메인이 결과에서 필터링되는지."""
        sources_with_excluded = [
            {
                "url": "https://www.nahdi.sa/products/para",
                "title": "Nahdi",
                "description": "Should be excluded",
                "category": "pharma_retailer",
                "has_price_data": True,
                "has_product_listing": True,
                "language": "en",
                "relevance_score": 0.95,
            },
            SAMPLE_SOURCES[0],  # dawaya.sa — OK
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response(sources_with_excluded)
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        domains = {r["domain"] for r in results}
        self.assertNotIn("www.nahdi.sa", domains)
        self.assertIn("dawaya.sa", domains)

    @patch("httpx.Client")
    def test_empty_response(self, MockClient):
        """빈 결과 처리."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response([], [])
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
        self.assertEqual(results, [])

    @patch("httpx.Client")
    def test_json_in_codeblock(self, MockClient):
        """마크다운 코드블록으로 감싼 JSON 처리."""
        content = '```json\n' + json.dumps([SAMPLE_SOURCES[0]]) + '\n```'

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": content, "role": "assistant"}, "finish_reason": "stop"}],
            "citations": [],
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        }
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["domain"], "dawaya.sa")

    @patch("httpx.Client")
    def test_non_json_response(self, MockClient):
        """JSON 파싱 실패 시 빈 리스트 graceful 반환 (파이프라인 중단 없음)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Sorry, I cannot help.", "role": "assistant"}, "finish_reason": "stop"}],
            "citations": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        # JSON 파싱 실패 시 예외 대신 빈 리스트 반환 (graceful degradation)
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 0)

    @patch("httpx.Client")
    def test_duplicate_domains_deduplicated(self, MockClient):
        """동일 도메인 중복 제거."""
        dupes = [
            {**SAMPLE_SOURCES[0], "url": "https://dawaya.sa/page1"},
            {**SAMPLE_SOURCES[0], "url": "https://dawaya.sa/page2"},
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response(dupes)
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        dawaya_results = [r for r in results if "dawaya" in r["domain"]]
        self.assertEqual(len(dawaya_results), 1)

    @patch("httpx.Client")
    def test_request_body_structure(self, MockClient):
        """API 요청 body가 올바른 구조인지."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response([SAMPLE_SOURCES[0]])
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        call_args = mock_http.post.call_args
        body = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        self.assertEqual(body["model"], "sonar")
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["role"], "user")
        # 프롬프트에 약품 정보 포함
        self.assertIn("Paracetamol", body["messages"][1]["content"])
        # 제외 도메인 포함
        self.assertIn("sfda.gov.sa", body["messages"][1]["content"])


class TestRetryLogic(unittest.TestCase):
    @patch("httpx.Client")
    def test_retry_on_429(self, MockClient):
        """429 시 재시도."""
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"retry-after": "0.01"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = _make_pplx_response([SAMPLE_SOURCES[0]])

        mock_http = MagicMock()
        mock_http.post.side_effect = [fail_resp, ok_resp]
        MockClient.return_value = mock_http

        import assets.snippets.perplexity_client as mod
        original_delay = mod.BASE_DELAY
        mod.BASE_DELAY = 0.01
        try:
            client = PerplexityClient(api_key="pplx-test")
            results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
            self.assertGreaterEqual(len(results), 1)
            self.assertEqual(client.usage.errors, 1)
        finally:
            mod.BASE_DELAY = original_delay

    @patch("httpx.Client")
    def test_retry_on_500(self, MockClient):
        """500 시 재시도."""
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.headers = {}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = _make_pplx_response([SAMPLE_SOURCES[0]])

        mock_http = MagicMock()
        mock_http.post.side_effect = [fail_resp, ok_resp]
        MockClient.return_value = mock_http

        import assets.snippets.perplexity_client as mod
        original_delay = mod.BASE_DELAY
        mod.BASE_DELAY = 0.01
        try:
            client = PerplexityClient(api_key="pplx-test")
            results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
            self.assertGreaterEqual(len(results), 1)
        finally:
            mod.BASE_DELAY = original_delay

    @patch("httpx.Client")
    def test_non_retryable_error(self, MockClient):
        """401 등 비재시도 에러는 즉시 raise."""
        fail_resp = MagicMock()
        fail_resp.status_code = 401
        fail_resp.headers = {}
        fail_resp.raise_for_status.side_effect = Exception("Unauthorized")

        mock_http = MagicMock()
        mock_http.post.return_value = fail_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-bad-key")
        with self.assertRaises(Exception):
            client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        # 1회만 호출 (재시도 없음)
        self.assertEqual(mock_http.post.call_count, 1)


class TestCitationMerge(unittest.TestCase):
    @patch("httpx.Client")
    def test_citation_only_urls_added(self, MockClient):
        """AI 응답에 없지만 citations에만 있는 URL이 추가되는지."""
        # AI 응답: dawaya.sa만
        # citations: dawaya.sa + newsite.sa
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response(
            [SAMPLE_SOURCES[0]],  # dawaya.sa
            citations=[
                "https://dawaya.sa/products/paracetamol",
                "https://newsite.sa/drugs",
            ],
        )
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)

        domains = {r["domain"] for r in results}
        self.assertIn("dawaya.sa", domains)
        self.assertIn("newsite.sa", domains)

        # citation-only 소스의 기본 score
        newsite = [r for r in results if r["domain"] == "newsite.sa"][0]
        self.assertEqual(newsite["relevance_score"], 0.65)
        self.assertEqual(newsite["description"], "Perplexity citation")

    @patch("httpx.Client")
    def test_citation_excluded_domain_not_added(self, MockClient):
        """citations에 있어도 제외 도메인이면 추가 안 됨."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_pplx_response(
            [],
            citations=["https://www.nahdi.sa/products"],
        )
        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = PerplexityClient(api_key="pplx-test")
        results = client.search_pharma_sources(DRUG_INFO, EXCLUDED_DOMAINS)
        self.assertEqual(len(results), 0)


if __name__ == "__main__":
    unittest.main()
