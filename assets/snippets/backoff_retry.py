"""
backoff_retry.py — HTTP 요청용 지수 백오프 + 지터 + 429 Retry-After 존중

핵심 원칙:
1. 429면 서버의 Retry-After 헤더를 최우선으로 따른다 (초 + HTTP-date 둘 다)
2. 5xx는 지터 포함 지수 백오프
3. 4xx (401/403/404)는 "비즈니스 로직이 처리할 에러"라 여기서 재시도 안 함
   (401은 SFDAClient가 자체 재시도, 403은 User-Agent 로테이션 등)
4. 재시도는 "성능 최적화"가 아니라 "안전장치" — 횟수 제한 필수

⚠️ IDEMPOTENCY 경고:
이 데코레이터는 **GET 및 idempotent 메서드에만** 적용하라.
POST/PATCH 같은 non-idempotent 호출에 쓰면 네트워크 타임아웃 시
서버에 중복 부작용(중복 주문·중복 insert)이 생길 수 있다.
SFDA/NUPCO 조회 API는 전부 GET이므로 현 사용처는 안전.

사용 예:
    @with_backoff(max_attempts=3, base=3.0, max_wait=60.0)
    def fetch(url: str) -> httpx.Response:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp
"""

from __future__ import annotations

import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

import httpx

from antibot import AntiBotType, detect as detect_antibot, get_countermeasure


logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class RetryExhausted(Exception):
    """최대 재시도 횟수 초과"""


def _compute_wait(
    attempt: int,
    *,
    base: float,
    max_wait: float,
    jitter: float,
) -> float:
    """지수 백오프 + 지터.

    attempt=0 → base + jitter
    attempt=1 → base*2 + jitter
    attempt=2 → base*4 + jitter
    """
    exponential = base * (2**attempt)
    jittered = exponential + random.uniform(0, jitter)
    return min(jittered, max_wait)


def _parse_retry_after(header_value: str | None) -> float | None:
    """Retry-After 헤더 파싱. 초(delta-seconds) 또는 HTTP-date 둘 다 지원.

    RFC 7231 §7.1.3에 따라 Retry-After는 두 형식이 허용된다:
      1) delta-seconds: "120"
      2) HTTP-date    : "Wed, 21 Oct 2026 07:28:00 GMT"

    Cloudflare 앞단 사이트(일부 소매 약국)는 HTTP-date를 반환하는 경우가
    있어서 둘 다 처리한다. 파싱 실패 시 None 반환 → 호출자가 지수 백오프로
    폴백.
    """
    if not header_value:
        return None
    header_value = header_value.strip()

    # 1) delta-seconds
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass

    # 2) HTTP-date
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(header_value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def with_backoff(
    *,
    max_attempts: int = 3,
    base: float = 3.0,
    max_wait: float = 60.0,
    jitter: float = 2.0,
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> Callable[[F], F]:
    """HTTP 호출 함수를 감싸는 재시도 데코레이터.

    함수가 httpx.Response를 리턴하거나 httpx.HTTPStatusError를 던져야 한다.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    last_exc = e

                    # ── Anti-bot 탐지 (SciSpace §6.2) ──
                    resp_body = ""
                    try:
                        resp_body = e.response.text[:2000]
                    except Exception:
                        pass
                    resp_headers = dict(e.response.headers) if e.response else {}
                    ab_type = detect_antibot(status, resp_body, resp_headers)
                    cm = get_countermeasure(ab_type)

                    # CAPTCHA/IP차단 → 재시도 무의미, 즉시 전파 (main.py가 circuit break)
                    if cm.should_circuit_break:
                        logger.warning(
                            "Anti-bot 탐지: %s → 즉시 전파 (circuit break 권고)",
                            ab_type.value,
                        )
                        raise

                    # 재시도 대상이 아닌 상태코드는 즉시 전파
                    if status not in retry_on_status:
                        raise

                    # 마지막 시도면 전파
                    if attempt == max_attempts - 1:
                        break

                    # ── 대기 시간 산출 (Anti-bot 배수 적용) ──
                    wait: float
                    if status == 429:
                        retry_after = _parse_retry_after(
                            e.response.headers.get("Retry-After")
                        )
                        if retry_after is not None:
                            wait = min(retry_after * cm.delay_multiplier, max_wait)
                            logger.warning(
                                "429 받음 [%s]. Retry-After=%s × %.1f배 = %.1f초 대기",
                                ab_type.value, retry_after,
                                cm.delay_multiplier, wait,
                            )
                        else:
                            wait = _compute_wait(
                                attempt, base=base, max_wait=max_wait, jitter=jitter
                            ) * cm.delay_multiplier
                            wait = min(wait, max_wait)
                            logger.warning(
                                "429 받음 [%s]. Retry-After 없음. %.1f초 대기",
                                ab_type.value, wait,
                            )
                    else:
                        wait = _compute_wait(
                            attempt, base=base, max_wait=max_wait, jitter=jitter
                        ) * cm.delay_multiplier
                        wait = min(wait, max_wait)
                        logger.warning(
                            "%d 받음 [%s] (시도 %d/%d). %.1f초 대기",
                            status, ab_type.value,
                            attempt + 1, max_attempts, wait,
                        )
                    time.sleep(wait)

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_exc = e
                    if attempt == max_attempts - 1:
                        break
                    wait = _compute_wait(
                        attempt, base=base, max_wait=max_wait, jitter=jitter
                    )
                    logger.warning(
                        "네트워크 오류: %s. %.1f초 대기 후 재시도", e, wait,
                    )
                    time.sleep(wait)

            raise RetryExhausted(
                f"{func.__name__} 최대 재시도 초과 (attempts={max_attempts})"
            ) from last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# ─── 사용 예시 ─────────────────────────────────────────
if __name__ == "__main__":
    @with_backoff(max_attempts=3)
    def fetch(url: str) -> httpx.Response:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp

    # 실제 사용시:
    # response = fetch("https://example.com/api")
