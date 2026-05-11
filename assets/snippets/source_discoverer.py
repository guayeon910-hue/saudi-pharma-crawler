"""
source_discoverer.py — AI 자율 소스 발견 (Phase A)

역할:
  1. 약품 정보 → LLM으로 검색 쿼리 생성
  2. 검색엔진 결과에서 URL 수집
  3. URL → HTML 가져오기 (antibot.py 재사용)
  4. HTML → 전처리 (html_preprocessor.py)
  5. 정제 스니펫 → LLM 사이트 판별 (의약품 관련? 사우디 시장? 신뢰도?)
  6. 유효 소스 목록 반환

통합 지점:
  - ai_search.py: 메인 오케스트레이터에서 호출
  - llm_client.py: Claude API 래퍼
  - html_preprocessor.py: HTML 정제
  - antibot.py: UA 회전 + anti-bot 탐지
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
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 도메인 평가 캐시 (핀포인트 크롤링: 재방문 시 LLM 호출 생략)
# ---------------------------------------------------------------------------

_DOMAIN_CACHE_PATH = Path(__file__).resolve().parents[2] / "reports" / "cache" / "domain_eval.json"
# [B6] max(1, ...) 으로 TTL=0 환경변수가 캐시를 완전 무력화하는 것 방지
_DOMAIN_CACHE_TTL_SEC = max(1, int(os.getenv("PINPOINT_CACHE_TTL_DAYS", "7"))) * 24 * 3600
_DOMAIN_CACHE_MAX_ENTRIES = 500  # 캐시 최대 항목 수 (domain::drug_key 단위)
_DOMAIN_CACHE_LOCK = threading.Lock()  # 동시 read-modify-write 방지


def _load_domain_cache() -> dict:
    """도메인 평가 캐시 로드. 실패 시 빈 dict."""
    try:
        if _DOMAIN_CACHE_PATH.exists():
            with open(_DOMAIN_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError) as e:
        logger.debug("domain cache load 실패 (무시): %s", e)
    return {}


def _save_domain_cache(cache: dict) -> None:
    """도메인 평가 캐시 저장. 원자적 write + 크기 상한 적용."""
    try:
        # [B8] 크기 상한: ts 오래된 순으로 초과분 제거
        if len(cache) > _DOMAIN_CACHE_MAX_ENTRIES:
            sorted_keys = sorted(
                cache,
                key=lambda k: float(cache[k].get("ts") or 0),
            )
            for k in sorted_keys[: len(cache) - _DOMAIN_CACHE_MAX_ENTRIES]:
                del cache[k]

        _DOMAIN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DOMAIN_CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DOMAIN_CACHE_PATH)  # 원자적 교체 (Windows 포함)
    except (OSError, ValueError) as e:
        logger.debug("domain cache save 실패 (무시): %s", e)

# ---------------------------------------------------------------------------
# 발견된 소스 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredSource:
    """AI가 발견한 소스."""
    url: str
    domain: str
    title: str = ""
    description: str = ""
    relevance_score: float = 0.0     # 0.0~1.0 LLM 판별 점수
    category: str = ""               # pharma_retailer | supplement_retailer | regulator | distributor | hospital | news | other
    has_price_data: bool = False
    has_product_listing: bool = False
    language: str = ""               # en | ar | mixed
    rejection_reason: str = ""       # 거부 시 사유
    search_query: str = ""           # 이 소스를 발견한 검색 쿼리

    @property
    def is_valid(self) -> bool:
        """유효 소스 판정 (임계값 0.6)."""
        return self.relevance_score >= 0.6 and not self.rejection_reason


# ---------------------------------------------------------------------------
# 제외 도메인 (이미 고정 10개에 포함되거나, 일반적 비관련 사이트)
# ---------------------------------------------------------------------------

# KSA/GCC가 아닌 동남아·남아 등 검색 노이즈 URL — 사우디 시장 조사에는 부적합·페치 실패 다발
_LOW_PRIORITY_TLD_SUFFIXES = (
    ".ph", ".id", ".vn", ".th", ".in", ".pk", ".bd", ".lk", ".np", ".mm",
)


def _search_result_domain_tier(domain: str) -> int:
    """0=SA 우선, 1=GCC, 2=기타, 3=동남아/남아 등 저우선."""
    d = (domain or "").lower()
    if d.endswith(".sa") or ".sa." in d:
        return 0
    if any(d.endswith(s) for s in (".ae", ".kw", ".bh", ".om", ".qa")):
        return 1
    if any(d.endswith(s) for s in _LOW_PRIORITY_TLD_SUFFIXES):
        return 3
    return 2


def prioritize_ksa_search_results(results: list[dict]) -> list[dict]:
    """DuckDuckGo 결과를 사우디·GCC 관련 도메인이 먼저 오도록 정렬. 충분한 상위 결과가 있으면 저우선 TLD 제외."""
    if not results:
        return results
    indexed = [( _search_result_domain_tier(r.get("domain", "")), i, r) for i, r in enumerate(results)]
    indexed.sort(key=lambda x: (x[0], x[1]))
    out = [x[2] for x in indexed]
    n_low = sum(1 for r in out if _search_result_domain_tier(r.get("domain", "")) == 3)
    n_ok = len(out) - n_low
    if n_low and n_ok >= 3:
        out = [r for r in out if _search_result_domain_tier(r.get("domain", "")) != 3]
    return out


EXCLUDED_DOMAINS = {
    # 이미 크롤러 존재
    "sfda.gov.sa", "developer.sfda.gov.sa",
    "nahdi.sa", "www.nahdi.sa",
    "aldawaa.com", "www.aldawaa.com",
    "whites.sa", "www.whites.sa",
    "nupco.com", "www.nupco.com",
    "etimad.sa", "www.etimad.sa",
    "tamergroup.com", "www.tamergroup.com",
    "noon.com", "www.noon.com",
    # 일반 사이트
    "google.com", "youtube.com", "facebook.com", "twitter.com",
    "linkedin.com", "instagram.com", "wikipedia.org",
    "amazon.com", "ebay.com",
}


# ---------------------------------------------------------------------------
# 검색 쿼리 생성 (LLM)
# ---------------------------------------------------------------------------

QUERY_SYSTEM_PROMPT = """You are a Saudi market research assistant for pharmaceuticals,
nutraceuticals, dietary supplements, and health-functional food ingredients.
Generate search queries to find NEW online sources of product, price, regulatory,
and distributor information specific to Saudi Arabia. Focus on:
- Online pharmacies, supplement retailers, and drug retailers in Saudi Arabia
- Saudi pharmaceutical, nutraceutical, and supplement distributors/wholesalers
- Saudi hospital pharmacy formularies when the target is a medicine
- Saudi drug, supplement, and health product pricing databases
- Saudi pharmaceutical, food, supplement, and health product regulatory databases

