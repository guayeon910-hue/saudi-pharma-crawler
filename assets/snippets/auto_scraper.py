"""
auto_scraper.py — AUTOSCRAPER: LLM 기반 자동 스크래퍼 생성

논문2 "AUTOSCRAPER: Progressive Understanding Web Agent for Web Scraper Generation" 구현.

2-Phase 프레임워크:
  Phase 1 — Progressive Generation:
    1. HTML → 전처리 (html_preprocessor.py)
    2. LLM이 DOM 트리 top-down 탐색 → XPath 생성
    3. XPath 실행 → 추출값 검증
    4. 실패 시 step-back (부모 노드로 이동, 재시도)
    5. 성공한 XPath를 Action Sequence에 추가

  Phase 2 — Synthesis:
    같은 사이트의 여러 페이지에서 생성된 XPath들을 통합하여
    범용 스크래퍼 합성.

실행 환경: GitHub Actions 전용

통합 지점:
  - ai_search.py: 발견된 유효 소스에 대해 자동 스크래퍼 생성
  - llm_client.py: Claude API
  - html_preprocessor.py: HTML 전처리
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from lxml import html as lxml_html
from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# XPath 패턴 캐시 (핀포인트 크롤링: 도메인 재방문 시 LLM 호출 생략)
# ---------------------------------------------------------------------------

_XPATH_CACHE_PATH = Path(__file__).resolve().parents[2] / "reports" / "cache" / "xpath_patterns.json"
_XPATH_CACHE_SCHEMA_VERSION = 2  # v2 uses stricter field validators; old entries are regenerated.
_XPATH_CACHE_MAX_FAILS = 3    # 연속 실패 횟수 상한 — 초과 시 재생성
_XPATH_CACHE_MAX_ENTRIES = 300  # 캐시 최대 항목 수 (도메인+경로 단위)
_XPATH_CACHE_LOCK = threading.Lock()  # 동시 read-modify-write 방지


def _load_xpath_cache() -> dict:
    """XPath 패턴 캐시 로드. 실패 시 빈 dict."""
    try:
        if _XPATH_CACHE_PATH.exists():
            with open(_XPATH_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError) as e:
        logger.debug("xpath cache load 실패 (무시): %s", e)
    return {}


def _save_xpath_cache(cache: dict) -> None:
    """XPath 패턴 캐시 저장. 원자적 write + 크기 상한 적용."""
    try:
        # 크기 상한: verified_at 오래된 순으로 초과분 제거
        if len(cache) > _XPATH_CACHE_MAX_ENTRIES:
            def _newest_ts(entry_dict: dict) -> float:
                # 각 캐시 키 값은 {field_name: {verified_at, ...}} 형태
                ts_values = [
                    v.get("verified_at", 0.0)
                    for v in entry_dict.values()
                    if isinstance(v, dict)
                ]
                return max(ts_values, default=0.0)

            sorted_keys = sorted(cache, key=lambda k: _newest_ts(cache[k]))
            for k in sorted_keys[: len(cache) - _XPATH_CACHE_MAX_ENTRIES]:
                del cache[k]

        _XPATH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _XPATH_CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _XPATH_CACHE_PATH)  # 원자적 교체 (Windows 포함)
    except (OSError, ValueError) as e:
        logger.debug("xpath cache save 실패 (무시): %s", e)

# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class XPathAction:
    """하나의 XPath 추출 액션."""
    field_name: str         # product_name, price, manufacturer, ...
    xpath: str = ""         # XPath 표현식
    sample_value: str = ""  # 추출된 샘플 값
    verified: bool = False  # 검증 통과 여부
    attempts: int = 0       # 시도 횟수


@dataclass
class ActionSequence:
    """사이트 1개에 대한 XPath 추출 액션 시퀀스."""
    url: str
    domain: str
    actions: list[XPathAction] = field(default_factory=list)
    page_title: str = ""
    success_rate: float = 0.0  # 검증 통과 비율

    @property
    def is_usable(self) -> bool:
        """최소 2개 필드 추출 성공 시 사용 가능."""
        verified = [a for a in self.actions if a.verified]
        return len(verified) >= 2

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "domain": self.domain,
            "page_title": self.page_title,
            "actions": [
                {"field": a.field_name, "xpath": a.xpath,
                 "sample": a.sample_value, "verified": a.verified}
                for a in self.actions
            ],
            "success_rate": self.success_rate,
            "usable": self.is_usable,
        }


@dataclass
class SynthesizedScraper:
    """합성된 범용 스크래퍼."""
    domain: str
    field_xpaths: dict[str, str]       # field_name → 최종 XPath
    confidence: float = 0.0
    source_pages: int = 0              # 합성에 사용된 페이지 수
    sample_data: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "field_xpaths": self.field_xpaths,
            "confidence": round(self.confidence, 2),
            "source_pages": self.source_pages,
            "sample_data": self.sample_data[:3],
        }


# ---------------------------------------------------------------------------
# 추출 대상 필드 정의 (의약품 도메인 특화)
# ---------------------------------------------------------------------------

_PRICE_TOKEN_RE = re.compile(r"(?i)(?:sar|sr|riyal|ر\.س|﷼|usd|\$|€|£)")
_DOSAGE_UNIT_RE = re.compile(
    r"(?i)\b\d+(?:[.,]\d+)?\s*(?:mg|mcg|µg|ug|g|ml|iu|unit|units|%)\b"
)
_RATIO_STRENGTH_RE = re.compile(
    r"(?i)\b\d+(?:[.,]\d+)?\s*/\s*\d+(?:[.,]\d+)?(?:\s*(?:mg|mcg|µg|ug|g|ml|iu|%))?\b"
)


def _clean_candidate(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _has_alpha(value: str) -> bool:
    return bool(re.search(r"[A-Za-z\u0600-\u06FF가-힣]", value))


def _looks_like_price(value: str) -> bool:
    s = _clean_candidate(value)
    if not s:
        return False
    has_number = bool(re.search(r"\d+(?:[.,]\d+)?", s))
    if not has_number:
        return False
    if _PRICE_TOKEN_RE.search(s):
        return True
    # Plain numeric decimals are often rendered without currency in ecommerce UIs.
    if re.fullmatch(r"\d{1,5}(?:[.,]\d{1,2})?", s):
        return True
    return False


def _looks_like_strength(value: str) -> bool:
    s = _clean_candidate(value)
    if not s or len(s) > 80:
        return False
    if _looks_like_price(s):
        return False
    if _DOSAGE_UNIT_RE.search(s) or _RATIO_STRENGTH_RE.search(s):
        # Product titles often include a strength; avoid learning title nodes as strength.
        noisy_terms = ("drug product", "tablet", "capsule", "inhaler", "pharma co", "company")
        return not any(term in s.lower() for term in noisy_terms)
    return False


def _valid_product_name(value: Any) -> bool:
    s = _clean_candidate(value)
    if not (3 <= len(s) <= 200) or not _has_alpha(s):
        return False
    lower = s.lower()
    if "company" in lower or "pharma co" in lower:
        return False
    return not _looks_like_price(s)


def _valid_price(value: Any) -> bool:
    s = _clean_candidate(value)
    if not _looks_like_price(s):
        return False
    return not (_looks_like_strength(s) and not _PRICE_TOKEN_RE.search(s))


def _valid_manufacturer(value: Any) -> bool:
    s = _clean_candidate(value)
    if len(s) < 2 or not _has_alpha(s):
        return False
    if _looks_like_price(s) or _looks_like_strength(s):
        return False
    return True


def _valid_active_ingredient(value: Any) -> bool:
    s = _clean_candidate(value)
    if len(s) < 3 or not _has_alpha(s):
        return False
    if _looks_like_price(s):
        return False
    lower = s.lower()
    if any(term in lower for term in ("drug product", "tablet", "capsule", "inhaler", "pharma co", "company")):
        return False
    return True


def _valid_strength(value: Any) -> bool:
    return _looks_like_strength(_clean_candidate(value))


PHARMA_FIELDS = [
    {
        "name": "product_name",
        "description": "The product or drug name",
        "validation": _valid_product_name,
    },
    {
        "name": "price",
        "description": "The product price (numeric value, may include currency symbol like SAR)",
        "validation": _valid_price,
    },
    {
        "name": "manufacturer",
        "description": "The manufacturer or pharmaceutical company name",
        "validation": _valid_manufacturer,
    },
    {
        "name": "active_ingredient",
        "description": "The active pharmaceutical ingredient (API/INN name)",
        "validation": _valid_active_ingredient,
    },
    {
        "name": "strength",
        "description": "The drug strength/dosage (e.g., 500mg, 10ml)",
        "validation": _valid_strength,
    },
]


# ---------------------------------------------------------------------------
# Phase 1: Progressive Generation
# ---------------------------------------------------------------------------

XPATH_SYSTEM_PROMPT = """You are an expert web scraper engineer.
Given a preprocessed HTML snippet, generate an XPath expression to extract a specific data field.

