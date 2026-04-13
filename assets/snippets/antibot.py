"""
antibot.py — Anti-bot 탐지 + 규칙 기반 적응형 대응 + User-Agent 회전

SciSpace 논문 §6.2 (AntiBotDetector / AdaptiveCrawler) 기반.
LLM 플래너 대신 규칙 테이블로 대응 — 고정 API 타겟에 적합한 경량 구현.

구성 요소:
  1. AntiBotType enum    — 탐지 결과 분류
  2. detect()            — HTTP 응답에서 Anti-bot 유형 판별
  3. COUNTERMEASURES      — 유형별 자동 대응 규칙
  4. UA_POOL              — User-Agent 회전용 풀
  5. pick_ua()            — 랜덤 UA 선택

통합 지점:
  - backoff_retry.py: detect() 결과에 따라 wait 시간 조정
  - main.py: detect() → CircuitBreaker.force_open() 연동
  - sfda_web.py: pick_ua()로 요청 헤더 교체
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AntiBotType(Enum):
    """Anti-bot 탐지 결과 분류."""
    CLOUDFLARE = "cloudflare"
    RECAPTCHA = "recaptcha"
    RATE_LIMIT = "rate_limit"
    IP_BLOCK = "ip_block"
    WAF_GENERIC = "waf_generic"
    NONE = "none"


@dataclass(frozen=True)
class Countermeasure:
    """유형별 자동 대응 규칙."""
    action: str                  # delay / circuit_break / backoff
    delay_multiplier: float      # 기존 delay에 곱하는 배수 (0 = 즉시 중단)
    extra_headers: dict | None   # 추가할 HTTP 헤더
    should_circuit_break: bool   # True면 CircuitBreaker.force_open()


COUNTERMEASURES: dict[AntiBotType, Countermeasure] = {
    AntiBotType.CLOUDFLARE: Countermeasure(
        action="add_delay_and_headers",
        delay_multiplier=3.0,
        extra_headers={"Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br"},
        should_circuit_break=False,
    ),
    AntiBotType.RATE_LIMIT: Countermeasure(
        action="respect_retry_after",
        delay_multiplier=2.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
    AntiBotType.IP_BLOCK: Countermeasure(
        action="circuit_break",
        delay_multiplier=0,
        extra_headers=None,
        should_circuit_break=True,
    ),
    AntiBotType.RECAPTCHA: Countermeasure(
        action="circuit_break",
        delay_multiplier=0,
        extra_headers=None,
        should_circuit_break=True,
    ),
    AntiBotType.WAF_GENERIC: Countermeasure(
        action="exponential_backoff",
        delay_multiplier=5.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
    AntiBotType.NONE: Countermeasure(
        action="none",
        delay_multiplier=1.0,
        extra_headers=None,
        should_circuit_break=False,
    ),
}


# ─── User-Agent 회전 풀 ──────────────────────────────────
# 실제 브라우저 UA 문자열. 6개월마다 업데이트 권장.
UA_POOL: list[str] = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Edge (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome (Linux)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def pick_ua() -> str:
    """UA_POOL에서 랜덤 선택."""
    return random.choice(UA_POOL)


# ─── Anti-bot 탐지 ────────────────────────────────────────
# Cloudflare 시그니처 패턴
_CF_BODY_PATTERNS = (
    "cloudflare",
    "cf-challenge",
    "cf-browser-verification",
    "checking your browser",
    "ray id:",
    "performance & security by cloudflare",
)

_CF_HEADER_PATTERNS = ("cloudflare", "cf-ray")

# CAPTCHA 패턴
_CAPTCHA_PATTERNS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "captcha-container",
    "captcha_challenge",
)

# WAF 상태코드 (Cloudflare, Akamai 등)
_WAF_STATUS_CODES = frozenset({520, 521, 522, 523, 524, 525, 526})


def detect(
    status_code: int,
    body: str = "",
    headers: Optional[dict[str, str]] = None,
) -> AntiBotType:
    """HTTP 응답에서 Anti-bot 유형을 판별한다.

    판별 우선순위:
      1. Cloudflare (body 패턴 + header 시그니처)
      2. Rate Limit (429)
      3. CAPTCHA (body 패턴)
      4. IP Block (403, CAPTCHA 아닌 경우)
      5. WAF Generic (5xx 특수 코드)
      6. None (정상)

    Args:
        status_code: HTTP 상태 코드
        body: 응답 본문 (HTML 또는 텍스트)
        headers: 응답 헤더 dict

    Returns:
        AntiBotType 열거값
    """
    headers = headers or {}
    body_lower = body.lower()
    headers_lower = {k.lower(): v.lower() for k, v in headers.items()}

    # 1. Cloudflare 시그니처 (body 또는 header)
    cf_in_body = any(p in body_lower for p in _CF_BODY_PATTERNS)
    cf_in_headers = any(
        any(p in v for p in _CF_HEADER_PATTERNS)
        for v in headers_lower.values()
    )
    if cf_in_body or cf_in_headers:
        return AntiBotType.CLOUDFLARE

    # 2. Rate Limit
    if status_code == 429:
        return AntiBotType.RATE_LIMIT

    # 3. CAPTCHA (403 + CAPTCHA 패턴)
    if status_code == 403:
        if any(p in body_lower for p in _CAPTCHA_PATTERNS):
            return AntiBotType.RECAPTCHA
        return AntiBotType.IP_BLOCK

    # 4. WAF 특수 코드 (Cloudflare 5xx)
    if status_code in _WAF_STATUS_CODES:
        return AntiBotType.WAF_GENERIC

    # 5. 일반 서버 오류 (500, 502, 503, 504) — 반봇이 아닌 서버 문제
    #    backoff_retry.py가 처리하므로 여기서는 NONE 반환
    return AntiBotType.NONE


def get_countermeasure(antibot_type: AntiBotType) -> Countermeasure:
    """탐지 유형에 대한 대응 규칙 반환."""
    return COUNTERMEASURES.get(antibot_type, COUNTERMEASURES[AntiBotType.NONE])