Return ONLY a JSON array of 5 search query strings. No explanation."""

QUERY_USER_TEMPLATE = """Generate 5 Google search queries to find Saudi Arabian pharmaceutical,
nutraceutical, dietary supplement, or health-functional food sources for this product:

Product: {trade_name}
Active / Functional Ingredients: {ingredients}
Dosage Form: {dosage_form}
Strength: {strength}

The queries should help find:
1. Saudi online pharmacies or supplement retailers selling this or similar products
2. Saudi pharmaceutical/supplement price comparison sites
3. Saudi importers or distributors carrying this ingredient/category
4. Saudi regulatory, food, supplement, or hospital formulary references as applicable
5. Regional/GCC pharmaceutical, nutraceutical, or supplement databases

Return JSON array of 5 query strings. Each query MUST include "Saudi" or "KSA" or "سعودية".
Prefer sources in Saudi Arabia (.sa) or GCC; do NOT optimize for Philippines, India, or Southeast Asia pharmacies.
Example format: ["query 1", "query 2", "query 3", "query 4", "query 5"]"""


def generate_search_queries(
    llm_client,
    drug_info: dict,
) -> list[str]:
    """LLM으로 약품에 맞는 검색 쿼리 5개 생성.

    Parameters
    ----------
    llm_client : ClaudeClient
    drug_info : dict
        {trade_name, ingredients, dosage_form, strength}

    Returns
    -------
    list[str]  검색 쿼리 문자열 리스트
    """
    prompt = QUERY_USER_TEMPLATE.format(
        trade_name=drug_info.get("trade_name", ""),
        ingredients=drug_info.get("ingredients", ""),
        dosage_form=drug_info.get("dosage_form", ""),
        strength=drug_info.get("strength", ""),
    )

    try:
        queries = llm_client.ask_json(prompt, system=QUERY_SYSTEM_PROMPT, max_tokens=256)
        if isinstance(queries, list):
            return [str(q) for q in queries[:5]]
    except Exception as e:
        logger.error("검색 쿼리 생성 실패: %s", e)

    # fallback: 수동 쿼리 생성
    return _fallback_queries(drug_info)


def _fallback_queries(drug_info: dict) -> list[str]:
    """LLM 실패 시 규칙 기반 쿼리 생성."""
    name = drug_info.get("trade_name", "")
    ingredients = drug_info.get("ingredients", "")
    form = drug_info.get("dosage_form", "")

    # 성분명에서 첫 번째 성분 추출
    first_ingredient = ingredients.split("+")[0].strip() if ingredients else name

    return [
        f"{name} site:.sa pharmacy price SAR",
        f"{first_ingredient} Saudi Arabia supplement pharmacy buy online",
        f"{first_ingredient} price Saudi Arabia SAR supplement",
        f"{first_ingredient} {form} KSA pharmaceutical nutraceutical",
        f"صيدلية {name} السعودية سعر",
    ]


# ---------------------------------------------------------------------------
# 검색 결과 URL 수집
# ---------------------------------------------------------------------------

# Google Custom Search API 없이 — DuckDuckGo HTML 파싱 (Actions에서 실행)
SEARCH_URL = "https://html.duckduckgo.com/html/"


def fetch_search_results(
    query: str,
    *,
    http_client,
    max_results: int = 10,
) -> list[dict]:
    """DuckDuckGo HTML에서 검색 결과 URL 수집.

    Parameters
    ----------
    query : str
    http_client : httpx.Client
    max_results : int

    Returns
    -------
    list[dict]  [{url, title, snippet}, ...]
    """
    from antibot import pick_ua

    headers = {
        "User-Agent": pick_ua(),
    }

    try:
        resp = http_client.post(
            SEARCH_URL,
            data={"q": query, "b": ""},
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning("DuckDuckGo 검색 실패: %d", resp.status_code)
            return []

        return _parse_ddg_results(resp.text, max_results)

    except Exception as e:
        logger.error("검색 요청 실패: %s", e)
        return []


def _parse_ddg_results(html: str, max_results: int) -> list[dict]:
    """DuckDuckGo HTML 결과 파싱."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results = []

    for link in soup.select("a.result__a"):
        href = link.get("href", "")
        # DuckDuckGo redirect URL에서 실제 URL 추출
        if "uddg=" in href:
            from urllib.parse import unquote, parse_qs, urlparse as _urlparse
            parsed = _urlparse(href)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                href = unquote(params["uddg"][0])

        if not href.startswith("http"):
            continue

        title = link.get_text(strip=True)
        snippet_el = link.find_parent("div")
        snippet = ""
        if snippet_el:
            snippet_text = snippet_el.find("a", class_="result__snippet")
            if snippet_text:
                snippet = snippet_text.get_text(strip=True)

        domain = urlparse(href).netloc.lower()
        # 제외 도메인 필터링
        base_domain = ".".join(domain.split(".")[-2:])
        if base_domain in EXCLUDED_DOMAINS or domain in EXCLUDED_DOMAINS:
            continue

        results.append({
            "url": href,
            "title": title,
            "snippet": snippet,
            "domain": domain,
        })

        if len(results) >= max_results:
            break

    return results


