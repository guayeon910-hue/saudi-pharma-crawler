"""
html_preprocessor.py — HTML → LLM 프롬프트용 정제 스니펫 변환

논문1 "HTML 문서에서 Task-Relevant Snippet 추출 및 압축 알고리즘" 구현.

6단계 파이프라인:
  1. HTML 파싱 (BeautifulSoup)
  2. 노이즈 태그 제거 (script, style, nav, footer, 숨김 요소)
  3. DOM 구조 단순화 (불필요 속성 제거, 태그 정규화, 트리 깊이 제한)
  4. 텍스트 블록 추출 및 밀도 계산
  5. 텍스트 밀도 기반 상위 N개 스니펫 선택
  6. 구조화 토큰 변환 → LLM 프롬프트 구성

통합 지점:
  - source_discoverer.py: 발견된 URL의 HTML → 정제 → LLM 판별
  - auto_scraper.py: 크롤링 대상 HTML → 정제 → XPath 생성용 컨텍스트
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup, Comment, Tag, NavigableString

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수: 노이즈 태그 / 속성 목록 (논문 §1)
# ---------------------------------------------------------------------------

# 기능적 태그 — 콘텐츠 없음
NOISE_TAGS_FUNCTIONAL = {"script", "style", "noscript", "iframe", "form", "button", "input", "select", "textarea"}
# 구조적 태그 — 네비게이션/헤더/푸터
NOISE_TAGS_STRUCTURAL = {"nav", "header", "footer", "aside"}
# 멀티미디어 태그
NOISE_TAGS_MEDIA = {"svg", "img", "video", "audio", "canvas", "map", "area", "picture", "source"}
# 메타데이터 태그
NOISE_TAGS_META = {"meta", "link"}

ALL_NOISE_TAGS = NOISE_TAGS_FUNCTIONAL | NOISE_TAGS_STRUCTURAL | NOISE_TAGS_MEDIA | NOISE_TAGS_META

# 불필요 속성 (논문 §3)
REMOVE_ATTRS = {"class", "id", "style", "data-*", "onclick", "onload", "onmouseover",
                "role", "tabindex", "aria-label", "aria-describedby"}

# 인라인 태그 정규화 대상 (논문 §3)
INLINE_NORMALIZE = {"b", "strong", "i", "em", "u", "mark", "small", "sub", "sup", "span"}

# 블록 태그 (텍스트 밀도 계산 단위)
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th",
              "div", "section", "article", "blockquote", "pre", "tr", "dt", "dd"}

# 구조화 토큰 매핑 (논문 §6)
STRUCTURE_TOKENS = {
    "h1": "TITLE",
    "h2": "HEADING",
    "h3": "SUBHEADING",
    "h4": "SUBHEADING",
    "h5": "SUBHEADING",
    "h6": "SUBHEADING",
    "p": "PARAGRAPH",
    "li": "LIST_ITEM",
    "td": "CELL",
    "th": "HEADER_CELL",
    "tr": "ROW",
    "table": "TABLE",
    "a": "LINK",
}

# ---------------------------------------------------------------------------
# 텍스트 블록 데이터
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    """텍스트 밀도 분석 단위."""
    tag: str
    text: str
    text_length: int = 0
    total_length: int = 0  # text + tag/속성 포함 전체
    link_count: int = 0
    word_count: int = 0
    density: float = 0.0   # text_length / total_length
    depth: int = 0

    def __post_init__(self):
        self.text_length = len(self.text)
        self.word_count = len(self.text.split())
        if self.total_length > 0:
            self.density = self.text_length / self.total_length
        else:
            self.density = 0.0


@dataclass
class PreprocessResult:
    """전처리 결과."""
    snippets: list[TextBlock]
    prompt_text: str           # LLM에 보낼 최종 텍스트
    total_blocks: int          # 전처리 전 블록 수
    selected_blocks: int       # 선택된 블록 수
    original_html_size: int
    processed_text_size: int
    has_table: bool = False
    has_price_pattern: bool = False
    has_product_pattern: bool = False


# ---------------------------------------------------------------------------
# Stage 1: HTML 파싱
# ---------------------------------------------------------------------------

def _parse_html(html: str) -> BeautifulSoup:
    """HTML → BeautifulSoup DOM."""
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Stage 2: 노이즈 태그 제거
# ---------------------------------------------------------------------------

def _remove_noise(soup: BeautifulSoup) -> BeautifulSoup:
    """노이즈 태그 + 숨김 요소 + 주석 제거."""
    # 주석 제거
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # 노이즈 태그 제거
    for tag in soup.find_all(ALL_NOISE_TAGS):
        tag.decompose()

    # display:none, visibility:hidden 제거 (논문 §1 속성 기반 제거)
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)):
        tag.decompose()

    # aria-hidden="true" 제거
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()

    return soup


# ---------------------------------------------------------------------------
# Stage 3: DOM 구조 단순화
# ---------------------------------------------------------------------------

def _simplify_dom(soup: BeautifulSoup, max_depth: int = 8) -> BeautifulSoup:
    """불필요 속성 제거, 태그 정규화, 트리 깊이 제한."""

    # 3-1. 불필요 속성 제거
    for tag in soup.find_all(True):
        attrs_to_remove = []
        for attr in list(tag.attrs.keys()):
            if attr in REMOVE_ATTRS or attr.startswith("data-"):
                attrs_to_remove.append(attr)
        for attr in attrs_to_remove:
            del tag[attr]

    # 3-2. 인라인 태그 정규화 — unwrap (텍스트만 남기기)
    for tag_name in INLINE_NORMALIZE:
        for tag in soup.find_all(tag_name):
            tag.unwrap()

    # 3-3. 단일 자식 div 제거 (자식으로 대체)
    changed = True
    while changed:
        changed = False
        for div in soup.find_all("div"):
            children = [c for c in div.children if isinstance(c, Tag)]
            if len(children) == 1 and not div.string:
                div.replace_with(children[0])
                changed = True

    # 3-4. 빈 요소 제거
    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and tag.name not in {"br", "hr"}:
            tag.decompose()

    # 3-5. 트리 깊이 제한
    _limit_depth(soup, max_depth)

    return soup


def _limit_depth(element, max_depth: int, current_depth: int = 0):
    """max_depth 이상 깊은 노드를 텍스트로 평탄화."""
    if not isinstance(element, Tag):
        return
    if current_depth >= max_depth:
        text = element.get_text(separator=" ", strip=True)
        if text:
            element.replace_with(NavigableString(text))
        else:
            element.decompose()
        return
    for child in list(element.children):
        _limit_depth(child, max_depth, current_depth + 1)


# ---------------------------------------------------------------------------
# Stage 4: 텍스트 블록 추출 + 밀도 계산
# ---------------------------------------------------------------------------

def _extract_blocks(soup: BeautifulSoup) -> list[TextBlock]:
    """블록 레벨 태그에서 텍스트 블록 추출 + 밀도 계산."""
    blocks: list[TextBlock] = []

    for tag in soup.find_all(BLOCK_TAGS):
        text = tag.get_text(separator=" ", strip=True)
        if not text:
            continue

        # 전체 길이 = 태그 포함 HTML
        total_html = str(tag)
        link_count = len(tag.find_all("a"))

        # 깊이 계산
        depth = 0
        parent = tag.parent
        while parent:
            depth += 1
            parent = parent.parent

        block = TextBlock(
            tag=tag.name,
            text=text,
            total_length=len(total_html),
            link_count=link_count,
            depth=depth,
        )
        blocks.append(block)

    return blocks


# ---------------------------------------------------------------------------
# Stage 5: 텍스트 밀도 기반 스니펫 선택
# ---------------------------------------------------------------------------

_MIN_WORDS = 5          # 최소 단어 수 (논문 §2 휴리스틱: 10단어 미만 필터링 → 약간 완화)
_MAX_LINK_RATIO = 0.7   # 링크 텍스트 비율 상한 (핵심 콘텐츠는 링크 밀도 낮음)


def _select_snippets(
    blocks: list[TextBlock],
    top_n: int = 30,
    min_density: float = 0.3,
) -> list[TextBlock]:
    """밀도 높은 상위 N개 블록 선택.

    선택 기준 (논문 §2):
    1. 최소 단어 수 이상
    2. 텍스트 밀도 임계값 이상
    3. 링크 비율 상한 이하
    4. 밀도 내림차순 정렬 → 상위 N개
    5. 인접 블록 병합 (원문 순서 유지)
    """
    filtered = []
    for b in blocks:
        # 짧은 블록 필터
        if b.word_count < _MIN_WORDS:
            continue
        # 밀도 임계값
        if b.density < min_density:
            continue
        # 링크 밀도 — 네비게이션 등 제거
        if b.link_count > 0 and b.word_count > 0:
            link_text_len = sum(len(a.get_text()) for a in [] )  # 근사값으로 link_count 사용
            if b.link_count / max(b.word_count, 1) > _MAX_LINK_RATIO:
                continue
        filtered.append(b)

    # 밀도 내림차순 정렬
    filtered.sort(key=lambda b: b.density, reverse=True)

    # 상위 N개 선택
    selected = filtered[:top_n]

    return selected


# ---------------------------------------------------------------------------
# Stage 6: 구조화 토큰 변환 → LLM 프롬프트
# ---------------------------------------------------------------------------

def _to_prompt(blocks: list[TextBlock], max_chars: int = 6000) -> str:
    """선택된 블록을 구조화 토큰 형식으로 변환.

    논문 §6:
      <h1>제목 → [TITLE] 제목 [/TITLE]
      <p>단락  → [PARAGRAPH] 단락 [/PARAGRAPH]
      <li>항목 → [LIST_ITEM] 항목 [/LIST_ITEM]
      <td>셀  → [CELL] 셀 [/CELL]

    max_chars로 LLM 컨텍스트 제한.
    """
    lines: list[str] = []
    total = 0

    for i, block in enumerate(blocks):
        token = STRUCTURE_TOKENS.get(block.tag, "TEXT")
        line = f"[{token}] {block.text} [/{token}]"

        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1  # +1 for newline

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 패턴 감지 (trafilatura_fallback.py와 동일 로직 재사용)
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(
    r'(?:SAR|SR|ر\.س)\s*\d+|'
    r'\d+\.?\d*\s*(?:SAR|SR)|'
    r'(?:price|Price|سعر)',
    re.IGNORECASE,
)

_PRODUCT_RE = re.compile(
    r'(?:tablet|capsule|syrup|cream|injection|inhaler|'
    r'mg\b|ml\b|mcg\b|'
    r'medicine|pharmaceutical|drug|'
    r'vitamin|supplement|دواء|صيدلية)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 메인 API
# ---------------------------------------------------------------------------

def preprocess_html(
    html: str,
    *,
    top_n: int = 30,
    max_chars: int = 6000,
    max_depth: int = 8,
    min_density: float = 0.3,
) -> PreprocessResult:
    """HTML → LLM 프롬프트용 정제 스니펫 변환.

    Parameters
    ----------
    html : str
        원본 HTML.
    top_n : int
        선택할 최대 블록 수.
    max_chars : int
        LLM 프롬프트 최대 문자 수.
    max_depth : int
        DOM 트리 최대 깊이.
    min_density : float
        텍스트 밀도 최소 임계값.

    Returns
    -------
    PreprocessResult
    """
    original_size = len(html)

    # Stage 1: 파싱
    soup = _parse_html(html)

    # Stage 2: 노이즈 제거
    soup = _remove_noise(soup)

    # Stage 3: DOM 단순화
    soup = _simplify_dom(soup, max_depth=max_depth)

    # Stage 4: 블록 추출
    blocks = _extract_blocks(soup)

    # Stage 5: 스니펫 선택
    selected = _select_snippets(blocks, top_n=top_n, min_density=min_density)

    # Stage 6: 프롬프트 구성
    prompt_text = _to_prompt(selected, max_chars=max_chars)

    # 패턴 감지
    full_text = " ".join(b.text for b in selected)
    has_table = any(b.tag in ("td", "th", "tr") for b in selected)

    return PreprocessResult(
        snippets=selected,
        prompt_text=prompt_text,
        total_blocks=len(blocks),
        selected_blocks=len(selected),
        original_html_size=original_size,
        processed_text_size=len(prompt_text),
        has_table=has_table,
        has_price_pattern=bool(_PRICE_RE.search(full_text)),
        has_product_pattern=bool(_PRODUCT_RE.search(full_text)),
    )


def preprocess_for_scraper(
    html: str,
    *,
    max_chars: int = 4000,
    max_depth: int = 6,
) -> str:
    """auto_scraper.py용 간소화 버전.

    XPath 생성에 필요한 구조 정보를 보존하면서 크기를 줄인다.
    구조화 토큰 대신 정제된 HTML 자체를 반환.
    """
    soup = _parse_html(html)
    soup = _remove_noise(soup)
    soup = _simplify_dom(soup, max_depth=max_depth)

    clean_html = str(soup)
    if len(clean_html) > max_chars:
        clean_html = clean_html[:max_chars] + "\n<!-- truncated -->"

    return clean_html
