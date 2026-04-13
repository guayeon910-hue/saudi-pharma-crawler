"""
trafilatura_fallback.py — Triple Fallback 3차: Trafilatura 기반 본문 추출

시뮬레이션 결과 기반 설계 (2026-04-10):
┌─────────────────────────────────────────────────────────────────┐
│ SUCCESS (HTTP 200 + 정적 HTML)                                  │
│   sfda_drugs_list_html — 833자, 가격(SAR) + 아랍어 감지         │
│   etimad_portal        — 2,717자, 서비스 설명 추출              │
│   whites_web           — 464자, 카테고리/상품명 감지            │
│                                                                 │
│ PARTIAL (JS 렌더링으로 본문 부족)                               │
│   sfda_companies  — 217자, 네비게이션만                         │
│   nupco_tenders   — 188자, 텐더 헤더만                          │
│                                                                 │
│ FAIL (Trafilatura 이전 단계 문제)                               │
│   nahdi_web       — SPA, __NEXT_DATA__ JSON 파싱으로 우회       │
│   al_dawaa_web    — 403 WAF 차단 (UA 로테이션 선행 필요)        │
│   tamer_group     — 403 WAF 차단                                │
│   noon_saudi      — 403 Cloudflare                              │
│   nahdi_medicine  — 404 URL 구조 변경                           │
└─────────────────────────────────────────────────────────────────┘

적용 규칙:
1. Trafilatura는 "마지막 안전망"으로만 사용 — CSS 셀렉터 성공하면 호출하지 않음
2. JS 렌더링 소스(__NEXT_DATA__ 등)는 JSON 파싱을 먼저 시도
3. 추출 결과가 50자 미만이면 실패로 판정
4. 가격표/상품카드 직접 파싱에는 사용하지 않음 (텍스트 밀도 원리의 한계)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import trafilatura
from trafilatura.settings import use_config

logger = logging.getLogger("fallback.trafilatura")


# ─── Trafilatura 설정 ─────────────────────────────────
_traf_config = use_config()
_traf_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "100")
_traf_config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "50")


# ─── 결과 컨테이너 ───────────────────────────────────
@dataclass
class ExtractionResult:
    """Trafilatura 추출 결과"""
    success: bool
    text: Optional[str] = None
    text_length: int = 0
    method: str = "none"              # trafilatura | next_data | nuxt | initial_state
    metadata: Optional[dict] = None
    has_prices: bool = False
    has_products: bool = False
    has_arabic: bool = False
    confidence_modifier: float = 0.0  # confidence 가감값

    @property
    def confidence_penalty(self) -> float:
        """Trafilatura fallback 사용 시 confidence 감점.

        시뮬레이션 결과:
        - 정적 HTML 성공: -0.05 (구조가 명확)
        - JSON 파싱 성공: -0.03 (구조화 데이터)
        - 짧은 추출:     -0.10 (신뢰도 낮음)
        """
        if not self.success:
            return -0.15
        if self.method in ("next_data", "nuxt", "initial_state"):
            return -0.03
        if self.text_length < 200:
            return -0.10
        return -0.05


# ─── 구조화 데이터 탐색 (2.5차 폴백) ─────────────────
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json">\s*({.*?})\s*</script>',
    re.DOTALL,
)
_NUXT_RE = re.compile(
    r'window\.__NUXT__\s*=\s*({.*?});\s*</script>',
    re.DOTALL,
)
_INITIAL_STATE_RE = re.compile(
    r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
    re.DOTALL,
)
_PRELOADED_RE = re.compile(
    r'window\.__PRELOADED_STATE__\s*=\s*({.*?});\s*</script>',
    re.DOTALL,
)


def _try_extract_json_state(html: str) -> Optional[tuple[dict, str]]:
    """HTML에서 __NEXT_DATA__, __NUXT__, __INITIAL_STATE__ 등을 탐색.

    시뮬레이션에서 nahdi_web(862KB HTML, Trafilatura 추출 0자)이
    __NEXT_DATA__를 갖고 있었음 → 이 경로로 우회.

    Returns:
        (parsed_json, method_name) or None
    """
    for pattern, method_name in [
        (_NEXT_DATA_RE, "next_data"),
        (_NUXT_RE, "nuxt"),
        (_INITIAL_STATE_RE, "initial_state"),
        (_PRELOADED_RE, "preloaded_state"),
    ]:
        match = pattern.search(html)
        if match:
            try:
                data = json.loads(match.group(1))
                return data, method_name
            except (json.JSONDecodeError, ValueError):
                logger.debug("JSON 파싱 실패: %s", method_name)
                continue
    return None


def _flatten_json_text(data: dict, max_depth: int = 5) -> str:
    """중첩 JSON에서 텍스트 값만 평탄화하여 추출.

    상품명, 가격, 설명 등이 깊은 중첩에 있을 수 있으므로
    재귀적으로 문자열 값을 수집한다.
    """
    texts: list[str] = []

    def _walk(obj, depth: int = 0):
        if depth > max_depth:
            return
        if isinstance(obj, str) and len(obj) > 2:
            texts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(data)
    return "\n".join(texts)


# ─── 패턴 감지 함수 ──────────────────────────────────
_PRICE_RE = re.compile(
    r'(?:SAR|SR|ر\.س)\s*\d+|'
    r'\d+\.?\d*\s*(?:SAR|SR)|'
    r'(?:price|Price|PRICE)',
    re.IGNORECASE,
)

_PRODUCT_KEYWORDS = re.compile(
    r'(?:tablet|capsule|syrup|cream|injection|'
    r'mg\b|ml\b|paracetamol|ibuprofen|'
    r'medicine|pharmaceutical|drug|'
    r'vitamin|supplement)',
    re.IGNORECASE,
)


def _detect_prices(text: str) -> bool:
    return bool(_PRICE_RE.search(text))


def _detect_products(text: str) -> bool:
    matches = _PRODUCT_KEYWORDS.findall(text)
    return len(matches) >= 2


def _detect_arabic(text: str) -> bool:
    return any('\u0600' <= c <= '\u06FF' for c in text)


# ─── 메인 추출 함수 ──────────────────────────────────
def extract_with_trafilatura(
    html: str,
    *,
    source_name: str = "unknown",
    favor_recall: bool = True,
) -> ExtractionResult:
    """HTML에서 본문을 추출한다. Triple Fallback 3차 진입점.

    호출 조건 (caller가 보장):
    - 1차(하드코딩 CSS 셀렉터) 실패
    - 2차(동적 셀렉터 캐시) 실패 또는 캐시 미스

    추출 순서:
    1. 구조화 데이터 JSON 파싱 시도 (__NEXT_DATA__ 등)
    2. JSON 없으면 Trafilatura 추출
    3. 결과 분석 (가격/상품명/아랍어 감지)

    Args:
        html: 이미 fetch된 HTML 문자열 (httpx 등으로 받은 것)
        source_name: 로깅용 소스 이름
        favor_recall: True면 재현율 우선 (놓치는 것보다 많이 잡음)

    Returns:
        ExtractionResult
    """
    if not html or len(html) < 100:
        logger.warning("[%s] HTML이 너무 짧음 (%d자)", source_name, len(html) if html else 0)
        return ExtractionResult(success=False, method="none")

    # ── Step 1: 구조화 데이터 JSON 파싱 (2.5차) ──
    json_result = _try_extract_json_state(html)
    if json_result:
        data, method = json_result
        flat_text = _flatten_json_text(data)
        if len(flat_text) >= 50:
            logger.info(
                "[%s] 구조화 데이터(%s) 추출 성공: %d자",
                source_name, method, len(flat_text),
            )
            return ExtractionResult(
                success=True,
                text=flat_text,
                text_length=len(flat_text),
                method=method,
                metadata={"keys_sample": list(data.keys())[:10]},
                has_prices=_detect_prices(flat_text),
                has_products=_detect_products(flat_text),
                has_arabic=_detect_arabic(flat_text),
            )
        logger.debug("[%s] %s 발견했으나 텍스트 부족 (%d자)", source_name, method, len(flat_text))

    # ── Step 2: Trafilatura 본문 추출 (3차) ──
    try:
        extracted = trafilatura.extract(
            html,
            config=_traf_config,
            include_links=True,
            include_tables=True,
            include_comments=False,
            favor_recall=favor_recall,
            output_format="txt",
        )
    except Exception as e:
        logger.error("[%s] Trafilatura 예외: %s", source_name, e)
        return ExtractionResult(success=False, method="trafilatura_error")

    if not extracted or len(extracted) < 50:
        logger.info(
            "[%s] Trafilatura 추출 실패 또는 부족 (%d자). HTML 크기: %d자",
            source_name,
            len(extracted) if extracted else 0,
            len(html),
        )
        return ExtractionResult(
            success=False,
            text=extracted,
            text_length=len(extracted) if extracted else 0,
            method="trafilatura",
        )

    # ── Step 3: 메타데이터 추출 ──
    meta = None
    try:
        meta_json = trafilatura.extract(
            html,
            config=_traf_config,
            output_format="json",
            include_links=True,
            include_tables=True,
            favor_recall=favor_recall,
        )
        if meta_json:
            meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
    except Exception:
        pass

    logger.info(
        "[%s] Trafilatura 추출 성공: %d자",
        source_name, len(extracted),
    )

    return ExtractionResult(
        success=True,
        text=extracted,
        text_length=len(extracted),
        method="trafilatura",
        metadata=meta,
        has_prices=_detect_prices(extracted),
        has_products=_detect_products(extracted),
        has_arabic=_detect_arabic(extracted),
    )


# ─── 편의 함수: 소매 크롤러 통합용 ──────────────────
def extract_or_none(
    html: str,
    *,
    source_name: str = "unknown",
    min_length: int = 100,
) -> Optional[str]:
    """단순 인터페이스: 본문 텍스트 또는 None.

    기존 크롤러 코드에서 최소한의 수정으로 통합할 때 사용.

    Usage:
        text = extract_or_none(html, source_name="nahdi_web")
        if text:
            # Claude Haiku에게 구조화 요청
            structured = call_haiku(text)
    """
    result = extract_with_trafilatura(html, source_name=source_name)
    if result.success and result.text_length >= min_length:
        return result.text
    return None


# ─── 자가 테스트 ──────────────────────────────────────
if __name__ == "__main__":
    # 1. 빈 HTML
    r = extract_with_trafilatura("", source_name="test_empty")
    assert not r.success
    assert r.method == "none"

    # 2. 정적 HTML (Trafilatura 성공 케이스)
    sample_html = """
    <html><head><title>Drug List</title></head>
    <body>
    <nav><a href="/">Home</a><a href="/about">About</a></nav>
    <article>
        <h1>Registered Drugs in Saudi Arabia</h1>
        <p>The following drugs are registered with SFDA for distribution.
        Paracetamol 500mg tablets are available at SAR 12.50 per pack.
        Ibuprofen 400mg capsules retail for SAR 18.00.
        Amoxicillin 250mg syrup is priced at SAR 25.75.</p>
        <p>All prices include 15% VAT as per Saudi regulations.
        These pharmaceutical products undergo rigorous quality checks
        before being approved for the Saudi market.</p>
        <table>
            <tr><th>Drug</th><th>Strength</th><th>Price SAR</th></tr>
            <tr><td>Paracetamol</td><td>500 mg</td><td>12.50</td></tr>
            <tr><td>Ibuprofen</td><td>400 mg</td><td>18.00</td></tr>
        </table>
    </article>
    <footer>Copyright 2024 SFDA</footer>
    </body></html>
    """
    r = extract_with_trafilatura(sample_html, source_name="test_static")
    assert r.success
    assert r.method == "trafilatura"
    assert r.has_prices
    assert r.has_products
    assert r.text_length > 50

    # 3. __NEXT_DATA__ JSON (SPA 케이스 — nahdi_web 시뮬)
    spa_html = """
    <html><head><title>Nahdi</title></head>
    <body><div id="__next"></div>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"products":[
        {"name":"Panadol Extra 500mg Tablet","price":12.5,"category":"medicine"},
        {"name":"Vitamin D3 1000IU Capsule","price":35.0,"category":"supplement"}
    ]}},"page":"/"}
    </script>
    </body></html>
    """
    r = extract_with_trafilatura(spa_html, source_name="test_spa")
    assert r.success
    assert r.method == "next_data"
    assert r.has_products
    # JSON 경로의 confidence penalty가 더 낮음 (구조화 데이터라 신뢰도 높음)
    assert r.confidence_penalty == -0.03

    # 4. extract_or_none 편의 함수
    text = extract_or_none(sample_html, source_name="test_convenience")
    assert text is not None
    assert len(text) > 50

    none_text = extract_or_none("<html><body></body></html>", source_name="test_none")
    assert none_text is None

    print("trafilatura_fallback self-tests passed")