# ---------------------------------------------------------------------------
# URL → HTML 가져오기
# ---------------------------------------------------------------------------

# 최신 크롬 UA (일부 소매 사이트가 구버전 UA·최소 헤더만으로 403/빈 응답)
_DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _browser_like_headers(url: str, variant: int) -> dict[str, str]:
    """variant 0: 직접 방문, 1: 구글 레퍼러, 2: 동일 origin 레퍼러 + 다른 UA."""
    from antibot import pick_ua

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""

    ua = _DEFAULT_CHROME_UA if variant < 2 else pick_ua()
    h: dict[str, str] = {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9,ar-SA;q=0.85,ar;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
    }
    if variant == 0:
        h["Sec-Fetch-Site"] = "none"
    elif variant == 1:
        h["Referer"] = "https://www.google.com/"
        h["Sec-Fetch-Site"] = "cross-site"
    else:
        h["Referer"] = (origin + "/") if origin else "https://www.google.com/"
        h["Sec-Fetch-Site"] = "same-origin"
    return h


def fetch_page_html_ex(
    url: str,
    *,
    http_client,
    timeout: float = 32.0,
    max_attempts: int = 3,
) -> tuple[Optional[str], str]:
    """URL에서 HTML 가져오기. 재시도·브라우저형 헤더·상세 실패 사유.

    Returns
    -------
    (html 또는 None, 빈 문자열 또는 마지막 실패 요약)
    """
    from antibot import AntiBotType, detect as detect_antibot

    last_err = "요청 없음"
    attempts = max(1, min(max_attempts, 5))

    for attempt in range(attempts):
        headers = _browser_like_headers(url, attempt % 3)
        try:
            resp = http_client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            text = resp.text or ""
            hdrs = dict(resp.headers)

            if resp.status_code != 200:
                ab_type = detect_antibot(resp.status_code, text[:4000], hdrs)
                last_err = f"HTTP {resp.status_code}"
                if ab_type != AntiBotType.NONE:
                    last_err += f" ({ab_type.value})"
                logger.info("페이지 가져오기 실패: %s → %s", url, last_err)
                time.sleep(0.35 + attempt * 0.25)
                continue

            ab_type = detect_antibot(200, text[:6000], hdrs)
            if ab_type == AntiBotType.CLOUDFLARE:
                last_err = "HTTP 200 (Cloudflare challenge 페이지)"
                time.sleep(0.4 + attempt * 0.2)
                continue

            if len(text.strip()) < 80:
                last_err = "HTTP 200 (본문 너무 짧음)"
                time.sleep(0.3)
                continue

            return text, ""

        except Exception as e:
            last_err = f"{type(e).__name__}: {e!s}"[:200]
            logger.warning("페이지 요청 예외 %s: %s", url, last_err)
            time.sleep(0.45 + attempt * 0.3)

    return None, last_err


