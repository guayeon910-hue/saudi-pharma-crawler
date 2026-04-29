"""
perplexity_client.py — Perplexity Sonar API 클라이언트

역할:
  - Perplexity Sonar API로 사우디 의약품 소스 웹 검색
  - 1회 호출로 URL + 메타데이터 + citations 반환
  - DuckDuckGo + Claude Haiku 다단계 파이프라인 대체
  - 재시도 + 지수 백오프 (429/5xx)
  - 토큰 사용량 추적 (llm_client.TokenUsage 재사용)

통합 지점:
  - source_discoverer.py: discover_sources_perplexity()에서 호출
  - ai_search.py: 메인 오케스트레이터에서 생성 + 전달

실행 환경: GitHub Actions 전용 (HTTP 요청 포함)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

from llm_client import TokenUsage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 모델 상수
# ---------------------------------------------------------------------------
MODEL_SONAR = "sonar"
MODEL_SONAR_PRO = "sonar-pro"

DEFAULT_MODEL = MODEL_SONAR  # 비용 최적

# ---------------------------------------------------------------------------
# 재시도 설정
# ---------------------------------------------------------------------------
_RETRYABLE = {429, 500, 502, 503}
MAX_RETRIES = 3
BASE_DELAY = 2.0

# ---------------------------------------------------------------------------
# 프롬프트
# ---------------------------------------------------------------------------

SEARCH_SYSTEM_PROMPT = """You are a pharmaceutical business development expert specializing in Saudi Arabia/KSA.
Your task is to identify potential buyer or distributor companies for Korean pharmaceutical products entering the Saudi market.
Return ONLY a valid JSON array. No explanation, no markdown, no code fences."""

SEARCH_USER_TEMPLATE = """Find Saudi Arabian pharmaceutical importers, distributors, or hospital procurement agencies that could be buyers for this Korean drug:
- Drug: {trade_name}
- Active Ingredients: {ingredients}
- Dosage Form: {dosage_form}
- Strength: {strength}

Focus on: licensed importers, SFDA-registered distributors, NUPCO-approved suppliers, hospital group procurement offices, pharmacy chains, and Saudi pharmaceutical companies that actively in-license or distribute partner products.
If drug-specific buyers are scarce, include credible general Saudi pharmaceutical distributors or procurement targets that could onboard a new Korean product after regulatory review.

Exclude these already-known domains: {excluded}

Return a JSON array of 15 to 20 objects when possible. Each object must have:
- "url": company website URL (homepage or product page)
- "title": full company name
- "description": 1-2 sentences about what this company does and why it is relevant as a buyer/distributor for this drug
- "category": one of "importer", "distributor", "hospital_group", "pharmacy_chain", "government_procurement", "other"
- "has_price_data": boolean — true if website shows drug prices
- "has_product_listing": boolean — true if website lists this or similar drugs
- "language": "en", "ar", or "mixed"
- "relevance_score": float 0.0–1.0 reflecting how likely this company could distribute this drug in Saudi Arabia

Return ONLY the JSON array, no other text. Do not return fewer than 10 objects unless fewer than 10 credible Saudi buyer targets exist."""

REFERENCE_SYSTEM_PROMPT = """You are a pharmaceutical market research assistant.
Find concise, citable web references for Saudi Arabia pharmaceutical market analysis.
Return ONLY a valid JSON array. No explanation, no markdown, no code fences."""

REFERENCE_USER_TEMPLATE = """Find citable sources for this Saudi Arabia/KSA pharmaceutical market report:
- Drug: {trade_name}
- Active Ingredients: {ingredients}
- Dosage Form: {dosage_form}
- Strength: {strength}

Prioritize official or high-signal sources: SFDA drug registration/price pages, Saudi pharmacy product pages, public procurement pages, clinical or regulatory references.

Return a JSON array of up to 8 objects. Each object must have:
- "url": source URL
- "title": short source title
- "category": one of "regulatory", "price_database", "pharmacy", "clinical", "procurement", "market"
- "reason": one short sentence explaining why it supports the report