Rules:
1. Return ONLY a JSON object with "xpath" and "expected_value" fields
2. The XPath must be specific enough to target the right element
3. Use relative XPaths starting with // when possible
4. Prefer class-based or structural selectors over absolute paths
5. The expected_value should be what you expect to find at that XPath

Example response:
{"xpath": "//div[@class='product-info']//span[@class='price']/text()", "expected_value": "SAR 12.50"}"""

XPATH_USER_TEMPLATE = """From this HTML snippet, write an XPath to extract: {field_description}

HTML:
---
{html_snippet}
---

Return JSON: {{"xpath": "...", "expected_value": "..."}}"""

# step-back 시 부모 컨텍스트 요청
STEPBACK_USER_TEMPLATE = """The previous XPath failed to extract the target field.
Previous XPath: {prev_xpath}
Error: {error}

Here is a WIDER context (parent node HTML):
---
{parent_html}
---

Generate a NEW XPath to extract: {field_description}
Return JSON: {{"xpath": "...", "expected_value": "..."}}"""

MAX_STEPBACK = 2  # 최대 step-back 횟수 (핀포인트: 3→2로 축소, 호출 1회/필드 절감)


def _execute_xpath(tree: etree._Element, xpath: str) -> list[str]:
    """XPath 실행 → 문자열 리스트 반환."""
    try:
        results = tree.xpath(xpath)
        texts = []
        for r in results:
            if isinstance(r, str):
                texts.append(r.strip())
            elif hasattr(r, "text") and r.text:
                texts.append(r.text.strip())
            elif hasattr(r, "text_content"):
                texts.append(r.text_content().strip())
        return [t for t in texts if t]
    except Exception as e:
        logger.debug("XPath 실행 실패 '%s': %s", xpath, e)
        return []


def _get_parent_html(tree: etree._Element, xpath: str, level: int = 1) -> str:
    """XPath의 부모 노드 HTML 반환 (step-back용).

    XPath에서 상위 노드를 찾지 못하면 body 전체를 반환.
    """
    try:
        # 부모 경로 생성: 끝에서 /segment를 level번 제거
        parent_xpath = xpath
        for _ in range(level):
            if "/" in parent_xpath:
                parent_xpath = parent_xpath.rsplit("/", 1)[0]
            else:
                parent_xpath = ""
                break
        if not parent_xpath or parent_xpath in ("/", "//"):
            parent_xpath = "//body"

        parents = tree.xpath(parent_xpath)
        if parents:
            parent = parents[0]
            html_bytes = etree.tostring(parent, encoding="unicode", method="html")
            return html_bytes[:3000] if len(html_bytes) > 3000 else html_bytes

        # XPath로 부모 못 찾으면 body fallback
        bodies = tree.xpath("//body")
        if bodies:
            html_bytes = etree.tostring(bodies[0], encoding="unicode", method="html")
            return html_bytes[:3000]
    except Exception:
        pass
    return ""


def generate_xpath_for_field(
    llm_client,
    html_snippet: str,
    tree: etree._Element,
    field_def: dict,
) -> XPathAction:
    """LLM으로 1개 필드의 XPath 생성 + 검증 (top-down + step-back).

    Parameters
    ----------
    llm_client : ClaudeClient
    html_snippet : str  전처리된 HTML 스니펫
    tree : lxml Element  파싱된 DOM 트리
    field_def : dict  {name, description, validation}

    Returns
    -------
    XPathAction
    """
    action = XPathAction(field_name=field_def["name"])
    validation_fn = field_def["validation"]

    # Top-down: 첫 시도 (핀포인트: 3000→1500자로 축소)
    prompt = XPATH_USER_TEMPLATE.format(
        field_description=field_def["description"],
        html_snippet=html_snippet[:1500],
    )

    try:
        result = llm_client.ask_json(prompt, system=XPATH_SYSTEM_PROMPT, max_tokens=256)
        xpath = result.get("xpath", "")
        action.xpath = xpath
        action.attempts = 1

        # XPath 실행 + 검증
        values = _execute_xpath(tree, xpath)
        if values and validation_fn(values[0]):
            action.sample_value = values[0]
            action.verified = True
            return action

        # Step-back 반복 (핀포인트: 적응형 크기 + 이전 시도 추적)
        prev_xpath = xpath
        prev_xpaths: list[str] = [xpath] if xpath else []
        error = f"No valid value found (got: {values[:3] if values else 'empty'})"

        for step in range(MAX_STEPBACK):
            # 적응형 parent_html 크기: 2000 → 1000 → 500 (단계 깊어질수록 축소)
            size = max(2000 >> step, 400)
            parent_html = _get_parent_html(tree, prev_xpath, level=step + 1)[:size]
            if not parent_html:
                break

            tried_list = "\n".join(f"  - {x}" for x in prev_xpaths[-3:])
            stepback_prompt = STEPBACK_USER_TEMPLATE.format(
                prev_xpath=prev_xpath,
                error=f"{error}\nPreviously tried:\n{tried_list}",
                parent_html=parent_html,
                field_description=field_def["description"],
            )

            result = llm_client.ask_json(stepback_prompt, system=XPATH_SYSTEM_PROMPT, max_tokens=256)
            new_xpath = result.get("xpath", "") if result is not None else ""
            action.xpath = new_xpath
            action.attempts += 1

            values = _execute_xpath(tree, new_xpath)
            if values and validation_fn(values[0]):
                action.sample_value = values[0]
                action.verified = True
                return action

            prev_xpath = new_xpath
            if new_xpath:
                prev_xpaths.append(new_xpath)
            error = f"Step-back {step+1}: still no valid value (got: {values[:3] if values else 'empty'})"

    except Exception as e:
        logger.error("XPath 생성 실패 (%s): %s", field_def["name"], e)

    return action


def generate_action_sequence(
    llm_client,
    url: str,
    html: str,
    *,
    fields: Optional[list[dict]] = None,
) -> ActionSequence:
    """1페이지에 대한 Action Sequence 생성.

    Parameters
    ----------
    llm_client : ClaudeClient
    url : str
    html : str  원본 HTML
    fields : list[dict] | None  추출 필드 정의 (기본: PHARMA_FIELDS)

    Returns
    -------
    ActionSequence
    """
    from assets.snippets.html_preprocessor import preprocess_for_scraper
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc
    # [B2] URL 경로 첫 세그먼트로 캐시 키 세분화 (같은 도메인의 다른 페이지 템플릿 구분)
    path_prefix = parsed.path.split("/")[1] if parsed.path.count("/") >= 1 else ""
    domain_cache_key = f"{domain}:{path_prefix}"
    fields = fields or PHARMA_FIELDS

    # HTML 전처리 (auto_scraper용)
    clean_html = preprocess_for_scraper(html, max_chars=4000)

    # lxml 파싱
    try:
        tree = lxml_html.fromstring(html)
    except Exception as e:
        logger.error("HTML 파싱 실패 %s: %s", url, e)
        return ActionSequence(url=url, domain=domain)

    # 페이지 타이틀
    title = ""
    title_els = tree.xpath("//title/text()")
    if title_els:
        title = str(title_els[0]).strip()

    seq = ActionSequence(url=url, domain=domain, page_title=title)

    # [B3/B4] Lock 획득 후 캐시 read-modify-write 전체를 원자적으로 처리
    with _XPATH_CACHE_LOCK:
        xpath_cache = _load_xpath_cache()
        domain_cache = xpath_cache.get(domain_cache_key, {})
        cache_hits = 0
        cache_dirty = False

        # 각 필드에 대해 XPath 생성 (캐시 우선, 실패 시 LLM 폴백)
        for field_def in fields:
            fname = field_def["name"]
            cached_entry = domain_cache.get(fname)
            action: Optional[XPathAction] = None

            # 1) 캐시된 XPath를 먼저 실제 HTML에 실행하여 검증
            if (
                cached_entry
                and cached_entry.get("xpath")
                and cached_entry.get("schema_version") == _XPATH_CACHE_SCHEMA_VERSION
                and cached_entry.get("fail_count", 0) < _XPATH_CACHE_MAX_FAILS
            ):
                cached_xpath = cached_entry["xpath"]
                values = _execute_xpath(tree, cached_xpath)
                if values and field_def["validation"](values[0]):
                    action = XPathAction(
                        field_name=fname,
                        xpath=cached_xpath,
                        sample_value=values[0],
                        verified=True,
                        attempts=0,  # LLM 호출 없음
                    )
                    cached_entry["success_count"] = int(cached_entry.get("success_count", 0)) + 1
                    cached_entry["fail_count"] = 0
                    cached_entry["verified_at"] = time.time()
                    cache_hits += 1
                    cache_dirty = True
                else:
                    # 캐시 hit이지만 검증 실패 → fail_count 증가 (3회되면 다음번에 재생성)
                    cached_entry["fail_count"] = int(cached_entry.get("fail_count", 0)) + 1
                    cache_dirty = True

            # 2) 캐시 미스 또는 캐시 실패 → LLM으로 생성
            if action is None:
                action = generate_xpath_for_field(llm_client, clean_html, tree, field_def)
                if action.verified and action.xpath:
                    domain_cache[fname] = {
                        "xpath": action.xpath,
                        "schema_version": _XPATH_CACHE_SCHEMA_VERSION,
                        "verified_at": time.time(),
                        "success_count": 1,
                        "fail_count": 0,
                    }
                    cache_dirty = True

            seq.actions.append(action)

        # 캐시 flush
        if cache_dirty:
            xpath_cache[domain_cache_key] = domain_cache
            _save_xpath_cache(xpath_cache)
        if cache_hits:
            logger.info("[%s] XPath 캐시 히트: %d/%d 필드", domain, cache_hits, len(fields))

    # 성공률 계산
    verified = [a for a in seq.actions if a.verified]
    seq.success_rate = len(verified) / len(seq.actions) if seq.actions else 0.0

    logger.info(
        "[%s] Action Sequence: %d/%d 필드 검증 (%.0f%%)",
        domain, len(verified), len(seq.actions), seq.success_rate * 100,
    )

    return seq


# ---------------------------------------------------------------------------
# Phase 2: Synthesis — 여러 페이지의 XPath 통합
# ---------------------------------------------------------------------------

def synthesize_scraper(
    sequences: list[ActionSequence],
) -> Optional[SynthesizedScraper]:
    """같은 도메인의 여러 ActionSequence를 통합하여 범용 스크래퍼 합성.

    논문 §Synthesis:
    - 같은 필드의 XPath들을 수집
    - 가장 많이 검증된 XPath를 선택 (투표 방식)
    - 신뢰도 = 검증 통과 비율

    Parameters
    ----------
    sequences : list[ActionSequence]
        같은 도메인의 ActionSequence 목록

    Returns
    -------
    SynthesizedScraper | None
    """
    if not sequences:
        return None

    domain = sequences[0].domain

    # 필드별 XPath 수집 (검증된 것만)
    field_votes: dict[str, dict[str, int]] = {}  # field → {xpath → count}

    for seq in sequences:
        for action in seq.actions:
            if action.verified:
                if action.field_name not in field_votes:
                    field_votes[action.field_name] = {}
                xpath = action.xpath
                field_votes[action.field_name][xpath] = field_votes[action.field_name].get(xpath, 0) + 1

    if not field_votes:
        return None

    # 각 필드에서 가장 많이 투표된 XPath 선택
    field_xpaths: dict[str, str] = {}
    for fname, votes in field_votes.items():
        best_xpath = max(votes, key=votes.get)
        field_xpaths[fname] = best_xpath

    # 신뢰도 계산
    total_fields = len(PHARMA_FIELDS)
    confidence = len(field_xpaths) / total_fields

    # 샘플 데이터 수집
    sample_data: list[dict] = []
    for seq in sequences:
        sample = {}
        for action in seq.actions:
            if action.verified and action.sample_value:
                sample[action.field_name] = action.sample_value
        if sample:
            sample_data.append(sample)

    scraper = SynthesizedScraper(
        domain=domain,
        field_xpaths=field_xpaths,
        confidence=confidence,
        source_pages=len(sequences),
        sample_data=sample_data,
    )

    logger.info(
        "[%s] 스크래퍼 합성: %d 필드, confidence=%.2f, %d 페이지 기반",
        domain, len(field_xpaths), confidence, len(sequences),
    )

    return scraper


# ---------------------------------------------------------------------------
# 스크래퍼 실행 — 합성된 XPath로 데이터 추출
# ---------------------------------------------------------------------------

def run_scraper(
    scraper: SynthesizedScraper,
    html: str,
) -> list[dict]:
    """합성된 스크래퍼로 HTML에서 데이터 추출.

    Parameters
    ----------
    scraper : SynthesizedScraper
    html : str  원본 HTML

    Returns
    -------
    list[dict]  추출된 레코드 리스트
    """
    try:
        tree = lxml_html.fromstring(html)
    except Exception as e:
        logger.error("HTML 파싱 실패: %s", e)
        return []

    # 각 필드별 값 추출
    field_values: dict[str, list[str]] = {}
    for fname, xpath in scraper.field_xpaths.items():
        values = _execute_xpath(tree, xpath)
        field_values[fname] = values

    # 레코드 구성 (가장 많은 값을 가진 필드 기준으로 행 수 결정)
    max_rows = max((len(v) for v in field_values.values()), default=0)
    if max_rows == 0:
        return []

    records: list[dict] = []
    for i in range(max_rows):
        record = {"_source_domain": scraper.domain}
        for fname, values in field_values.items():
            record[fname] = values[i] if i < len(values) else ""
        records.append(record)

    return records