def fetch_page_html(
    url: str,
    *,
    http_client,
    timeout: float = 32.0,
) -> Optional[str]:
    """URL에서 HTML 가져오기. `fetch_page_html_ex` 래퍼."""
    html, _ = fetch_page_html_ex(url, http_client=http_client, timeout=timeout)
    return html


# ---------------------------------------------------------------------------
# LLM 사이트 판별
# ---------------------------------------------------------------------------

EVALUATE_SYSTEM_PROMPT = """You are a market research analyst evaluating websites
for relevance to the Saudi Arabian pharmaceutical, nutraceutical, supplement,
and health-functional food market.

Evaluate the provided website snippet and return a JSON object with these fields:
- relevance_score: float 0.0-1.0 (how relevant to Saudi product market research)
- category: one of "pharma_retailer", "supplement_retailer", "pharma_regulator", "food_regulator", "distributor", "hospital", "price_database", "news", "other"
- has_price_data: boolean (does the site show product prices?)
- has_product_listing: boolean (does the site list pharmaceutical, supplement, or health products?)
- language: "en", "ar", or "mixed"
- reason: brief explanation of your assessment (1 sentence)

Scoring guide:
- 0.9-1.0: Saudi pharma/supplement retailer or regulator with product listings and prices
- 0.7-0.8: Saudi pharma/supplement-related site with partial data (prices OR listings)
- 0.5-0.6: Pharma/supplement-related but not Saudi-specific, or limited data
- 0.3-0.4: Tangentially related (general health, news)
- 0.0-0.2: Not relevant

Return ONLY the JSON object."""

