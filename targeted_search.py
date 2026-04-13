"""
targeted_search.py — 타겟 의약품 검색 오케스트레이터

1개 의약품을 받아서 10개 사우디 소스에서 타겟 검색을 수행하고,
결과를 카테고리별로 집계하여 수출 가능 여부를 판정한다.

사용:
    from drug_registry import DrugRegistry
    from targeted_search import search_one_drug

    reg = DrugRegistry()
    drug = reg.get_drug("rosumeg-combigel")
    result = search_one_drug(drug)
    print(result.export_feasibility)  # "가능" / "조건부" / "불가"
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

from dotenv import load_dotenv
load_dotenv()
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# snippets 경로 추가
sys.path.insert(0, str(Path(__file__).resolve().parent / "assets" / "snippets"))
from sfda_web import SFDAWebClient, map_web_to_schema
from normalizer import normalize_record, normalize_dosage_form
from antibot import pick_ua

from drug_registry import TargetDrug, DrugRegistry

logger = logging.getLogger("targeted_search")


# ─── 결과 데이터 모델 ──────────────────────────────────

@dataclass
class SearchResult:
    """개별 소스의 검색 결과."""
    source_name: str
    source_category: str       # "공공조달" | "민간" | "논문"
    source_url: str
    matches: list[dict] = field(default_factory=list)
    queries_used: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str | None = None
    search_time_sec: float = 0.0


@dataclass
class AggregatedResult:
    """전체 검색 결과 집계."""
    drug: TargetDrug
    source_results: list[SearchResult] = field(default_factory=list)
    export_feasibility: str = "불가"           # "가능" / "조건부" / "불가"
    feasibility_rationale: str = ""
    feasibility_evidence_urls: list[str] = field(default_factory=list)
    timestamp: str = ""
    total_matches: int = 0
    search_duration_sec: float = 0.0

    @property
    def by_category(self) -> dict[str, list[SearchResult]]:
        """카테고리별 결과 분류."""
        cats: dict[str, list[SearchResult]] = {
            "공공조달": [],
            "민간": [],
            "논문": [],
        }
        for r in self.source_results:
            cat = r.source_category
            if cat in cats:
                cats[cat].append(r)
            else:
                cats.setdefault(cat, []).append(r)
        return cats

    def to_dict(self) -> dict:
        """JSON 직렬화용."""
        from dataclasses import asdict
        d = asdict(self)
        d["by_category"] = {
            k: [asdict(r) for r in v]
            for k, v in self.by_category.items()
        }
        return d


# ─── 소스 카테고리 매핑 ─────────────────────────────────

SOURCE_CATEGORIES = {
    "sfda_api":             "공공조달",
    "sfda_drugs_list_html": "공공조달",
    "sfda_companies":       "공공조달",
    "nupco_tenders":        "공공조달",
    "etimad_api":           "공공조달",
    "nahdi_web":            "민간",
    "al_dawaa_web":         "민간",
    "whites_web":           "민간",
    "tamer_group":          "민간",
    "noon_saudi":           "민간",
}


# ─── 개별 소스 검색 함수 ─────────────────────────────────

def _search_sfda(drug: TargetDrug, keywords: dict) -> SearchResult:
    """SFDA 의약품 API에서 타겟 검색.

    가장 중요한 소스 — 규제 등록 데이터.
    검색 전략:
      1) trade_name으로 검색
      2) 각 ingredient_name으로 검색
      3) 결과에서 dosage_form/strength 매칭으로 필터링
    """
    result = SearchResult(
        source_name="sfda_api",
        source_category="공공조달",
        source_url="https://www.sfda.gov.sa/en/drugs-list",
    )
    all_matches = []
    seen_ids = set()

    try:
        with SFDAWebClient(delay=0.5) as client:
            # 1) 품목명 검색
            trade_name = keywords["trade_name"]
            result.queries_used.append(f"TradeName={trade_name}")
            try:
                data = client.search(trade_name=trade_name, page=1)
                for item in (data.get("results") or []):
                    rid = item.get("registerNumber", "")
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        record = map_web_to_schema(item, source_url=result.source_url)
                        record = normalize_record(record)
                        all_matches.append(record)
            except Exception as e:
                logger.warning(f"SFDA trade_name 검색 실패: {e}")

            # 2) 성분명 검색 (각 성분별, 5페이지까지)
            for name in keywords.get("ingredient_names", []):
                result.queries_used.append(f"ScientificName={name}")
                try:
                    data = client.search(scientific_name=name, page=1)
                    items = data.get("results") or []
                    page_count = min(data.get("pageCount", 1), 5)
                    for pg in range(2, page_count + 1):
                        data_pg = client.search(scientific_name=name, page=pg)
                        items.extend(data_pg.get("results") or [])

                    for item in items:
                        rid = item.get("registerNumber", "")
                        if rid not in seen_ids:
                            seen_ids.add(rid)
                            record = map_web_to_schema(item, source_url=result.source_url)
                            record = normalize_record(record)
                            all_matches.append(record)
                except Exception as e:
                    logger.warning(f"SFDA scientific_name 검색 실패 ({name}): {e}")

        result.matches = all_matches
        result.confidence = 0.92 if all_matches else 0.0

    except Exception as e:
        result.error = str(e)
        logger.error(f"SFDA 검색 전체 실패: {e}")

    return result


def _search_sfda_companies(drug: TargetDrug, keywords: dict) -> SearchResult:
    """SFDA 제약사 API에서 검색."""
    result = SearchResult(
        source_name="sfda_companies",
        source_category="공공조달",
        source_url="https://www.sfda.gov.sa/en/drug-companies",
    )

    try:
        from crawlers.sfda_companies import SFDACompaniesClient

        with SFDACompaniesClient(delay=0.5) as client:
            # 성분명의 첫 번째 단어로 제조사 검색
            for name in keywords.get("ingredient_names", [])[:2]:
                first_word = name.split()[0] if name else ""
                if len(first_word) < 3:
                    continue
                result.queries_used.append(f"CompanyEnName={first_word}")
                try:
                    data = client.search(company_name=first_word, page=1)
                    for item in (data.get("results") or []):
                        result.matches.append({
                            "company_name": item.get("companY_ENG_DESC") or item.get("company_eng_desc"),
                            "agent_name": item.get("agenT_NAME") or item.get("agent_name"),
                            "country": item.get("country_Desc") or item.get("country_desc"),
                            "production_line": item.get("productionLine"),
                            "raw": item,
                        })
                except Exception as e:
                    logger.warning(f"SFDA companies 검색 실패 ({first_word}): {e}")

        result.confidence = 0.80 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_nahdi(drug: TargetDrug, keywords: dict) -> SearchResult:
    """Nahdi 약국 Algolia API 검색."""
    from crawlers.nahdi_web import NahdiClient

    result = SearchResult(
        source_name="nahdi_web",
        source_category="민간",
        source_url="https://www.nahdionline.com/en-sa",
    )

    search_terms = []
    # 성분명으로 검색 (제품명은 한국 브랜드라 매칭 안됨)
    for name in keywords.get("ingredient_names", []):
        search_terms.append(name)
    # 성분명 첫 단어만으로도 검색 (약어/변형 대응)
    for name in keywords.get("ingredient_names", []):
        first_word = name.split()[0]
        if first_word not in search_terms and len(first_word) >= 4:
            search_terms.append(first_word)

    seen_names: set[str] = set()

    try:
        with NahdiClient(delay=0.5) as client:
            for term in search_terms:
                result.queries_used.append(f"algolia_query={term}")
                try:
                    products = client.search(term, hits_per_page=20)
                    for p in products:
                        pname = p.get("name", "")
                        if pname and pname not in seen_names:
                            seen_names.add(pname)
                            result.matches.append(p)
                except Exception as e:
                    logger.warning(f"Nahdi 검색 실패 ({term}): {e}")

        result.confidence = 0.75 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_whites(drug: TargetDrug, keywords: dict) -> SearchResult:
    """Whites 약국 검색 (Akinon 커머스, search_text 파라미터)."""
    from crawlers.whites_web import WhitesClient, _parse_products_from_html

    result = SearchResult(
        source_name="whites_web",
        source_category="민간",
        source_url="https://www.whites.sa/en-sa",
    )

    search_terms = []
    for name in keywords.get("ingredient_names", []):
        search_terms.append(name)
    # 첫 단어만으로도 검색 (약어/변형 대응)
    for name in keywords.get("ingredient_names", []):
        first_word = name.split()[0]
        if first_word not in search_terms and len(first_word) >= 4:
            search_terms.append(first_word)

    seen_names: set[str] = set()

    try:
        with WhitesClient(delay=1.0) as client:
            for term in search_terms:
                result.queries_used.append(f"search_text={term}")
                try:
                    html = client.search(term)
                    products = _parse_products_from_html(html)
                    for p in products:
                        pname = p.get("name", "")
                        if pname and pname not in seen_names:
                            seen_names.add(pname)
                            result.matches.append(p)
                except Exception as e:
                    logger.warning(f"Whites 검색 실패 ({term}): {e}")
                    if "anti-bot" in str(e).lower() or "cloudflare" in str(e).lower():
                        result.error = f"Anti-bot 차단: {e}"
                        break

        result.confidence = 0.70 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_retail_generic(
    source_name: str,
    source_url: str,
    client_class: type,
    drug: TargetDrug,
    keywords: dict,
) -> SearchResult:
    """Al Dawaa 등 기타 소매 사이트 공통 검색 (HTML 파싱 방식)."""
    result = SearchResult(
        source_name=source_name,
        source_category="민간",
        source_url=source_url,
    )

    search_terms = [keywords["trade_name"]]
    if keywords.get("ingredient_names"):
        search_terms.append(keywords["ingredient_names"][0])

    try:
        with client_class(delay=1.5) as client:
            for term in search_terms:
                result.queries_used.append(f"q={term}")
                try:
                    html = client.search(term)

                    if source_name == "al_dawaa_web":
                        from crawlers.al_dawaa_web import _parse_products_from_html
                    else:
                        break

                    products = _parse_products_from_html(html)
                    for p in products:
                        if p not in result.matches:
                            result.matches.append(p)

                except Exception as e:
                    logger.warning(f"{source_name} 검색 실패 ({term}): {e}")
                    if "anti-bot" in str(e).lower() or "cloudflare" in str(e).lower():
                        result.error = f"Anti-bot 차단: {e}"
                        break

        result.confidence = 0.75 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_nupco(drug: TargetDrug, keywords: dict) -> SearchResult:
    """NUPCO 텐더에서 검색 (HTML 텍스트 매칭)."""
    result = SearchResult(
        source_name="nupco_tenders",
        source_category="공공조달",
        source_url="https://www.nupco.com/en/tenders/",
    )

    try:
        from crawlers.nupco_tenders import NUPCOClient, _parse_tender_links

        search_terms = [keywords["trade_name"]] + keywords.get("ingredient_names", [])
        search_terms_lower = [t.lower() for t in search_terms if t]

        with NUPCOClient(delay=1.0) as client:
            # 최근 5페이지만 스캔
            for page in range(1, 6):
                result.queries_used.append(f"page={page}")
                try:
                    html = client.get_tender_list(page)
                    tenders = _parse_tender_links(html)

                    for tender in tenders:
                        title_lower = tender.get("title", "").lower()
                        if any(term in title_lower for term in search_terms_lower):
                            result.matches.append(tender)
                except Exception as e:
                    logger.warning(f"NUPCO 페이지 {page} 실패: {e}")
                    break

        result.confidence = 0.70 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_etimad(drug: TargetDrug, keywords: dict) -> SearchResult:
    """Etimad 공공조달 API 검색."""
    import os
    result = SearchResult(
        source_name="etimad_api",
        source_category="공공조달",
        source_url="https://apiportal.etimad.sa",
    )

    api_key = os.environ.get("ETIMAD_API_KEY", "")
    if not api_key:
        result.error = "ETIMAD_API_KEY 미설정"
        return result

    try:
        import httpx
        client = httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "application/json",
                "Ocp-Apim-Subscription-Key": api_key,
            },
        )

        for name in keywords.get("ingredient_names", [])[:1]:
            result.queries_used.append(f"keyword={name}")
            try:
                resp = client.get(
                    "https://apiportal.etimad.sa/api/ContractsPlus/v1/contracts",
                    params={"keyword": name, "page": "1", "pageSize": "20"},
                )
                resp.raise_for_status()
                data = resp.json()
                contracts = data.get("data") or data.get("results") or []
                result.matches.extend(contracts)
            except Exception as e:
                logger.warning(f"Etimad 검색 실패 ({name}): {e}")

        client.close()
        result.confidence = 0.85 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_tamer(drug: TargetDrug, keywords: dict) -> SearchResult:
    """Tamer Group 페이지에서 텍스트 매칭."""
    result = SearchResult(
        source_name="tamer_group",
        source_category="민간",
        source_url="https://tamergroup.com/sectors/distribution-healthcare-fmcg",
    )

    try:
        import httpx
        from antibot import detect as detect_antibot, AntiBotType

        resp = httpx.get(
            result.source_url,
            headers={"User-Agent": pick_ua()},
            timeout=15.0,
            follow_redirects=True,
        )

        ab = detect_antibot(resp.status_code, resp.text[:2000], dict(resp.headers))
        if ab != AntiBotType.NONE:
            result.error = f"Anti-bot 차단: {ab.value}"
            return result

        resp.raise_for_status()
        html_lower = resp.text.lower()

        search_terms = [keywords["trade_name"]] + keywords.get("ingredient_names", [])
        for term in search_terms:
            if term.lower() in html_lower:
                result.matches.append({"matched_term": term, "source": "tamer_group"})

        result.confidence = 0.60 if result.matches else 0.0

    except Exception as e:
        result.error = str(e)

    return result


def _search_noon(drug: TargetDrug, keywords: dict) -> SearchResult:
    """Noon Saudi 검색 (CF 차단 가능)."""
    result = SearchResult(
        source_name="noon_saudi",
        source_category="민간",
        source_url="https://www.noon.com/saudi-en/",
    )
    # enabled: false in sources.yaml
    result.error = "비활성 상태 (enabled: false, 법무 검토 대기)"
    return result


# ─── 수출 가능 여부 판정 ─────────────────────────────────

def _determine_feasibility(
    drug: TargetDrug,
    keywords: dict,
    results: list[SearchResult],
) -> tuple[str, str, list[str]]:
    """SFDA 검색 결과를 기반으로 수출 가능 여부 판정.

    Returns:
        (판정, 근거 문단, 근거 URL 목록)
    """
    sfda_results = [r for r in results if r.source_name == "sfda_api"]
    sfda_matches = []
    for r in sfda_results:
        sfda_matches.extend(r.matches)

    if not sfda_matches:
        return (
            "불가",
            f"SFDA 등록 데이터베이스에서 '{drug.trade_name}'의 성분({', '.join(keywords.get('ingredient_names', []))})에 "
            f"해당하는 등록 의약품을 찾을 수 없습니다. "
            f"사우디 식약처에 해당 성분이 등록되어 있지 않아 수출이 어려울 수 있습니다.",
            [],
        )

    # 성분명 매칭 확인
    target_names = [n.lower() for n in keywords.get("ingredient_names", [])]
    target_form = keywords.get("dosage_form_normalized", "")

    exact_matches = []  # 성분 + 제형 일치
    partial_matches = []  # 성분만 일치

    for match in sfda_matches:
        sci_name = (match.get("scientific_name") or "").lower()
        form = normalize_dosage_form(match.get("dosage_form") or "") or ""

        # 성분 매칭 여부
        ingredient_match = any(name in sci_name for name in target_names if name)

        if ingredient_match:
            if target_form and form == target_form:
                exact_matches.append(match)
            else:
                partial_matches.append(match)

    evidence_urls = list(set(
        m.get("source_url", "https://www.sfda.gov.sa/en/drugs-list")
        for m in (exact_matches or partial_matches or sfda_matches[:3])
    ))

    if exact_matches:
        sample = exact_matches[0]
        reg_id = sample.get("regulatory_id", "")
        price = sample.get("price_sar", "N/A")
        return (
            "가능",
            f"SFDA에 동일 성분·동일 제형의 의약품이 등록되어 있습니다. "
            f"등록번호: {reg_id}, 등록 가격: {price} SAR. "
            f"총 {len(exact_matches)}건의 정확 매칭이 확인되었습니다. "
            f"해당 성분({', '.join(keywords.get('ingredient_names', []))})이 "
            f"사우디 시장에서 유통 중이므로 수출 가능성이 높습니다.",
            evidence_urls,
        )

    if partial_matches:
        sample = partial_matches[0]
        reg_id = sample.get("regulatory_id", "")
        existing_form = sample.get("dosage_form", "N/A")
        return (
            "조건부",
            f"SFDA에 동일 성분의 의약품이 등록되어 있으나, 제형이 다릅니다. "
            f"등록번호: {reg_id}, 등록 제형: {existing_form} (대상 제형: {drug.dosage_form}). "
            f"총 {len(partial_matches)}건의 부분 매칭이 확인되었습니다. "
            f"제형 차이로 인해 추가 등록 절차가 필요할 수 있습니다.",
            evidence_urls,
        )

    # 성분 매칭 없지만 SFDA에 데이터는 있음
    return (
        "조건부",
        f"SFDA 검색에서 {len(sfda_matches)}건이 반환되었으나, "
        f"대상 성분({', '.join(keywords.get('ingredient_names', []))})과의 "
        f"직접적인 매칭이 확인되지 않았습니다. "
        f"추가 조사가 필요합니다.",
        evidence_urls,
    )


# ─── 메인 오케스트레이터 ─────────────────────────────────

def search_one_drug(drug: TargetDrug, skip_blocked: bool = True) -> AggregatedResult:
    """1개 의약품에 대해 10개 소스를 순차 검색.

    Args:
        drug: 검색할 의약품
        skip_blocked: True면 Cloudflare 차단된 소스 건너뜀

    Returns:
        AggregatedResult with all source results and feasibility determination
    """
    reg = DrugRegistry()
    keywords = reg.generate_search_keywords(drug)

    logger.info(f"타겟 검색 시작: {drug.trade_name}")
    logger.info(f"  성분명: {keywords.get('ingredient_names')}")
    logger.info(f"  제형: {keywords.get('dosage_form_normalized')}")

    t_start = time.time()
    results: list[SearchResult] = []

    # ─── 1. SFDA 의약품 (가장 중요) ───
    logger.info("[1/7] SFDA 의약품 검색...")
    t = time.time()
    r = _search_sfda(drug, keywords)
    r.search_time_sec = time.time() - t
    results.append(r)
    logger.info(f"  → {len(r.matches)}건 매칭 ({r.search_time_sec:.1f}초)")

    # ─── 2. SFDA 제약사 ───
    logger.info("[2/7] SFDA 제약사 검색...")
    t = time.time()
    r = _search_sfda_companies(drug, keywords)
    r.search_time_sec = time.time() - t
    results.append(r)
    logger.info(f"  → {len(r.matches)}건 매칭 ({r.search_time_sec:.1f}초)")

    # ─── 3. NUPCO 텐더 ───
    logger.info("[3/7] NUPCO 텐더 검색...")
    t = time.time()
    r = _search_nupco(drug, keywords)
    r.search_time_sec = time.time() - t
    results.append(r)
    logger.info(f"  → {len(r.matches)}건 매칭 ({r.search_time_sec:.1f}초)")

    # ─── 4. Etimad ───
    logger.info("[4/7] Etimad 검색...")
    t = time.time()
    r = _search_etimad(drug, keywords)
    r.search_time_sec = time.time() - t
    results.append(r)
    if r.error:
        logger.info(f"  → 건너뜀: {r.error}")
    else:
        logger.info(f"  → {len(r.matches)}건")

    # ─── 5. Nahdi (소매, Algolia API) ───
    logger.info("[5/7] Nahdi 약국 검색 (Algolia API)...")
    t = time.time()
    try:
        r = _search_nahdi(drug, keywords)
    except Exception as e:
        r = SearchResult(source_name="nahdi_web", source_category="민간",
                         source_url="https://www.nahdionline.com", error=str(e))
    r.search_time_sec = time.time() - t
    results.append(r)
    logger.info(f"  → {len(r.matches)}건 ({r.error or 'OK'})")

    # ─── 6. Whites (소매, Akinon) ───
    logger.info("[6/7] Whites 약국 검색...")
    t = time.time()
    try:
        r = _search_whites(drug, keywords)
    except Exception as e:
        r = SearchResult(source_name="whites_web", source_category="민간",
                         source_url="https://www.whites.sa", error=str(e))
    r.search_time_sec = time.time() - t
    results.append(r)
    logger.info(f"  → {len(r.matches)}건 ({r.error or 'OK'})")

    # ─── 7. 차단/비활성 소스 (상태만 기록) ───
    if skip_blocked:
        for name, url, reason in [
            ("al_dawaa_web", "https://www.al-dawaa.com", "Cloudflare 차단 (403)"),
            ("tamer_group", "https://tamergroup.com", "403 차단"),
            ("noon_saudi", "https://www.noon.com", "비활성 (enabled: false)"),
        ]:
            results.append(SearchResult(
                source_name=name,
                source_category="민간",
                source_url=url,
                error=reason,
            ))
    else:
        # 차단 소스도 시도
        logger.info("[+] Al Dawaa 검색 (CF 차단 가능)...")
        t = time.time()
        try:
            from crawlers.al_dawaa_web import AlDawaaClient
            r = _search_retail("al_dawaa_web", "https://www.al-dawaa.com/en/",
                               AlDawaaClient, drug, keywords)
        except Exception as e:
            r = SearchResult(source_name="al_dawaa_web", source_category="민간",
                             source_url="https://www.al-dawaa.com", error=str(e))
        r.search_time_sec = time.time() - t
        results.append(r)

        logger.info("[+] Tamer Group 검색...")
        t = time.time()
        r = _search_tamer(drug, keywords)
        r.search_time_sec = time.time() - t
        results.append(r)

        logger.info("[+] Noon Saudi...")
        r = _search_noon(drug, keywords)
        results.append(r)

    # ─── 판정 ───
    feasibility, rationale, evidence_urls = _determine_feasibility(drug, keywords, results)

    total_matches = sum(len(r.matches) for r in results)
    duration = time.time() - t_start

    agg = AggregatedResult(
        drug=drug,
        source_results=results,
        export_feasibility=feasibility,
        feasibility_rationale=rationale,
        feasibility_evidence_urls=evidence_urls,
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_matches=total_matches,
        search_duration_sec=round(duration, 2),
    )

    logger.info(f"검색 완료: {drug.trade_name}")
    logger.info(f"  총 매칭: {total_matches}건, 판정: {feasibility}")
    logger.info(f"  소요 시간: {duration:.1f}초")

    return agg


# ─── CLI 실행 ─────────────────────────────────────────

if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    reg = DrugRegistry()
    drugs = reg.list_drugs()

    if not drugs:
        print("drug_registry.json이 비어 있습니다. Excel에서 먼저 로드하세요.")
        sys.exit(1)

    # 인자로 drug_id 지정 가능
    drug_id = sys.argv[1] if len(sys.argv) > 1 else None

    if drug_id:
        drug = reg.get_drug(drug_id)
        if not drug:
            print(f"약품 '{drug_id}'를 찾을 수 없습니다.")
            print("사용 가능한 ID:")
            for d in drugs:
                print(f"  {d.id}: {d.trade_name}")
            sys.exit(1)
    else:
        # 목록 표시 후 선택
        print("=== 등록된 의약품 ===\n")
        for i, d in enumerate(drugs, 1):
            print(f"  {i}. [{d.drug_type}] {d.trade_name} ({d.id})")
        print()

        try:
            choice = int(input("검색할 약품 번호: ")) - 1
            drug = drugs[choice]
        except (ValueError, IndexError):
            print("잘못된 선택입니다.")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"타겟 검색: {drug.trade_name}")
    print(f"성분: {drug.ingredient}")
    print(f"{'='*60}\n")

    result = search_one_drug(drug)

    print(f"\n{'='*60}")
    print(f"결과 요약")
    print(f"{'='*60}")
    print(f"수출 가능 여부: {result.export_feasibility}")
    print(f"총 매칭: {result.total_matches}건")
    print(f"소요 시간: {result.search_duration_sec}초")
    print(f"\n근거:")
    print(f"  {result.feasibility_rationale}")

    print(f"\n소스별 결과:")
    for r in result.source_results:
        status = f"{len(r.matches)}건" if not r.error else f"에러: {r.error[:40]}"
        print(f"  [{r.source_category}] {r.source_name}: {status}")
