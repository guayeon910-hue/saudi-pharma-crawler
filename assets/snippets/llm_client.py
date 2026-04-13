"""
llm_client.py — Claude API 래퍼 (AI 자율 서칭용)

역할:
  - Anthropic Messages API 호출 (Haiku / Sonnet 선택)
  - 재시도 + 지수 백오프 (429/5xx)
  - 토큰 사용량 추적 (감사로그 연동)
  - JSON 모드 응답 파싱

통합 지점:
  - source_discoverer.py: 검색 쿼리 생성, 사이트 판별
  - auto_scraper.py: XPath 생성, 추출값 검증
  - html_preprocessor.py: 스니펫 → 프롬프트 구성
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 모델 상수
# ---------------------------------------------------------------------------
MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"

DEFAULT_MODEL = MODEL_HAIKU  # 비용 최적: 대부분 Haiku로 충분

# ---------------------------------------------------------------------------
# 토큰 사용량 추적
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    """누적 토큰 사용량."""
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    errors: int = 0

    def add(self, input_tok: int, output_tok: int) -> None:
        self.input_tokens += input_tok
        self.output_tokens += output_tok
        self.requests += 1

    def summary(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "requests": self.requests,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# LLM 응답 래퍼
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """LLM 호출 결과."""
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str

    def parse_json(self) -> Any:
        """응답 텍스트에서 JSON 추출. 마크다운 코드블록도 처리."""
        text = self.text.strip()
        # ```json ... ``` 블록 처리
        if text.startswith("```"):
            lines = text.split("\n")
            # 첫 줄(```json)과 마지막 줄(```) 제거
            inner = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
            return json.loads(inner)
        return json.loads(text)


# ---------------------------------------------------------------------------
# Claude 클라이언트
# ---------------------------------------------------------------------------

# 재시도 대상 상태 코드
_RETRYABLE = {429, 500, 502, 503, 529}

# 최대 재시도 횟수
MAX_RETRIES = 3

# 기본 대기 시간 (초)
BASE_DELAY = 2.0


class ClaudeClient:
    """Anthropic Messages API 클라이언트.

    Parameters
    ----------
    api_key : str | None
        CLAUDE_API_KEY. None이면 환경변수에서 읽음.
    default_model : str
        기본 모델 ID.
    max_tokens : int
        기본 max_tokens.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        max_tokens: int = 1024,
    ):
        self._api_key = api_key or os.environ.get("CLAUDE_API_KEY", "")
        self._default_model = default_model
        self._default_max_tokens = max_tokens
        self.usage = TokenUsage()

        # httpx는 프로젝트에서 이미 사용 중 — 추가 의존성 없음
        import httpx
        self._http = httpx.Client(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=60.0,
        )

    @property
    def available(self) -> bool:
        """API 키가 설정되었는지 확인."""
        return bool(self._api_key)

    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """단일 메시지 호출.

        Parameters
        ----------
        prompt : str
            사용자 메시지.
        system : str
            시스템 프롬프트.
        model : str | None
            모델 오버라이드.
        max_tokens : int | None
            max_tokens 오버라이드.
        temperature : float
            생성 온도 (기본 0.0 = 결정적).

        Returns
        -------
        LLMResponse
        """
        model = model or self._default_model
        max_tokens = max_tokens or self._default_max_tokens

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system

        return self._call_with_retry(body, model)

    def ask_json(
        self,
        prompt: str,
        *,
        system: str = "",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """JSON 응답을 기대하는 호출. parse_json() 자동 적용."""
        resp = self.ask(
            prompt,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return resp.parse_json()

    # -------------------------------------------------------------------
    # 내부
    # -------------------------------------------------------------------

    def _call_with_retry(self, body: dict, model: str) -> LLMResponse:
        """지수 백오프 재시도."""
        import httpx

        last_err: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.post("/v1/messages", json=body)

                if resp.status_code == 200:
                    data = resp.json()
                    text = data["content"][0]["text"]
                    usage = data.get("usage", {})
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    self.usage.add(in_tok, out_tok)

                    return LLMResponse(
                        text=text,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        model=model,
                        stop_reason=data.get("stop_reason", ""),
                    )

                if resp.status_code in _RETRYABLE and attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    # 429일 때 retry-after 헤더 존중
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("retry-after")
                        if retry_after:
                            delay = max(delay, float(retry_after))
                    logger.warning(
                        "Claude API %d, retry %d/%d (%.1fs)",
                        resp.status_code, attempt + 1, MAX_RETRIES, delay,
                    )
                    self.usage.errors += 1
                    time.sleep(delay)
                    continue

                # 복구 불가능한 에러
                resp.raise_for_status()

            except httpx.TimeoutException as e:
                last_err = e
                self.usage.errors += 1
                if attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning("Claude API timeout, retry %d/%d", attempt + 1, MAX_RETRIES)
                    time.sleep(delay)
                    continue
                raise

        # 여기 도달하면 모든 재시도 실패
        raise RuntimeError(f"Claude API failed after {MAX_RETRIES} retries: {last_err}")

    def close(self) -> None:
        """HTTP 클라이언트 정리."""
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# 편의 함수 (모듈 레벨)
# ---------------------------------------------------------------------------

_default_client: Optional[ClaudeClient] = None


def get_client() -> ClaudeClient:
    """싱글턴 클라이언트 반환."""
    global _default_client
    if _default_client is None:
        _default_client = ClaudeClient()
    return _default_client