EVALUATE_USER_TEMPLATE = """Evaluate this website for Saudi pharmaceutical, nutraceutical,
or supplement market research:

URL: {url}
Domain: {domain}
Page Title (from search): {title}

Website content snippet:
---
{snippet}
---

Return JSON with: relevance_score, category, has_price_data, has_product_listing, language, reason"""


def evaluate_source(
    llm_client,
    url: str,
    domain: str,
    title: str,
    snippet: str,
    *,
    drug_key: str = "",
) -> DiscoveredSource:
    """LLM으로 발견된 사이트 평가.

    Parameters
    ----------
    llm_client : ClaudeClient
    url, domain, title, snippet : 사이트 정보
    drug_key : str
        약품 식별자 (trade_name 등). 캐시 키에 포함되어 약품별 평가가 교차 오염되지 않도록 함.

    Returns
    -------
    DiscoveredSource
    """
    # [B1] 캐시 키 = 도메인 + 약품 (약품별 relevance가 혼재하지 않도록)
    cache_key = f"{domain}::{drug_key}" if drug_key else domain

    # [B3/B4] Lock 하에서 캐시 읽기 (읽기만이므로 빠르게 반환)
    with _DOMAIN_CACHE_LOCK:
        cache = _load_domain_cache()
        entry = cache.get(cache_key)
        # [B5] ts 키 없거나 타입 불일치 시 0으로 안전 처리
        ts = float(entry.get("ts") or 0) if entry else 0.0
        if entry and (time.time() - ts) < _DOMAIN_CACHE_TTL_SEC:
            logger.info("evaluate_source 캐시 히트: %s", cache_key)
            data = entry.get("data", {})
            cached = DiscoveredSource(url=url, domain=domain, title=title)
            cached.relevance_score = float(data.get("relevance_score", 0.0))
            cached.category = data.get("category", "other")
            cached.has_price_data = bool(data.get("has_price_data", False))
            cached.has_product_listing = bool(data.get("has_product_listing", False))
            cached.language = data.get("language", "")
            cached.description = data.get("description", "")
            if cached.relevance_score < 0.6:
                cached.rejection_reason = f"Low relevance (cached): {cached.relevance_score:.2f}"
            return cached

    # 캐시 미스 → LLM 호출 (lock 밖에서 실행하여 병렬 I/O 허용)
    # 핀포인트: 3000 → 1500자로 축소
    prompt = EVALUATE_USER_TEMPLATE.format(
        url=url,
        domain=domain,
        title=title,
        snippet=snippet[:1500],
    )

    source = DiscoveredSource(url=url, domain=domain, title=title)

    try:
        result = llm_client.ask_json(prompt, system=EVALUATE_SYSTEM_PROMPT, max_tokens=256)

        source.relevance_score = float(result.get("relevance_score", 0.0))
        source.category = result.get("category", "other")
        source.has_price_data = bool(result.get("has_price_data", False))
        source.has_product_listing = bool(result.get("has_product_listing", False))
        source.language = result.get("language", "")
        source.description = result.get("reason", "")

        if source.relevance_score < 0.6:
            source.rejection_reason = f"Low relevance: {source.relevance_score:.2f} - {source.description}"

        # [B3/B4] 저장 시 Lock 재획득 (write 원자성 보장)
        with _DOMAIN_CACHE_LOCK:
            cache = _load_domain_cache()
            cache[cache_key] = {
                "ts": time.time(),
                "data": {
                    "relevance_score": source.relevance_score,
                    "category": source.category,
                    "has_price_data": source.has_price_data,
                    "has_product_listing": source.has_product_listing,
                    "language": source.language,
                    "description": source.description,
                },
            }
            _save_domain_cache(cache)

    except Exception as e:
        logger.error("사이트 평가 실패 %s: %s", url, e)
        source.rejection_reason = f"Evaluation error: {e}"

    return source