Return ONLY the JSON array, no other text."""


# ---------------------------------------------------------------------------
# Perplexity 클라이언트
# ---------------------------------------------------------------------------

class PerplexityClient:
    """Perplexity Sonar API 클라이언트.

    Parameters
    ----------
    api_key : str | None
        PERPLEXITY_API_KEY. None이면 환경변수에서 읽음.
    model : str
        Perplexity 모델 (sonar / sonar-pro).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._api_key = (
            api_key.strip()
            if api_key is not None
            else os.environ.get("PERPLEXITY_API_KEY", "").strip()
        )
        self.model = model or os.environ.get("PERPLEXITY_MODEL", DEFAULT_MODEL)
        self.usage = TokenUsage()

        import httpx
        self._http = httpx.Client(
            base_url="https://api.perplexity.ai",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    @property
    def available(self) -> bool:
        """API 키가 설정되었는지 확인."""
        return bool(self._api_key)

    def search_pharma_sources(
        self,
        drug_info: dict,
        excluded_domains: set[str],
    ) -> list[dict]:
        """사우디 의약품 소스 검색. 1회 호출.

        Parameters
        ----------
        drug_info : dict
            {trade_name, ingredients, dosage_form, strength}
        excluded_domains : set[str]
            제외할 도메인 집합

        Returns
        -------
        list[dict]
            [{url, domain, title, description, relevance_score,
              category, has_price_data, has_product_listing, language}, ...]
        """
        # 제외 도메인을 간결하게 (www. 제거 후 중복 제거)
        clean_excluded = sorted(set(
            d.replace("www.", "") for d in excluded_domains
        ))

        prompt = SEARCH_USER_TEMPLATE.format(
            trade_name=drug_info.get("trade_name", ""),
            ingredients=drug_info.get("ingredients", ""),
            dosage_form=drug_info.get("dosage_form", ""),
            strength=drug_info.get("strength", ""),
            excluded=", ".join(clean_excluded),
        )

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 4096,
        }

        data = self._call_with_retry(body)

        # 응답 파싱
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])

        # 토큰 추적
        usage = data.get("usage", {})
        self.usage.add(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        # JSON 파싱 (마크다운 코드블록 처리, 실패 시 [] 반환)
        try:
            sources_raw = self._parse_json(content)
        except Exception as parse_err:
            logger.warning("Perplexity JSON 파싱 예외: %s | 응답: %s", parse_err, content[:300])
            sources_raw = []
        if not isinstance(sources_raw, list):
            logger.warning("Perplexity 응답이 리스트가 아님: %s | 응답: %s", type(sources_raw), content[:200])
            sources_raw = []
        logger.info("Perplexity sources_raw 파싱 결과: %d 항목", len(sources_raw))

        # citation URL → domain 매핑 (검증된 URL)
        citation_domains: set[str] = set()
        for url in citations:
            try:
                citation_domains.add(urlparse(url).netloc.lower())
            except Exception:
                pass

        # 소스 정규화 + 필터링
        results: list[dict] = []
        seen_domains: set[str] = set()

        for src in sources_raw:
            url = src.get("url", "")
            if not url.startswith("http"):
                continue
            url_path = urlparse(url).path.lower()
            if url_path.endswith(".pdf"):
                continue

            domain = urlparse(url).netloc.lower()
            base_domain = ".".join(domain.split(".")[-2:])
            source_text = " ".join(
                str(src.get(key, ""))
                for key in ("title", "description", "category")
            ).lower()
            source_text = f"{source_text} {url.lower()}"
            saudi_terms = (
                "saudi",
                "ksa",
                "kingdom of saudi arabia",
                "riyadh",
                "jeddah",
                "dammam",
                "khobar",
                ".sa",
            )
            foreign_terms = (
                "qatar",
                "uae",
                "united arab emirates",
                "kuwait",
                "bahrain",
                "oman",
                "egypt",
                "jordan",
            )
            has_saudi_signal = any(term in source_text for term in saudi_terms)
            if any(term in source_text for term in foreign_terms) and not has_saudi_signal:
                continue
            if str(src.get("category", "")).lower() == "hospital_group" and not has_saudi_signal:
                continue

            # 제외 도메인 필터 (방어적)
            if base_domain in excluded_domains or domain in excluded_domains:
                continue

            # 중복 도메인 제거
            if base_domain in seen_domains:
                continue
            seen_domains.add(base_domain)

            # citation에도 있으면 신뢰도 유지, 아니면 약간 감점
            score = float(src.get("relevance_score", 0.7))
            if domain in citation_domains or base_domain in {
                ".".join(cd.split(".")[-2:]) for cd in citation_domains
            }:
                score = max(score, 0.70)  # citation 검증 최소 보장

            results.append({
                "url": url,
                "domain": domain,
                "title": src.get("title", ""),
                "description": src.get("description", ""),
                "relevance_score": score,
                "category": src.get("category", "other"),
                "has_price_data": bool(src.get("has_price_data", False)),
                "has_product_listing": bool(src.get("has_product_listing", False)),
                "language": src.get("language", ""),
            })

        # Citation-only URLs are evidence links, not buyer records. They are
        # used above to score returned company URLs, but not appended to P3.

        logger.info(
            "Perplexity 검색 완료: %d sources (%d citations)",
            len(results), len(citations),
        )

        return results

    def search(
        self,
        *,
        trade_name: str,
        ingredients: str,
        dosage_form: str = "",
        strength: str = "",
    ) -> dict:
        """Report reference search used by frontend.server._fetch_references.

        Returns
        -------
        dict
            {"sources": [{title, url, category, reason}, ...]}
        """
        prompt = REFERENCE_USER_TEMPLATE.format(
            trade_name=trade_name,
            ingredients=ingredients,
            dosage_form=dosage_form,
            strength=strength,
        )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": REFERENCE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 1536,
        }

        data = self._call_with_retry(body)
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", []) or []

        usage = data.get("usage", {})
        self.usage.add(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        try:
            raw_items = self._parse_json(content)
        except Exception as parse_err:
            logger.warning("Perplexity reference JSON parse exception: %s", parse_err)
            raw_items = []
        if not isinstance(raw_items, list):
            raw_items = []

        results: list[dict] = []
        seen_urls: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith("http") or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "url": url,
                "title": str(item.get("title") or urlparse(url).netloc or "Source")[:200],
                "category": str(item.get("category") or "web"),
                "reason": str(item.get("reason") or "Perplexity reference")[:300],
            })

        for url in citations:
            if not isinstance(url, str) or not url.startswith("http") or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "url": url,
                "title": urlparse(url).netloc or "Citation",
                "category": "citation",
                "reason": "Perplexity citation",
            })

        return {"sources": results[:8]}

    # -------------------------------------------------------------------
    # 내부
    # -------------------------------------------------------------------

    def _call_with_retry(self, body: dict) -> dict:
        """지수 백오프 재시도."""
        import httpx

        last_err: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.post("/chat/completions", json=body)

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code in _RETRYABLE and attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("retry-after")
                        if retry_after:
                            delay = max(delay, float(retry_after))
                    logger.warning(
                        "Perplexity API %d, retry %d/%d (%.1fs)",
                        resp.status_code, attempt + 1, MAX_RETRIES, delay,
                    )
                    self.usage.errors += 1
                    time.sleep(delay)
                    continue

                resp.raise_for_status()

            except httpx.TimeoutException as e:
                last_err = e
                self.usage.errors += 1
                if attempt < MAX_RETRIES:
                    delay = BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Perplexity API timeout, retry %d/%d",
                        attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise

        raise RuntimeError(f"Perplexity API failed after {MAX_RETRIES} retries: {last_err}")

    @staticmethod
    def _parse_json(text: str) -> Any:
        """응답 텍스트에서 JSON 추출. 여러 형식 순차 시도 후 실패 시 [] 반환."""
        import re
        text = text.strip()

        # 1) 코드블록 추출 (```json ... ``` 또는 ``` ... ```)
        m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 2) 전체 텍스트 직접 파싱
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 3) 텍스트 내부에서 JSON 배열 패턴 추출
        m2 = re.search(r'\[[\s\S]*\]', text)
        if m2:
            try:
                return json.loads(m2.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("Perplexity JSON 파싱 실패 — 응답 앞 300자: %s", text[:300])
        return []

    def close(self) -> None:
        """HTTP 클라이언트 정리."""
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
