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
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from lxml import html as lxml_html
from lxml import etree

logger = logging.getLogger(__name__)

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

PHARMA_FIELDS = [
    {
        "name": "product_name",
        "description": "The product or drug name",
        "validation": lambda v: isinstance(v, str) and 3 <= len(v) <= 200,
    },
    {
        "name": "price",
        "description": "The product price (numeric value, may include currency symbol like SAR)",
        "validation": lambda v: bool(re.search(r"\d+\.?\d*", str(v))),
    },
    {
        "name": "manufacturer",
        "description": "The manufacturer or pharmaceutical company name",
        "validation": lambda v: isinstance(v, str) and len(v) >= 2,
    },
    {
        "name": "active_ingredient",
        "description": "The active pharmaceutical ingredient (API/INN name)",
        "validation": lambda v: isinstance(v, str) and len(v) >= 3,
    },
    {
        "name": "strength",
        "description": "The drug strength/dosage (e.g., 500mg, 10ml)",
        "validation": lambda v: bool(re.search(r"\d+", str(v))),
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

MAX_STEPBACK = 3  # 최대 step-back 횟수


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

    # Top-down: 첫 시도
    prompt = XPATH_USER_TEMPLATE.format(
        field_description=field_def["description"],
        html_snippet=html_snippet[:3000],
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

        # Step-back 반복
        prev_xpath = xpath
        error = f"No valid value found (got: {values[:3] if values else 'empty'})"

        for step in range(MAX_STEPBACK):
            parent_html = _get_parent_html(tree, prev_xpath, level=step + 1)
            if not parent_html:
                break

            stepback_prompt = STEPBACK_USER_TEMPLATE.format(
                prev_xpath=prev_xpath,
                error=error,
                parent_html=parent_html[:2000],
                field_description=field_def["description"],
            )

            result = llm_client.ask_json(stepback_prompt, system=XPATH_SYSTEM_PROMPT, max_tokens=256)
            new_xpath = result.get("xpath", "")
            action.xpath = new_xpath
            action.attempts += 1

            values = _execute_xpath(tree, new_xpath)
            if values and validation_fn(values[0]):
                action.sample_value = values[0]
                action.verified = True
                return action

            prev_xpath = new_xpath
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

    domain = urlparse(url).netloc
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

    # 각 필드에 대해 XPath 생성
    for field_def in fields:
        action = generate_xpath_for_field(llm_client, clean_html, tree, field_def)
        seq.actions.append(action)

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