# ---------------------------------------------------------------------------
# 전체 파이프라인: 1약품 → 발견된 소스 목록
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    """소스 발견 전체 결과."""
    drug_name: str
    queries_generated: list[str]
    urls_found: int
    pages_fetched: int
    sources_evaluated: int
    valid_sources: list[DiscoveredSource]
    rejected_sources: list[DiscoveredSource]
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "drug_name": self.drug_name,
            "queries_generated": self.queries_generated,
            "urls_found": self.urls_found,
            "pages_fetched": self.pages_fetched,
            "sources_evaluated": self.sources_evaluated,
            "valid_count": len(self.valid_sources),
            "rejected_count": len(self.rejected_sources),
            "valid_sources": [
                {"url": s.url, "domain": s.domain, "score": s.relevance_score,
                 "category": s.category, "has_price": s.has_price_data}
                for s in self.valid_sources
            ],
            "duration_sec": round(self.duration_sec, 1),
        }


def discover_sources_perplexity(
    pplx_client,
    drug_info: dict,
) -> DiscoveryResult:
    """Perplexity API 1회 호출로 소스 발견.

    Parameters
    ----------
    pplx_client : PerplexityClient
    drug_info : dict
        {trade_name, ingredients, dosage_form, strength}

    Returns
    -------
    DiscoveryResult
    """
    start_time = time.time()
    drug_name = drug_info.get("trade_name", "unknown")

    raw_sources = pplx_client.search_pharma_sources(drug_info, EXCLUDED_DOMAINS)

    valid_sources: list[DiscoveredSource] = []
    rejected_sources: list[DiscoveredSource] = []

    for src in raw_sources:
        ds = DiscoveredSource(
            url=src["url"],
            domain=src["domain"],
            title=src.get("title", ""),
            description=src.get("description", ""),
            relevance_score=src.get("relevance_score", 0.0),
            category=src.get("category", "other"),
            has_price_data=src.get("has_price_data", False),
            has_product_listing=src.get("has_product_listing", False),
            language=src.get("language", ""),
            search_query="perplexity_sonar",
        )

        if ds.relevance_score < 0.6:
            ds.rejection_reason = f"Low relevance: {ds.relevance_score:.2f} - {ds.description}"

        if ds.is_valid:
            valid_sources.append(ds)
            logger.info(
                "[%s] Perplexity 유효 소스: %s (%.2f, %s)",
                drug_name, ds.domain, ds.relevance_score, ds.category,
            )
        else:
            rejected_sources.append(ds)

    elapsed = time.time() - start_time

    return DiscoveryResult(
        drug_name=drug_name,
        queries_generated=[f"perplexity:{drug_name}"],
        urls_found=len(raw_sources),
        pages_fetched=0,
        sources_evaluated=len(raw_sources),
        valid_sources=valid_sources,
        rejected_sources=rejected_sources,
        duration_sec=elapsed,
    )


