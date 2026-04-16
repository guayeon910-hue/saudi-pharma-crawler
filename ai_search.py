"""
ai_search.py — AI 자율 서칭 엔트리포인트

`python ai_search.py` 로 실행. 고정 크롤 소스 외에 AI가 추가 정보원을 탐색하고,
발견된 사이트에서 데이터를 추출한다.

환경변수:
  CLAUDE_API_KEY      (필수) Anthropic API 키
  SUPABASE_URL        (선택) DB 연결
  SUPABASE_SERVICE_KEY (선택) DB 인증
  DRUG_ID             (선택) 특정 약품 ID, 없으면 전체
  DRY_RUN             (선택) 'true'면 DB 저장 생략
  MAX_QUERIES         (선택) 약품당 최대 검색 쿼리 수 (기본 5)
  FETCH_DELAY         (선택) 요청 간 딜레이 초 (기본 2.0)

파이프라인:
  1. DrugRegistry에서 타겟 약품 로드
  2. 각 약품별:
     a) Phase A — 소스 발견: 검색 쿼리 생성 → URL 수집 → HTML 전처리 → LLM 판별
     b) Phase B — 데이터 추출: 유효 소스 HTML → XPath 생성 → 추출값 검증 → 합성
     c) Phase C — 통합: normalize → INN 매칭 → 이상치 검사 → DB 적재
  3. 결과 요약 출력

"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time

from dotenv import load_dotenv
load_dotenv()
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

# 프로젝트 루트 및 snippets 경로
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "assets" / "snippets"))

from drug_registry import DrugRegistry, TargetDrug
from assets.snippets.llm_client import ClaudeClient
from assets.snippets.source_discoverer import (
    discover_sources,
    DiscoveryResult,
    DiscoveredSource,
    fetch_page_html,
    fetch_page_html_ex,
)
from assets.snippets.auto_scraper import (
    generate_action_sequence,
    synthesize_scraper,
    run_scraper,
    SynthesizedScraper,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ai_search")

# AI 자율 추출 → products 적재 시 고정 메타
_AI_COUNTRY = "SA"
_AI_CURRENCY = "SAR"
_AI_SOURCE_NAME = "ai_discovered"
_AI_SOURCE_TIER = 4
_AI_MARKET_SEGMENT = "retail"

_inn_norm_instance: Any = None
_inn_norm_load_failed = False


def _get_inn_normalizer() -> Any:
    """WHO INN 정규화기 (지연 로드). 실패 시 None."""
    global _inn_norm_instance, _inn_norm_load_failed
    if _inn_norm_load_failed:
        return None
    if _inn_norm_instance is not None:
        return _inn_norm_instance
    try:
        from inn_normalizer import INNNormalizer
        n = INNNormalizer()
        n.load_reference()
        _inn_norm_instance = n
        return n
    except Exception as e:
        logger.warning("INNNormalizer 로드 실패 — INN 필드 생략: %s", e)
        _inn_norm_load_failed = True
        return None


def _stable_product_id(page_url: str, trade_name: str, scientific_name: str) -> str:
    raw = f"{_AI_COUNTRY}|{_AI_SOURCE_NAME}|{page_url}|{trade_name}|{scientific_name}"
    return "ai_discovered:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------

@dataclass
class DrugAISearchResult:
    """1약품에 대한 AI 자율 서칭 결과."""
    drug_id: str
    drug_name: str
    discovery: Optional[dict] = None        # DiscoveryResult.to_dict()
    scrapers_generated: int = 0
    records_extracted: int = 0
    records_normalized: int = 0
    records_stored: int = 0
    errors: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


@dataclass
class AISearchSummary:
    """전체 AI 자율 서칭 실행 요약."""
    drugs_processed: int = 0
    total_sources_found: int = 0
    total_sources_valid: int = 0
    total_scrapers: int = 0
    total_records: int = 0
    total_errors: int = 0
    llm_token_usage: dict = field(default_factory=dict)
    drug_results: list[DrugAISearchResult] = field(default_factory=list)
    duration_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "drugs_processed": self.drugs_processed,
            "total_sources_found": self.total_sources_found,
            "total_sources_valid": self.total_sources_valid,
            "total_scrapers": self.total_scrapers,
            "total_records": self.total_records,
            "total_errors": self.total_errors,
            "llm_token_usage": self.llm_token_usage,
            "duration_sec": round(self.duration_sec, 1),
            "drug_results": [
                {
                    "drug_id": r.drug_id,
                    "drug_name": r.drug_name,
                    "sources_found": r.discovery.get("valid_count", 0) if r.discovery else 0,
                    "records_extracted": r.records_extracted,
                    "errors": len(r.errors),
                }
                for r in self.drug_results
            ],
        }


# ---------------------------------------------------------------------------
# Phase C: 통합 — 기존 파이프라인 재사용
# ---------------------------------------------------------------------------

def _normalize_records(
    records: list[dict],
    source_domain: str,
) -> list[dict]:
    """추출 레코드를 `products` 테이블 upsert용 행으로 변환.

    `normalizer`(가격·함량·제형) + 선택적 INN 매칭을 적용한다.
    """
    try:
        from normalizer import normalize_price_sar, normalize_strength, normalize_dosage_form, normalize_scientific_name
    except ImportError:
        logger.warning("normalizer import 실패 — 저장 생략")
        return []

    inn_norm = _get_inn_normalizer()
    out: list[dict] = []

    for rec in records:
        try:
            page_url = (rec.get("_page_url") or "").strip()
            if not page_url:
                page_url = f"https://{source_domain}/"

            pname = (rec.get("product_name") or rec.get("name") or "").strip()
            sci_raw = (rec.get("active_ingredient") or rec.get("scientific_name") or "").strip()
            trade_name = pname or sci_raw
            if not trade_name:
                logger.debug("AI 추출 행 스킵: 품목명 없음 (%s)", source_domain)
                continue

            price_raw = rec.get("price") or rec.get("price_sar") or rec.get("retail_price")
            dec = normalize_price_sar(price_raw) if price_raw not in (None, "") else None
            price_local = float(dec) if dec is not None else None

            strength = normalize_strength(rec.get("strength")) if rec.get("strength") else None
            dosage_form = normalize_dosage_form(rec.get("dosage_form")) if rec.get("dosage_form") else None
            scientific_name = normalize_scientific_name(sci_raw) if sci_raw else None

            inn_name: Optional[str] = None
            inn_id: Optional[str] = None
            inn_match_type: str = "none"
            base_conf = 0.42
            if sci_raw and inn_norm is not None:
                ir = inn_norm.normalize(sci_raw)
                if ir.success and ir.inn_name:
                    inn_name = ir.inn_name
                    inn_id = ir.inn_id
                    inn_match_type = ir.match_type
                    base_conf = min(0.92, max(0.08, base_conf + ir.confidence_bonus))

            confidence = min(0.98, max(0.05, base_conf))

            product_id = _stable_product_id(page_url, trade_name, scientific_name or "")

            payload_keys = (
                "product_name", "name", "price", "price_sar", "retail_price",
                "manufacturer", "active_ingredient", "scientific_name",
                "strength", "dosage_form",
            )
            slim = {k: rec.get(k) for k in payload_keys if k in rec and rec.get(k) not in (None, "")}

            row: dict[str, Any] = {
                "product_id": product_id,
                "market_segment": _AI_MARKET_SEGMENT,
                "fob_estimated_usd": None,
                "confidence": confidence,
                "country": _AI_COUNTRY,
                "currency": _AI_CURRENCY,
                "regulatory_id": None,
                "trade_name": trade_name[:500],
                "scientific_name": scientific_name,
                "strength": strength,
                "dosage_form": dosage_form,
                "price_local": price_local,
                "manufacturer_or_marketing_company": (rec.get("manufacturer") or "")[:500] or None,
                "agent_or_supplier": None,
                "atc_code": None,
                "inn_name": inn_name,
                "inn_id": inn_id,
                "inn_match_type": inn_match_type,
                "source_url": page_url[:2000],
                "source_tier": _AI_SOURCE_TIER,
                "source_name": _AI_SOURCE_NAME,
                "raw_payload": {"ai_extract": slim, "source_domain": source_domain},
                "outlier_flagged": False,
                "anomaly_reason": None,
                "toggle_id": None,
            }
            out.append(row)

        except Exception as e:
            logger.debug("레코드 정규화 실패: %s", e)

    return out


def _store_records(
    records: list[dict],
    sb_client,
    dry_run: bool = False,
) -> int:
    """`products` 테이블에 upsert (기존 ai_discovered_products 전용 insert 대체)."""
    if dry_run or not sb_client:
        logger.info("DRY_RUN: %d건 저장 생략", len(records))
        return 0

    stored = 0
    for row in records:
        try:
            sb_client.table("products").upsert(row, on_conflict="product_id").execute()
            stored += 1
        except Exception as e:
            err_s = str(e).lower()
            logger.warning("products upsert 실패 product_id=%s: %s", row.get("product_id"), e)
            # 일부 Supabase 배포는 `scientific_name` 컬럼이 없음 — 해당 키만 제거 후 재시도
            if "scientific_name" in err_s and "does not exist" in err_s:
                slim = {k: v for k, v in row.items() if k != "scientific_name"}
                try:
                    sb_client.table("products").upsert(slim, on_conflict="product_id").execute()
                    stored += 1
                except Exception as e2:
                    logger.warning("products upsert 재시도 실패 product_id=%s: %s", row.get("product_id"), e2)

    return stored


# ---------------------------------------------------------------------------
# 기존 소스 재크롤링 (추가 경로)
# ---------------------------------------------------------------------------

def _load_existing_sources(sb_client) -> list[dict]:
    """DB에서 재크롤링 가능한 기존 유효 소스(scraper_xpaths 있는 것) 로드."""
    if not sb_client:
        return []
    try:
        resp = (
            sb_client.table("ai_discovered_sources")
            .select("*")
            .eq("country", "SA")
            .not_.is_("scraper_xpaths", "null")
            .order("last_crawled_at", desc=True, nullsfirst=True)
            .limit(100)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.warning("기존 소스 로드 실패: %s", e)
        return []


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _recrawl_source(
    source_row: dict,
    http: httpx.Client,
    sb_client,
    dry_run: bool,
    fetch_delay: float,
) -> dict:
    """기존 소스 1개 재크롤링. 저장된 scraper_xpaths로 Phase B만 실행."""
    url = source_row["url"]
    domain = source_row["domain"]
    source_id = source_row["id"]
    xpaths = source_row.get("scraper_xpaths", {})

    result = {"url": url, "domain": domain, "records": 0, "success": False, "error": None}

    try:
        html, fetch_err = fetch_page_html_ex(url, http_client=http)
        if not html:
            result["error"] = f"HTML fetch failed: {fetch_err}"
            _update_source_failure(sb_client, source_id, source_row)
            return result

        # 저장된 xpaths로 SynthesizedScraper 생성
        scraper = SynthesizedScraper(
            domain=domain,
            field_xpaths=xpaths if isinstance(xpaths, dict) else {},
            confidence=0.7,
            source_pages=1,
        )

        records = run_scraper(scraper, html)
        if records:
            for r in records:
                r["_page_url"] = url
            normalized = _normalize_records(records, domain)
            _store_records(normalized, sb_client, dry_run=dry_run)
            result["records"] = len(records)
            result["success"] = True

            # 성공: crawl_count 증가, last_crawled_at 갱신
            if sb_client and not dry_run:
                try:
                    sb_client.table("ai_discovered_sources").update({
                        "crawl_count": source_row.get("crawl_count", 0) + 1,
                        "last_crawled_at": _now_iso(),
                    }).eq("id", source_id).execute()
                except Exception as e:
                    logger.debug("소스 상태 갱신 실패: %s", e)
        else:
            result["error"] = "No records extracted"
            _update_source_failure(sb_client, source_id, source_row)

    except Exception as e:
        result["error"] = str(e)
        _update_source_failure(sb_client, source_id, source_row)
        logger.error("재크롤링 실패 %s: %s", domain, e)

    return result


def _update_source_failure(sb_client, source_id: str, source_row: dict) -> None:
    """재크롤링 실패 시 crawl_count 감소. 0 이하면 scraper_xpaths를 null로 비활성화."""
    if not sb_client:
        return
    try:
        current_count = source_row.get("crawl_count", 0)
        new_count = max(0, current_count - 1)
        update_data: dict[str, Any] = {"crawl_count": new_count}
        if new_count <= 0:
            update_data["scraper_xpaths"] = None
            logger.info("소스 비활성화: %s (crawl_count → 0)", source_row.get("domain", ""))
        sb_client.table("ai_discovered_sources").update(update_data).eq("id", source_id).execute()
    except Exception as e:
        logger.debug("소스 실패 상태 갱신 실패: %s", e)


def _save_discovered_source(
    sb_client,
    source: "DiscoveredSource",
    scraper_xpaths: dict | None,
    dry_run: bool,
) -> None:
    """발견된 소스를 ai_discovered_sources에 저장 (기존이면 업데이트)."""
    if not sb_client or dry_run:
        return
    try:
        # 기존 소스 확인
        resp = (
            sb_client.table("ai_discovered_sources")
            .select("id, crawl_count")
            .eq("url", source.url)
            .maybe_single()
            .execute()
        )
        row = {
            "country": "SA",
            "url": source.url,
            "domain": source.domain,
            "category": source.category,
            "relevance_score": source.relevance_score,
            "has_price_data": source.has_price_data,
            "has_product_listing": source.has_product_listing,
            "scraper_xpaths": scraper_xpaths,
            "last_crawled_at": _now_iso(),
        }
        if resp.data:
            row["crawl_count"] = resp.data.get("crawl_count", 0) + 1
            sb_client.table("ai_discovered_sources").update(row).eq("id", resp.data["id"]).execute()
        else:
            row["crawl_count"] = 1
            sb_client.table("ai_discovered_sources").insert(row).execute()
    except Exception as e:
        logger.debug("소스 저장 실패 %s: %s", source.url, e)


def _run_recrawl_pass(
    sb_client,
    http: httpx.Client,
    dry_run: bool,
    fetch_delay: float,
) -> dict:
    """기존 유효 소스 전체 재크롤링. 요약 dict 반환."""
    existing = _load_existing_sources(sb_client)
    if not existing:
        return {"recrawled": 0, "records": 0, "errors": 0}

    logger.info("기존 소스 %d개 재크롤링 시작", len(existing))
    total_records = 0
    total_errors = 0

    for source_row in existing:
        result = _recrawl_source(source_row, http, sb_client, dry_run, fetch_delay)
        total_records += result["records"]
        if not result["success"]:
            total_errors += 1
        logger.info(
            "  재크롤링 %s: %d건 %s",
            source_row.get("domain", "?"),
            result["records"],
            "OK" if result["success"] else f"FAIL ({result['error']})",
        )
        time.sleep(fetch_delay)

    return {"recrawled": len(existing), "records": total_records, "errors": total_errors}


# ---------------------------------------------------------------------------
# 1약품 처리
# ---------------------------------------------------------------------------

def _drug_to_info(drug: TargetDrug) -> dict:
    """TargetDrug → source_discoverer용 dict 변환."""
    return {
        "trade_name": drug.trade_name,
        "ingredients": drug.ingredient,
        "dosage_form": drug.dosage_form,
        "strength": drug.strength,
    }


def process_one_drug(
    drug: TargetDrug,
    llm: ClaudeClient,
    http: httpx.Client,
    *,
    pplx_client=None,
    sb_client=None,
    dry_run: bool = False,
    max_queries: int = 5,
    fetch_delay: float = 2.0,
) -> DrugAISearchResult:
    """1약품에 대한 전체 AI 자율 서칭.

    Phase A → Phase B → Phase C.
    """
    start = time.time()
    result = DrugAISearchResult(drug_id=drug.id, drug_name=drug.trade_name)

    drug_info = _drug_to_info(drug)

    # ── Phase A: 소스 발견 ──
    try:
        discovery = discover_sources(
            llm, http, drug_info,
            pplx_client=pplx_client,
            max_queries=max_queries,
            max_urls_per_query=5,
            fetch_delay=fetch_delay,
        )
        result.discovery = discovery.to_dict()
        logger.info("[%s] Phase A: %d 유효 소스 발견", drug.trade_name, len(discovery.valid_sources))
    except Exception as e:
        result.errors.append(f"Phase A error: {e}")
        logger.error("[%s] Phase A 실패: %s", drug.trade_name, e)
        result.duration_sec = time.time() - start
        return result

    if not discovery.valid_sources:
        logger.info("[%s] 유효 소스 없음 — 스킵", drug.trade_name)
        result.duration_sec = time.time() - start
        return result

    # ── Phase B: 데이터 추출 ──
    all_records: list[dict] = []

    for source in discovery.valid_sources:
        try:
            # HTML 다시 가져오기 (Phase A에서 이미 가져왔지만 전체 HTML 필요)
            html, fetch_err = fetch_page_html_ex(source.url, http_client=http)
            if not html:
                result.errors.append(f"HTML fetch failed: {source.url} ({fetch_err})")
                continue

            # XPath Action Sequence 생성
            seq = generate_action_sequence(llm, url=source.url, html=html)
            if not seq.is_usable:
                logger.info("[%s] %s: 스크래퍼 생성 실패 (usable=False)", drug.trade_name, source.domain)
                continue

            result.scrapers_generated += 1

            # 단일 페이지 합성 (여러 페이지 크롤링은 향후 확장)
            scraper = synthesize_scraper([seq])
            if not scraper:
                continue

            # 데이터 추출
            records = run_scraper(scraper, html)
            if records:
                for r in records:
                    r["_page_url"] = source.url
                all_records.extend(records)
                logger.info(
                    "[%s] %s: %d건 추출",
                    drug.trade_name, source.domain, len(records),
                )

                # 발견 소스 + xpaths DB 저장 (다음 실행에서 재크롤링용)
                _save_discovered_source(
                    sb_client, source,
                    scraper_xpaths=scraper.field_xpaths,
                    dry_run=dry_run,
                )

            time.sleep(fetch_delay)

        except Exception as e:
            result.errors.append(f"Phase B error ({source.domain}): {e}")
            logger.error("[%s] %s Phase B 실패: %s", drug.trade_name, source.domain, e)

    result.records_extracted = len(all_records)

    if not all_records:
        result.duration_sec = time.time() - start
        return result

    # ── Phase C: 통합 ──
    try:
        # 도메인별로 정규화
        domains = set(r.get("_source_domain", "") for r in all_records)
        normalized_all: list[dict] = []
        for domain in domains:
            domain_records = [r for r in all_records if r.get("_source_domain") == domain]
            normalized = _normalize_records(domain_records, domain)
            normalized_all.extend(normalized)

        result.records_normalized = len(normalized_all)

        # DB 저장
        stored = _store_records(normalized_all, sb_client, dry_run=dry_run)
        result.records_stored = stored

    except Exception as e:
        result.errors.append(f"Phase C error: {e}")
        logger.error("[%s] Phase C 실패: %s", drug.trade_name, e)

    result.duration_sec = time.time() - start
    return result


# ---------------------------------------------------------------------------
# 메인 엔트리포인트 (CLI · FastAPI 공용)
# ---------------------------------------------------------------------------

def run_ai_search_session(
    drug_ids: Optional[list[str]] = None,
    dry_run: bool = False,
    max_queries: int = 5,
    fetch_delay: float = 2.0,
    save_json: bool = True,
) -> dict[str, Any]:
    """AI 자율 서칭 전체 실행. `drug_ids`가 None이면 레지스트리 전체.

    배포 시 대시보드( FastAPI )에서 `asyncio.to_thread`로 호출한다.
    반환 dict: ok, error, exit_code, summary_dict, recrawl_stats
    """
    start = time.time()
    api_key = os.environ.get("CLAUDE_API_KEY", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    if not api_key:
        return {
            "ok": False,
            "error": "CLAUDE_API_KEY 미설정",
            "exit_code": 1,
            "summary_dict": None,
            "recrawl_stats": None,
        }

    sb_client = None
    if supabase_url and supabase_key:
        try:
            from supabase import create_client
            sb_client = create_client(supabase_url, supabase_key)
            logger.info("Supabase 연결됨")
        except Exception as e:
            logger.warning("Supabase 연결 실패 (계속 진행): %s", e)

    llm = ClaudeClient(api_key=api_key)
    if not llm.available:
        llm.close()
        return {
            "ok": False,
            "error": "Claude API 키 무효",
            "exit_code": 1,
            "summary_dict": None,
            "recrawl_stats": None,
        }

    pplx_client = None
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if pplx_key:
        from assets.snippets.perplexity_client import PerplexityClient
        pplx_client = PerplexityClient(api_key=pplx_key)
        logger.info("Perplexity API 활성화 (model: %s)", pplx_client.model)

    http = httpx.Client(
        timeout=httpx.Timeout(45.0, connect=25.0, read=40.0),
        follow_redirects=True,
    )

    registry = DrugRegistry()
    drugs = registry.load_from_json()
    if not drugs:
        llm.close()
        if pplx_client:
            pplx_client.close()
        http.close()
        return {
            "ok": False,
            "error": "약품 레지스트리 비어있음",
            "exit_code": 1,
            "summary_dict": None,
            "recrawl_stats": None,
        }

    if drug_ids is not None:
        id_set = set(drug_ids)
        drugs = [d for d in drugs if d.id in id_set]
        if not drugs:
            llm.close()
            if pplx_client:
                pplx_client.close()
            http.close()
            return {
                "ok": False,
                "error": "지정한 품목 ID가 레지스트리에 없음",
                "exit_code": 1,
                "summary_dict": None,
                "recrawl_stats": None,
            }

    logger.info("AI 자율 서칭 시작: %d개 약품, dry_run=%s", len(drugs), dry_run)

    recrawl_stats = _run_recrawl_pass(sb_client, http, dry_run, fetch_delay)
    if recrawl_stats["recrawled"] > 0:
        logger.info(
            "재크롤링 완료: %d개 소스, %d건 추출, %d 에러",
            recrawl_stats["recrawled"], recrawl_stats["records"], recrawl_stats["errors"],
        )

    summary = AISearchSummary()

    for drug in drugs:
        logger.info("━" * 60)
        logger.info("약품: %s (%s)", drug.trade_name, drug.id)
        logger.info("━" * 60)

        result = process_one_drug(
            drug, llm, http,
            pplx_client=pplx_client,
            sb_client=sb_client,
            dry_run=dry_run,
            max_queries=max_queries,
            fetch_delay=fetch_delay,
        )

        summary.drug_results.append(result)
        summary.drugs_processed += 1

        if result.discovery:
            summary.total_sources_found += result.discovery.get("urls_found", 0)
            summary.total_sources_valid += result.discovery.get("valid_count", 0)

        summary.total_scrapers += result.scrapers_generated
        summary.total_records += result.records_extracted
        summary.total_errors += len(result.errors)

    summary.llm_token_usage = llm.usage.summary()
    if pplx_client:
        summary.llm_token_usage["perplexity"] = pplx_client.usage.summary()
    summary.duration_sec = time.time() - start

    llm.close()
    if pplx_client:
        pplx_client.close()
    http.close()

    output_data = summary.to_dict()
    output_data["recrawl_stats"] = recrawl_stats

    if save_json:
        output_path = ROOT / "reports" / "ai_search_result.json"
        output_path.parent.mkdir(exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        logger.info("결과 저장: %s", output_path)

    exit_code = 0 if summary.total_errors == 0 else 1
    err_msg: Optional[str] = None
    if exit_code != 0:
        err_msg = f"단계 오류 {summary.total_errors}건"
        snippets: list[str] = []
        for dr in summary.drug_results:
            for ex in dr.errors[:5]:
                snippets.append(ex[:240])
                if len(snippets) >= 8:
                    break
            if len(snippets) >= 8:
                break
        if snippets:
            err_msg += ": " + " | ".join(snippets)
    return {
        "ok": exit_code == 0,
        "error": err_msg,
        "exit_code": exit_code,
        "summary_dict": output_data,
        "recrawl_stats": recrawl_stats,
        "summary": summary,
    }


def main() -> int:
    """AI 자율 서칭 메인 함수. exit code 반환."""
    drug_id = os.environ.get("DRUG_ID", "")
    drug_ids = [drug_id] if drug_id else None
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    max_queries = int(os.environ.get("MAX_QUERIES", "5"))
    fetch_delay = float(os.environ.get("FETCH_DELAY", "2.0"))

    out = run_ai_search_session(
        drug_ids=drug_ids,
        dry_run=dry_run,
        max_queries=max_queries,
        fetch_delay=fetch_delay,
        save_json=True,
    )

    if not out["ok"] and out.get("error"):
        logger.error("%s", out["error"])
        return int(out.get("exit_code", 1))

    summary = out.get("summary")
    if summary is not None:
        _print_summary(summary)
    recrawl_stats = out.get("recrawl_stats") or {}
    if recrawl_stats.get("recrawled", 0) > 0:
        print(
            f"\n  재크롤링: {recrawl_stats['recrawled']}개 소스 → "
            f"{recrawl_stats['records']}건 추출 ({recrawl_stats['errors']} 에러)"
        )

    return int(out.get("exit_code", 1))


def _print_summary(summary: AISearchSummary) -> None:
    """실행 결과 요약 출력."""
    print("\n" + "=" * 60)
    print("AI 자율 서칭 결과 요약")
    print("=" * 60)
    print(f"  약품 수:       {summary.drugs_processed}")
    print(f"  소스 발견:     {summary.total_sources_found} URL → {summary.total_sources_valid} 유효")
    print(f"  스크래퍼 생성: {summary.total_scrapers}")
    print(f"  레코드 추출:   {summary.total_records}")
    print(f"  에러:          {summary.total_errors}")
    print(f"  소요 시간:     {summary.duration_sec:.1f}초")
    print(f"  LLM 토큰:     {summary.llm_token_usage}")
    print()

    for r in summary.drug_results:
        status = "✓" if not r.errors else "✗"
        sources = r.discovery.get("valid_count", 0) if r.discovery else 0
        print(f"  {status} {r.drug_name:<30} | 소스 {sources} | 레코드 {r.records_extracted} | {r.duration_sec:.1f}s")

    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
