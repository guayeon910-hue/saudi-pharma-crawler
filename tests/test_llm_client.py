"""
test_llm_client.py — llm_client.py 단위 테스트

API 키 없이 구조 검증 + mock 응답 테스트.
"""

import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from assets.snippets.llm_client import (
    ClaudeClient,
    LLMResponse,
    TokenUsage,
    MODEL_HAIKU,
    MODEL_SONNET,
    DEFAULT_MODEL,
    get_client,
)


class TestTokenUsage(unittest.TestCase):
    def test_add_and_summary(self):
        u = TokenUsage()
        u.add(100, 50)
        u.add(200, 80)
        s = u.summary()
        self.assertEqual(s["input_tokens"], 300)
        self.assertEqual(s["output_tokens"], 130)
        self.assertEqual(s["total_tokens"], 430)
        self.assertEqual(s["requests"], 2)
        self.assertEqual(s["errors"], 0)

    def test_error_count(self):
        u = TokenUsage()
        u.errors += 1
        u.errors += 1
        self.assertEqual(u.summary()["errors"], 2)


class TestLLMResponse(unittest.TestCase):
    def test_parse_json_plain(self):
        r = LLMResponse(
            text='{"key": "value", "num": 42}',
            input_tokens=10, output_tokens=20,
            model="test", stop_reason="end_turn",
        )
        data = r.parse_json()
        self.assertEqual(data["key"], "value")
        self.assertEqual(data["num"], 42)

    def test_parse_json_codeblock(self):
        r = LLMResponse(
            text='```json\n{"wrapped": true}\n```',
            input_tokens=10, output_tokens=20,
            model="test", stop_reason="end_turn",
        )
        data = r.parse_json()
        self.assertTrue(data["wrapped"])

    def test_parse_json_with_whitespace(self):
        r = LLMResponse(
            text='  \n  {"spaced": true}  \n  ',
            input_tokens=10, output_tokens=20,
            model="test", stop_reason="end_turn",
        )
        data = r.parse_json()
        self.assertTrue(data["spaced"])

    def test_parse_json_array(self):
        r = LLMResponse(
            text='[1, 2, 3]',
            input_tokens=10, output_tokens=20,
            model="test", stop_reason="end_turn",
        )
        data = r.parse_json()
        self.assertEqual(data, [1, 2, 3])


class TestClaudeClient(unittest.TestCase):
    def test_model_constants(self):
        self.assertEqual(DEFAULT_MODEL, MODEL_HAIKU)
        self.assertIn("haiku", MODEL_HAIKU)
        self.assertIn("sonnet", MODEL_SONNET)

    def test_available_without_key(self):
        with patch.dict(os.environ, {"CLAUDE_API_KEY": ""}, clear=False):
            c = ClaudeClient(api_key="")
            self.assertFalse(c.available)

    def test_available_with_key(self):
        c = ClaudeClient(api_key="sk-ant-test-key")
        self.assertTrue(c.available)
        c.close()

    @patch("httpx.Client")
    def test_ask_success(self, MockClient):
        """정상 응답 시뮬레이션."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"text": '{"result": "ok"}'}],
            "usage": {"input_tokens": 50, "output_tokens": 30},
            "stop_reason": "end_turn",
        }

        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = ClaudeClient(api_key="sk-test")
        resp = client.ask("test prompt", system="sys")

        self.assertEqual(resp.text, '{"result": "ok"}')
        self.assertEqual(resp.input_tokens, 50)
        self.assertEqual(resp.output_tokens, 30)
        self.assertEqual(client.usage.summary()["requests"], 1)

    @patch("httpx.Client")
    def test_ask_json(self, MockClient):
        """ask_json이 parse_json을 자동 적용하는지."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"text": '{"pharma": true, "count": 5}'}],
            "usage": {"input_tokens": 40, "output_tokens": 25},
            "stop_reason": "end_turn",
        }

        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = ClaudeClient(api_key="sk-test")
        data = client.ask_json("extract info")

        self.assertTrue(data["pharma"])
        self.assertEqual(data["count"], 5)

    @patch("httpx.Client")
    def test_retry_on_429(self, MockClient):
        """429 시 재시도하는지."""
        fail_resp = MagicMock()
        fail_resp.status_code = 429
        fail_resp.headers = {"retry-after": "0.1"}

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            "content": [{"text": "recovered"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        }

        mock_http = MagicMock()
        mock_http.post.side_effect = [fail_resp, ok_resp]
        MockClient.return_value = mock_http

        client = ClaudeClient(api_key="sk-test")
        # BASE_DELAY를 줄여서 테스트 빠르게
        import assets.snippets.llm_client as mod
        original_delay = mod.BASE_DELAY
        mod.BASE_DELAY = 0.01
        try:
            resp = client.ask("retry test")
            self.assertEqual(resp.text, "recovered")
            self.assertEqual(client.usage.errors, 1)
            self.assertEqual(client.usage.requests, 1)
        finally:
            mod.BASE_DELAY = original_delay

    @patch("httpx.Client")
    def test_request_body_structure(self, MockClient):
        """API 요청 body가 올바른 구조인지."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "stop_reason": "end_turn",
        }

        mock_http = MagicMock()
        mock_http.post.return_value = mock_resp
        MockClient.return_value = mock_http

        client = ClaudeClient(api_key="sk-test", default_model=MODEL_HAIKU)
        client.ask("hello", system="you are a helper", temperature=0.5)

        call_args = mock_http.post.call_args
        body = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        self.assertEqual(body["model"], MODEL_HAIKU)
        self.assertEqual(body["temperature"], 0.5)
        self.assertEqual(body["system"], "you are a helper")
        self.assertEqual(body["messages"][0]["content"], "hello")

    def test_context_manager(self):
        """with 문 지원."""
        with ClaudeClient(api_key="sk-test") as c:
            self.assertTrue(c.available)

    def test_singleton_get_client(self):
        """get_client 싱글턴 반환."""
        import assets.snippets.llm_client as mod
        mod._default_client = None
        with patch.dict(os.environ, {"CLAUDE_API_KEY": "sk-singleton"}, clear=False):
            c1 = get_client()
            c2 = get_client()
            self.assertIs(c1, c2)
            mod._default_client = None  # cleanup


if __name__ == "__main__":
    unittest.main()