def discover_sources(
    llm_client,
    http_client,
    drug_info: dict,
    *,
    pplx_client=None,
    max_queries: int = 5,
    max_urls_per_query: int = 5,
    fetch_delay: float = 2.0,
) -> DiscoveryResult:
    """1약품에 대해 AI 자율 소스 발견 실행.

    Perplexity API 키가 있으면 우선 사용, 없거나 실패하면 DuckDuckGo fallback.

    Parameters
    ----------
    llm_client : ClaudeClient
    http_client : httpx.Client
    drug_info : dict
        {trade_name, ingredients, dosage_form, strength}
    pplx_client : PerplexityClient | None
        Perplexity 클라이언트 (선택)
    max_queries : int
    max_urls_per_query : int
    fetch_delay : float  요청 간 딜레이 (초)

    Returns
    -------
    DiscoveryResult
    """
    drug_name = drug_info.get("trade_name", "unknown")

    # ── Perplexity 우선 시도 ──
    if pplx_client and pplx_client.available:
        try:
            result = discover_sources_perplexity(pplx_client, drug_info)
            if result.valid_sources:
                logger.info(
                    "[%s] Perplexity: %d개 유효 소스 발견",
                    drug_name, len(result.valid_sources),
                )
                return result
            logger.info("[%s] Perplexity 결과 없음 → DuckDuckGo fallback", drug_name)
        except Exception as e:
            logger.warning("[%s] Perplexity 실패 → DuckDuckGo fallback: %s", drug_name, e)

    # ── 기존 DuckDuckGo 파이프라인 ──
    start_time = time.time()
    drug_name = drug_info.get("trade_name", "unknown")

    # Step 1: 검색 쿼리 생성
    queries = generate_search_queries(llm_client, drug_info)
    logger.info("[%s] 검색 쿼리 %d개 생성", drug_name, len(queries))

    # Step 2: 검색 결과 URL 수집 (중복 제거)
    seen_domains: set[str] = set()
    all_search_results: list[dict] = []

    for query in queries[:max_queries]:
        results = fetch_search_results(query, http_client=http_client, max_results=max_urls_per_query)
        for r in results:
            domain = r["domain"]
            base = ".".join(domain.split(".")[-2:])
            if base not in seen_domains:
                seen_domains.add(base)
                r["search_query"] = query
                all_search_results.append(r)
        time.sleep(fetch_delay)

    all_search_results = prioritize_ksa_search_results(all_search_results)
    logger.info("[%s] URL %d개 수집 (KSA/GCC 우선 정렬 후)", drug_name, len(all_search_results))

    # Step 3-5: 각 URL → HTML 가져오기 → 전처리 → LLM 판별
    # [A2] import 스타일 통일 (bare name → 절대 경로)
    # [A1] 키워드 집중 스니펫 추출로 전환 (extract_pinpoint_snippet)
    from assets.snippets.html_preprocessor import extract_pinpoint_snippet

    valid_sources: list[DiscoveredSource] = []
    rejected_sources: list[DiscoveredSource] = []
    pages_fetched = 0

    for sr in all_search_results:
        url = sr["url"]
        domain = sr["domain"]

        # HTML 가져오기
        html = fetch_page_html(url, http_client=http_client)
        if html:
            pages_fetched += 1
            # [A1] 핀포인트 추출: 약품명 키워드 주변 블록만 선별 (토큰 절감)
            snippet = extract_pinpoint_snippet(html, keywords=[drug_name], max_chars=1500)
            if not snippet:
                snippet = sr.get("snippet", "")
        else:
            snippet = sr.get("snippet", "")

        # LLM 판별 — [B1] drug_key 전달로 약품별 캐시 격리
        source = evaluate_source(
            llm_client,
            url=url,
            domain=domain,
            title=sr.get("title", ""),
            snippet=snippet,
            drug_key=drug_name,
        )
        source.search_query = sr.get("search_query", "")

        if source.is_valid:
            valid_sources.append(source)
            logger.info("[%s] 유효 소스 발견: %s (%.2f, %s)", drug_name, domain, source.relevance_score, source.category)
        else:
            rejected_sources.append(source)

        time.sleep(fetch_delay)

    elapsed = time.time() - start_time

    result = DiscoveryResult(
        drug_name=drug_name,
        queries_generated=queries,
        urls_found=len(all_search_results),
        pages_fetched=pages_fetched,
        sources_evaluated=len(valid_sources) + len(rejected_sources),
        valid_sources=valid_sources,
        rejected_sources=rejected_sources,
        duration_sec=elapsed,
    )

    logger.info(
        "[%s] 소스 발견 완료: %d개 유효 / %d개 거부 (%.1fs)",
        drug_name, len(valid_sources), len(rejected_sources), elapsed,
    )

    return result
