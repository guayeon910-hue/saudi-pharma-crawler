"""
frontend/server.py -- 사우디 제약 크롤러 대시보드 서버

대시보드 UI 서빙 + SSE 실시간 이벤트 + 단일 품목 파이프라인 실행.
기존 targeted_search / report_generator 모듈을 재활용한다.

실행:
    cd <project_root>
    python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ── 프로젝트 루트 계산 ──
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "products.db"

# dotenv
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# snippets + 루트 경로
sys.path.insert(0, str(ROOT / "assets" / "snippets"))
sys.path.insert(0, str(ROOT))

from drug_registry import DrugRegistry, TargetDrug
from targeted_search import search_one_drug, AggregatedResult, _annotate_match
from frontend.buyer_sources import curated_buyer_candidates, registrable_domain_from_url
from frontend.dashboard_sites import SITES, get_initial_states
from frontend.fob_private import run_private_pipeline, run_public_pipeline

logger = logging.getLogger("frontend.server")

# ═══════════════════════════════════════════════════════════════════════════
# 전역 상태
# ═══════════════════════════════════════════════════════════════════════════

_state: dict = {
    "events": [],
    "lock": None,  # lifespan에서 생성
    "running": False,
}

_site_states: dict[str, dict] = {}

_analysis_cache: dict = {
    "result": None,
    "running": False,
}

_pipeline_tasks: dict[str, dict] = {}

_report_cache: dict = {
    "running": False,
    "latest_pdf": None,
}

_p2_report_cache: dict = {"latest": None}
_p3_report_cache: dict = {"latest": None}

# 레지스트리 + 클라이언트 (lazy)
_registry = DrugRegistry()
_llm_client = None
_pplx_client = None
_sb_client = None


class P3ProspectsRequest(BaseModel):
    product_key: Optional[str] = None
    trade_name: Optional[str] = None
    ingredients: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None


class P3WhiteSpaceRequest(BaseModel):
    """Phase 3 빈틈 분석 요청."""
    target_inn: str
    target_atc_level3: Optional[str] = None   # 주어지면 inn 보다 우선
    min_atc_products: int = 3
    top_n: int = 15
    product_limit: int = 2000                 # DB fetch 상한 (성능/메모리 보호)
    include_tender_power: bool = True         # Phase 4: 공공조달 실적 스코어 병합


class P1CompetitorMapRequest(BaseModel):
    """Phase 5: 경쟁사 유통 에이전트 역추적 요청."""
    product_key: Optional[str] = None         # drug_registry 의 제품 키
    trade_name: Optional[str] = None          # 직접 지정 시
    ingredients: Optional[str] = None         # 직접 지정 시 (쉼표 구분 가능)
    target_inn: Optional[str] = None          # INN 으로 직접 필터
    target_atc_level3: Optional[str] = None   # ATC L3 필터 (선택)
    include_tender_power: bool = True         # Tender 실적 결합 여부
    top_n: int = 15
    product_limit: int = 1500


def _reset_site_states() -> None:
    global _site_states
    _site_states = get_initial_states()


# ═══════════════════════════════════════════════════════════════════════════
# Lazy client getters
# ═══════════════════════════════════════════════════════════════════════════

def _get_supabase():
    global _sb_client
    if _sb_client is not None:
        return _sb_client
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if url and key:
        try:
            from supabase import create_client
            _sb_client = create_client(url, key)
            logger.info("Supabase 연결됨")
        except Exception as e:
            logger.warning("Supabase 연결 실패: %s", e)
    return _sb_client


def _get_llm():
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    api_key = (
        os.environ.get("CLAUDE_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    ).strip()
    if api_key:
        try:
            from llm_client import ClaudeClient
            _llm_client = ClaudeClient(api_key=api_key)
            if _llm_client.available:
                logger.info("Claude API 연결됨")
            else:
                _llm_client = None
        except Exception as e:
            logger.warning("Claude 초기화 실패: %s", e)
    return _llm_client


def _perplexity_key_configured() -> bool:
    """UI용: 루트 `.env`에 키가 있는지(서버 프로세스 환경)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return bool(os.environ.get("PERPLEXITY_API_KEY", "").strip())


def _get_pplx():
    global _pplx_client
    if _pplx_client is not None:
        return _pplx_client
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if pplx_key:
        try:
            from perplexity_client import PerplexityClient
            _pplx_client = PerplexityClient(api_key=pplx_key)
            logger.info("Perplexity API 연결됨")
        except Exception as e:
            logger.warning("Perplexity 초기화 실패: %s", e)
    return _pplx_client


# ═══════════════════════════════════════════════════════════════════════════
# 이벤트 시스템
# ═══════════════════════════════════════════════════════════════════════════

async def _emit(event: dict) -> None:
    """이벤트를 SSE 버퍼에 적재. 사이트 상태도 자동 갱신."""
    event["ts"] = datetime.now(timezone.utc).isoformat()
    async with _state["lock"]:
        _state["events"].append(event)
        if len(_state["events"]) > 500:
            _state["events"] = _state["events"][-400:]

    if event.get("phase") == "site_progress" and event.get("site_key"):
        sk = event["site_key"]
        if sk in _site_states:
            _site_states[sk] = {
                "status": event.get("status", "pending"),
                "message": event.get("message", ""),
                "ts": event["ts"],
            }


def _emit_sync(event: dict) -> None:
    """동기 컨텍스트에서 이벤트 적재 (백그라운드 스레드용)."""
    event["ts"] = datetime.now(timezone.utc).isoformat()
    _state["events"].append(event)
    if len(_state["events"]) > 500:
        _state["events"] = _state["events"][-400:]

    if event.get("phase") == "site_progress" and event.get("site_key"):
        sk = event["site_key"]
        if sk in _site_states:
            _site_states[sk] = {
                "status": event.get("status", "pending"),
                "message": event.get("message", ""),
                "ts": event["ts"],
            }


# ═══════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["lock"] = asyncio.Lock()
    _reset_site_states()
    _static_probe = Path(__file__).resolve().parent / "static"
    logger.info(
        "Frontend server 시작 · PORT=%s cwd=%s static_dir=%s exists=%s",
        os.environ.get("PORT"),
        Path.cwd(),
        _static_probe,
        _static_probe.is_dir(),
    )
    yield
    logger.info("Frontend server 종료")


app = FastAPI(title="Saudi Pharma Dashboard", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════
# SSE 스트림
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stream")
async def sse_stream():
    async def generator():
        last = len(_state["events"])
        while True:
            await asyncio.sleep(0.12)
            current_len = len(_state["events"])
            if current_len > last:
                new_events = _state["events"][last:current_len]
                for ev in new_events:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                last = current_len

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# 크롤/상태 API
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/run")
async def run_full_crawl():
    if _state["running"]:
        raise HTTPException(409, "이미 크롤링 실행 중")
    _state["running"] = True
    _reset_site_states()
    await _emit({"phase": "pipeline", "message": "전체 크롤링 시작"})

    async def _crawl_bg():
        try:
            drugs = _registry.list_drugs()
            for drug in drugs:
                await _emit({"phase": "log", "message": f"[{drug.trade_name}] 크롤링 시작"})
                try:
                    agg = search_one_drug(drug)
                    sr_dict = agg.to_dict()
                    sb_run = _get_supabase()
                    if sb_run:
                        try:
                            from assets.snippets.pipeline_persist import (
                                persist_aggregated_search_to_supabase,
                            )

                            n_ins = persist_aggregated_search_to_supabase(sb_run, agg)
                            if n_ins:
                                await _emit({
                                    "phase": "log",
                                    "message": f"[{drug.trade_name}] Supabase 적재 {n_ins}건",
                                })
                        except Exception as ex:
                            logger.warning("[%s] DB 적재 실패: %s", drug.trade_name, ex)
                    for sr in sr_dict.get("source_results", []):
                        sname = sr.get("source_name", "")
                        for sd in SITES:
                            if sd["key"] == sname or sd["key"] in sname or sname in sd["key"]:
                                await _emit({
                                    "phase": "site_progress",
                                    "site_key": sd["key"],
                                    "status": "error" if sr.get("error") else "done",
                                    "message": sr.get("error") or f"{len(sr.get('matches', []))}건",
                                })
                                break
                    total = sr_dict.get("total_matches", 0)
                    await _emit({"phase": "log", "message": f"[{drug.trade_name}] 완료: {total}건"})
                except Exception as e:
                    await _emit({"phase": "log", "message": f"[{drug.trade_name}] 실패: {e}"})
            await _emit({"phase": "pipeline", "message": "전체 크롤링 완료 ✓"})
        finally:
            _state["running"] = False

    asyncio.create_task(_crawl_bg())
    return {"status": "started"}


@app.get("/api/status")
async def get_status():
    return {
        "running": _state["running"],
        "event_count": len(_state["events"]),
    }


@app.get("/api/sites")
async def get_sites():
    result = []
    # DB에서 소스별 크롤링 통계 조회 (Supabase 등에 적재된 데이터 반영)
    db_stats: dict[str, dict] = {}
    sb = _get_supabase()
    if sb:
        try:
            resp = (
                sb.table("products")
                .select("source_name, trade_name")
                .eq("country", "SA")
                .execute()
            )
            if resp.data:
                for row in resp.data:
                    sn = row.get("source_name", "")
                    db_stats.setdefault(sn, {"count": 0})
                    db_stats[sn]["count"] += 1
        except Exception:
            pass

    for s in SITES:
        state = _site_states.get(s["key"], {"status": "pending", "message": "", "ts": ""})
        # 로컬 상태가 pending이면 DB 통계로 보강
        if state["status"] == "pending" and not state["message"]:
            for db_key, stats in db_stats.items():
                if s["key"] in db_key or db_key in s["key"] or s["name"].lower().replace(" ", "_") in db_key.lower():
                    state = {
                        "status": "done",
                        "message": f"DB: {stats['count']}건",
                        "ts": "",
                    }
                    break
        result.append({**s, **state})
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 제품 목록
# ═══════════════════════════════════════════════════════════════════════════

def _row_ingredient_label(rec: dict) -> str:
    """`products` 행 성분 표시. 일부 Supabase 스키마는 `scientific_name` 대신 `inn_name`만 둠."""
    return (rec.get("inn_name") or rec.get("scientific_name") or "").strip()


def _row_matches_ingredient_key(row: dict, ingredient_key: str) -> bool:
    """성분 문자열이 행의 INN/과학명/raw_payload 등에 부분 일치하는지 (스키마 차이 대응)."""
    k = (ingredient_key or "").strip().lower()
    if len(k) < 2:
        return False
    for fld in ("inn_name", "scientific_name"):
        v = row.get(fld)
        if v and k in str(v).lower():
            return True
    rp = row.get("raw_payload")
    if isinstance(rp, dict):
        for sub in ("scientific_name", "inn_name", "active_ingredient", "ingredient", "inn"):
            v = rp.get(sub)
            if v and k in str(v).lower():
                return True
    elif isinstance(rp, str) and k in rp.lower():
        return True
    return False


def _fetch_products_by_ingredient_flexible(sb, ingredient_key: str, limit: int = 50) -> tuple[list[dict], Optional[str]]:
    """Supabase `products` 컬럼명이 배포마다 달라도 동일 성분 행을 최대한 찾는다.

    1) `inn_name` ilike → 2) `scientific_name` ilike → 3) SA 최근 행을 넓게 받아 메모리 필터.
    실패 시 ( [], 오류문자열 ).
    """
    key = (ingredient_key or "").strip()
    if not key:
        return [], None

    # ── 서버측 필터 (빠름) ──
    for col in ("inn_name", "scientific_name"):
        try:
            resp = (
                sb.table("products")
                .select("*")
                .eq("country", "SA")
                .ilike(col, f"%{key}%")
                .order("crawled_at", desc=True)
                .limit(limit)
                .execute()
            )
            data = resp.data or []
            if data:
                return data, None
        except Exception as e:
            logger.debug("products %s 필터 조회 생략: %s", col, e)

    # ── 폴백: 컬럼 부재·필터 오류 시 최근 SA 행만 받아 매칭 ──
    try:
        resp = (
            sb.table("products")
            .select("*")
            .eq("country", "SA")
            .order("crawled_at", desc=True)
            .limit(1500)
            .execute()
        )
        rows = resp.data or []
        matched = [r for r in rows if _row_matches_ingredient_key(r, key)][:limit]
        return matched, None
    except Exception as e:
        logger.warning("products 성분 폴백 조회 실패: %s", e)
        return [], str(e)


def _row_price_local(row: dict) -> Optional[float]:
    """가격 컬럼명 차이 대응."""
    for fld in ("price_local", "price_sar", "price", "retail_price"):
        v = row.get(fld)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _looks_like_source_url(value: object) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def _price_sample(
    *,
    trade_name: object,
    price: object,
    source: object = "",
    source_url: object = "",
    currency: object = "SAR",
    ingredient: object = "",
    strength: object = "",
    sample_type: object = "",
    observed_at: object = "",
) -> Optional[dict]:
    try:
        price_f = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if price_f <= 0:
        return None

    url = str(source_url or "").strip()
    verified = _looks_like_source_url(url)
    return {
        "trade_name": str(trade_name or ""),
        "price": price_f,
        "currency": str(currency or "SAR"),
        "source": str(source or ""),
        "source_url": url,
        "ingredient": str(ingredient or ""),
        "strength": str(strength or ""),
        "type": str(sample_type or ""),
        "observed_at": str(observed_at or ""),
        "is_verified_price": verified,
        "verification_status": "observed_with_source_url" if verified else "observed_missing_source_url",
    }


def _price_lookup_key(trade_name: object, price: object) -> tuple[str, float] | None:
    try:
        price_f = round(float(price), 4)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    name = re.sub(r"\s+", " ", str(trade_name or "").strip().lower())
    if not name or price_f <= 0:
        return None
    return (name, price_f)


def _known_price_source_urls(search_data: Optional[dict]) -> dict[tuple[str, float], str]:
    lookup: dict[tuple[str, float], str] = {}
    if not isinstance(search_data, dict):
        return lookup

    rows = search_data.get("rows") if search_data.get("source") == "database" else []
    for row in rows or []:
        price = row.get("price") if row.get("price") is not None else row.get("price_local")
        key = _price_lookup_key(row.get("trade_name"), price)
        url = str(row.get("source_url") or "").strip()
        if key and _looks_like_source_url(url):
            lookup[key] = url

    for sr in search_data.get("source_results", []) or []:
        sr_url = str(sr.get("source_url") or "").strip()
        for match in sr.get("matches", []) or []:
            price = match.get("price_sar") or match.get("price") or match.get("retail_price")
            key = _price_lookup_key(match.get("trade_name") or match.get("name"), price)
            url = str(match.get("source_url") or match.get("url") or sr_url).strip()
            if key and _looks_like_source_url(url):
                lookup[key] = url
    return lookup


def _annotate_price_verification(samples: list[dict], search_data: Optional[dict]) -> None:
    lookup = _known_price_source_urls(search_data)
    checked_at = datetime.now(timezone.utc).isoformat()
    for sample in samples:
        key = _price_lookup_key(sample.get("trade_name"), sample.get("price"))
        url = str(sample.get("source_url") or "").strip()
        if not _looks_like_source_url(url) and key in lookup:
            url = lookup[key]
            sample["source_url"] = url
        verified = _looks_like_source_url(url)
        sample["is_verified_price"] = verified
        sample["verification_status"] = (
            "observed_with_source_url" if verified else "observed_missing_source_url"
        )
        sample["verification_checked_at"] = checked_at


def _is_health_functional_product(drug: TargetDrug) -> bool:
    text = " ".join(
        str(v or "")
        for v in (drug.drug_type, drug.trade_name, drug.ingredient, drug.dosage_form)
    ).lower()
    markers = (
        "health functional",
        "functional food",
        "inner beauty",
        "nutraceutical",
        "dietary supplement",
        "supplement",
        "agatri",
        "agastache",
        "baechohyang",
    )
    return any(marker in text for marker in markers)


def _analysis_market_context(drug: TargetDrug) -> str:
    if _is_health_functional_product(drug):
        return (
            "Treat this target as a health functional food / nutraceutical ingredient, "
            "not as a prescription drug. For Saudi Arabia, analyze the SFDA food, "
            "dietary supplement, health claim, import, labeling, halal, and GCC "
            "distribution route. Do not mark the product unsuitable only because it "
            "does not appear in the Saudi drug register. If direct Saudi retail price "
            "data is unavailable, estimate cautiously from comparable skin health, "
            "collagen, beauty-from-within, and supplement products, and state the "
            "evidence gap."
        )
    return (
        "Treat this target as a pharmaceutical product. Use Saudi drug registration, "
        "procurement, pharmacy price, distribution, and comparable-ingredient evidence "
        "as the primary decision basis."
    )


def _shape_record_for_dashboard(rec: dict) -> dict:
    """JSON 스냅샷(`price`, `outlier`)과 Supabase `products`(`price_local`, `outlier_flagged`) 병합 시 UI 필드 통일."""
    out = dict(rec)
    if out.get("price") is None and out.get("price_local") is not None:
        try:
            out["price"] = float(out["price_local"])
        except (TypeError, ValueError):
            pass
    if out.get("price_sar") is None:
        local_price = _row_price_local(out)
        if local_price is not None and (out.get("currency") or "SAR") == "SAR":
            out["price_sar"] = local_price
    if "outlier" not in out and "outlier_flagged" in out:
        out["outlier"] = bool(out.get("outlier_flagged"))
    if _row_price_local(out) is not None:
        verified_price = _looks_like_source_url(out.get("source_url"))
        out["is_verified_price"] = verified_price
        out["verification_status"] = (
            "observed_with_source_url" if verified_price else "observed_missing_source_url"
        )
    return out


def _source_category_for_report(source_name: str, market_segment: str = "") -> str:
    """Supabase products 행을 1공정 보고서의 source_category로 변환."""
    key = (source_name or "").strip().lower()
    segment = (market_segment or "").strip().lower()
    if any(tok in key for tok in ("nahdi", "whites", "dawaa", "rosheta", "noon", "tamer", "ai_discovered")):
        return "민간"
    if any(tok in key for tok in ("sdi", "sfda", "nupco", "etimad")):
        return "공공조달"
    if segment in {"retail", "wholesale"}:
        return "민간"
    if segment == "tender":
        return "공공조달"
    return "규제/제품정보"


def _database_rows_to_report_source_results(rows: list[dict]) -> list[dict]:
    """DB 재조회 결과를 report_generator가 집계할 수 있는 source_results로 변환."""
    def _safe_conf(value: object) -> Optional[float]:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    grouped: dict[tuple[str, str], list[dict]] = {}
    for raw in rows or []:
        row = _shape_record_for_dashboard(raw)
        source_name = str(row.get("source_name") or "products")
        category = _source_category_for_report(source_name, row.get("market_segment", ""))
        price_sar = _row_price_local(row) if (row.get("currency") or "SAR") == "SAR" else None
        match = {
            "trade_name": row.get("trade_name", ""),
            "name": row.get("trade_name", ""),
            "scientific_name": row.get("scientific_name") or row.get("inn_name") or "",
            "strength": row.get("strength", ""),
            "dosage_form": row.get("dosage_form", ""),
            "price_sar": price_sar,
            "price": price_sar,
            "manufacturer": row.get("manufacturer_or_marketing_company") or "",
            "source_url": row.get("source_url", ""),
            "confidence": row.get("confidence"),
            "match_quality": "ingredient",
        }
        grouped.setdefault((source_name, category), []).append(match)

    source_results: list[dict] = []
    for (source_name, category), matches in sorted(grouped.items()):
        source_url = next((m.get("source_url") for m in matches if m.get("source_url")), "")
        confs = [_safe_conf(m.get("confidence")) for m in matches]
        source_results.append(
            {
                "source_name": source_name,
                "source_category": category,
                "source_url": source_url,
                "matches": matches,
                "confidence": max((c for c in confs if c is not None), default=0.85),
                "error": None,
            }
        )
    return source_results


def _parse_ai_product_price(val: object) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@app.get("/api/products")
async def get_products():
    """레지스트리 품목 + dashboard_data.json SFDA 레코드를 병합 반환."""
    from dataclasses import asdict

    drugs = _registry.list_drugs()
    drug_list = [asdict(d) for d in drugs]

    # dashboard_data.json에서 SFDA 레코드 병합 (이상치/신뢰도 데이터 포함)
    json_path = ROOT / "dashboard_data.json"
    sfda_records: list[dict] = []
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            sfda_records = [_shape_record_for_dashboard(r) for r in data.get("records", [])]
        except Exception:
            pass

    # Supabase DB도 시도
    sb = _get_supabase()
    if sb:
        try:
            resp = (
                sb.table("products")
                .select("*")
                .eq("country", "SA")
                .order("crawled_at", desc=True)
                .limit(200)
                .execute()
            )
            rows = resp.data or []
            if rows:
                for row in rows:
                    rp = row.get("raw_payload")
                    if isinstance(rp, str):
                        try:
                            row["raw_payload"] = json.loads(rp)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    sfda_records.append(_shape_record_for_dashboard(row))
        except Exception as e:
            logger.warning("DB products 조회 실패: %s", e)

        # AI 자율 서칭 추출분 (ai_discovered_products) — 메인 products와 별도 테이블이므로 여기서 합쳐 표시
        try:
            air = (
                sb.table("ai_discovered_products")
                .select("*")
                .eq("country", "SA")
                .order("crawled_at", desc=True)
                .limit(100)
                .execute()
            )
            for row in air.data or []:
                conf = row.get("confidence")
                try:
                    conf_f = float(conf) if conf is not None else 0.5
                except (TypeError, ValueError):
                    conf_f = 0.5
                sfda_records.append({
                    "product_id": f"ai_discovered:{row.get('id', '')}",
                    "trade_name": row.get("product_name") or "(AI 추출)",
                    "price": _parse_ai_product_price(row.get("price")),
                    "confidence": conf_f,
                    "outlier": False,
                    "inn_name": row.get("inn_name"),
                    "dosage_form": row.get("strength") or "",
                    "source_name": row.get("source") or "ai_discovered",
                    "anomaly_reason": None,
                })
        except Exception as e:
            logger.debug("ai_discovered_products 병합 생략: %s", e)

    return {
        "drugs": drug_list,
        "records": sfda_records,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 분석 API (전체)
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/analyze")
async def run_analysis(
    use_perplexity: bool = Query(True),
    force_refresh: bool = Query(False),
):
    if _analysis_cache["running"]:
        raise HTTPException(409, "분석이 이미 실행 중")

    if _analysis_cache["result"] and not force_refresh:
        return {"status": "cached", "message": "이전 분석 결과 존재. force_refresh=true로 재실행."}

    _analysis_cache["running"] = True
    await _emit({"phase": "analysis", "message": "전체 분석 시작"})

    async def _analyze_bg():
        try:
            drugs = _registry.list_drugs()
            results = {}
            for drug in drugs:
                await _emit({"phase": "log", "message": f"[{drug.trade_name}] 분석 중..."})
                analysis = _analyze_single_product(drug, use_perplexity)
                results[drug.id] = analysis
                verdict = analysis.get("verdict", "분석실패")
                await _emit({"phase": "log", "message": f"[{drug.trade_name}] → {verdict}"})

            _analysis_cache["result"] = results
            await _emit({"phase": "analysis", "message": f"전체 분석 완료: {len(results)}개 품목"})
        except Exception as e:
            await _emit({"phase": "log", "message": f"분석 오류: {e}"})
        finally:
            _analysis_cache["running"] = False

    asyncio.create_task(_analyze_bg())
    return {"status": "started"}


@app.get("/api/analyze/status")
async def get_analysis_status():
    return {
        "running": _analysis_cache["running"],
        "has_result": _analysis_cache["result"] is not None,
    }


@app.get("/api/analyze/result")
async def get_analysis_result():
    if _analysis_cache["running"]:
        return JSONResponse({"status": "running"}, status_code=202)
    if not _analysis_cache["result"]:
        raise HTTPException(404, "분석 결과 없음")
    return _analysis_cache["result"]


# ═══════════════════════════════════════════════════════════════════════════
# 단일 품목 파이프라인
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/pipeline/{product_key}")
async def start_pipeline(product_key: str):
    existing = _pipeline_tasks.get(product_key, {})
    if existing.get("status") == "running":
        raise HTTPException(409, "이미 실행 중")

    drug = _registry.get_drug(product_key)
    if not drug:
        raise HTTPException(404, f"품목 '{product_key}' 없음")

    _pipeline_tasks[product_key] = {
        "status": "running",
        "step": "db_load",
        "step_label": "DB 조회·크롤링",
        "result": None,
        "refs": None,
        "pdf": None,
        "started_at": time.time(),
    }

    asyncio.create_task(_run_pipeline_for_product(product_key, drug))
    return {"status": "started", "product_key": product_key}


@app.get("/api/pipeline/{product_key}/status")
async def get_pipeline_status(product_key: str):
    task = _pipeline_tasks.get(product_key)
    if not task:
        raise HTTPException(404, "파이프라인 상태 없음")
    return {
        "status": task["status"],
        "step": task["step"],
        "step_label": task["step_label"],
        "has_result": task.get("result") is not None,
        "ref_count": len(task.get("refs") or []),
        "has_pdf": task.get("pdf") is not None,
    }


@app.get("/api/pipeline/{product_key}/result")
async def get_pipeline_result(product_key: str):
    task = _pipeline_tasks.get(product_key)
    if not task:
        raise HTTPException(404, "파이프라인 결과 없음")
    if task["status"] == "running":
        return JSONResponse({"status": "running", "step": task["step"]}, status_code=202)
    return {
        "result": task.get("result"),
        "refs": task.get("refs"),
        "pdf": task.get("pdf"),
        "ai_sources": _ai_sources_cache.get(product_key, []),
        "perplexity_key_set": _perplexity_key_configured(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 파이프라인 내부: 4단계 실행
# ═══════════════════════════════════════════════════════════════════════════

async def _run_pipeline_for_product(product_key: str, drug: TargetDrug) -> None:
    task = _pipeline_tasks[product_key]
    try:
        # ── Step 1: 크롤 + DB 재조회 (프론트 progress: db_load) ──
        task["step"] = "db_load"
        task["step_label"] = "DB 조회·크롤링"
        _state["running"] = True
        _reset_site_states()
        await _emit({"phase": "pipeline", "step": "db_load",
                      "message": f"[{drug.trade_name}] 크롤링 시작"})

        search_result: Optional[dict] = None
        sb = _get_supabase()
        crawl_agg: Optional[AggregatedResult] = None

        # ── Step 1a: 크롤 (고정 소스 search_one_drug) ──
        try:
            crawl_agg = search_one_drug(drug)
            sr_dict = crawl_agg.to_dict()
            search_result = sr_dict
            for sr in sr_dict.get("source_results", []):
                sname = sr.get("source_name", "")
                for sd in SITES:
                    if sd["key"] == sname or sd["key"] in sname or sname in sd["key"]:
                        _emit_sync({
                            "phase": "site_progress",
                            "site_key": sd["key"],
                            "status": "error" if sr.get("error") else "done",
                            "message": sr.get("error") or f"{len(sr.get('matches', []))}건",
                        })
                        break
            total = sr_dict.get("total_matches", 0)
            await _emit({"phase": "pipeline", "step": "db_load",
                          "message": f"크롤링 완료: {total}건"})
        except Exception as e:
            await _emit({"phase": "log", "message": f"크롤링 실패: {e}"})
            search_result = {"total_matches": 0, "source_results": [], "error": str(e)}
            crawl_agg = None

        # 크롤 매칭 → Supabase `products` 적재 (이전에는 메모리만 반환되어 DB 건수가 안 늘었음)
        if sb and crawl_agg:
            try:
                from assets.snippets.pipeline_persist import persist_aggregated_search_to_supabase

                n_ins = await asyncio.to_thread(
                    persist_aggregated_search_to_supabase, sb, crawl_agg
                )
                if n_ins:
                    await _emit({"phase": "pipeline", "step": "db_load",
                                  "message": f"Supabase 적재: {n_ins}건"})
            except Exception as e:
                logger.warning("크롤 결과 DB 적재 실패: %s", e)
                await _emit({"phase": "log", "message": f"DB 적재 실패: {e}"})

        # ── Step 1b: DB 재조회 (적재 반영 후 동일 성분 스냅샷, Claude 입력용) ──
        if sb:
            ingredient_key = drug.ingredient.split("+")[0].strip()
            rows, db_err = _fetch_products_by_ingredient_flexible(sb, ingredient_key, limit=50)
            if db_err:
                await _emit({"phase": "log", "message": f"DB 재조회 실패: {db_err}"})
            elif rows:
                search_result = {"source": "database", "rows": rows, "count": len(rows)}
                await _emit({"phase": "pipeline", "step": "db_load",
                              "message": f"DB 재조회: 총 {len(rows)}건"})
            else:
                await _emit({"phase": "pipeline", "step": "db_load",
                              "message": "DB 재조회: 동일 성분 레코드 없음 — 크롤 결과로 분석"})

        _state["running"] = False

        # ── Step 2: Analyze (Claude) ──
        task["step"] = "analyze"
        task["step_label"] = "Claude 분석"
        await _emit({"phase": "pipeline", "step": "analyze",
                      "message": f"[{drug.trade_name}] Claude 분석 시작"})

        analysis = _analyze_single_product(drug, use_perplexity=True, search_data=search_result)
        task["result"] = analysis
        verdict = analysis.get("verdict", "분석실패")
        await _emit({"phase": "pipeline", "step": "analyze",
                      "message": f"분석 완료: {verdict}"})

        # ── Step 3: References (Perplexity) ──
        task["step"] = "refs"
        task["step_label"] = "논문 검색"
        await _emit({"phase": "pipeline", "step": "refs",
                      "message": "참고문헌 검색 중..."})

        refs = _fetch_references(drug)
        task["refs"] = refs
        await _emit({"phase": "pipeline", "step": "refs",
                      "message": f"참고문헌 {len(refs)}건 수집"})

        # ── Step 4: Report ──
        task["step"] = "report"
        task["step_label"] = "보고서 생성"
        await _emit({"phase": "pipeline", "step": "report",
                      "message": "보고서 생성 중..."})

        pdf_path = _generate_report_for_pipeline(drug, analysis, refs, search_result)
        task["pdf"] = pdf_path
        if pdf_path:
            await _emit({"phase": "pipeline", "step": "report",
                          "message": f"보고서 생성 완료: {pdf_path}"})

        # ── Done ──
        task["status"] = "done"
        task["step"] = "done"
        task["step_label"] = "완료"
        await _emit({"phase": "pipeline", "step": "done",
                      "message": f"파이프라인 완료 ✓"})

    except Exception as e:
        task["status"] = "error"
        task["step"] = "error"
        task["step_label"] = str(e)[:100]
        await _emit({"phase": "log", "message": f"오류: {e}"})
        _state["running"] = False


# ═══════════════════════════════════════════════════════════════════════════
# 분석 로직: Claude로 verdict/confidence 판정 + 가격 비교
# ═══════════════════════════════════════════════════════════════════════════

def _analyze_single_product(
    drug: TargetDrug,
    use_perplexity: bool = True,
    search_data: Optional[dict] = None,
) -> dict:
    """단일 품목 분석. Claude API로 진출 적합성 판정 + 가격 예상치."""

    base_result = {
        "product_id": drug.id,
        "trade_name": drug.trade_name,
        "inn": drug.ingredient,
        "ingredient": drug.ingredient,
        "dosage_form": drug.dosage_form,
        "strength": drug.strength,
        "drug_type": drug.drug_type,
        "verdict": "분석실패",
        "confidence": 0.0,
        "rationale": "",
        "key_factors": [],
        "hs_code": None,
        "case_type": None,
        "pillars": {},
        "strategy": {},
        "analysis_error": None,
        "analysis_model": None,
        "claude_model_id": None,
        "claude_error_detail": None,
        "price_comparison": None,
    }

    # 가격 비교 데이터 수집
    price_data = _collect_price_data(drug, search_data)
    base_result["price_comparison"] = price_data

    llm = _get_llm()
    if not llm:
        base_result["analysis_error"] = "no_api_key"
        base_result["verdict"] = "API 키 미설정"
        return base_result

    # 크롤링 데이터 요약
    crawl_summary = _summarize_crawl_data(drug, search_data)
    price_summary = _summarize_price_data(price_data)
    market_context = _analysis_market_context(drug)

    prompt = f"""사우디아라비아(KSA) 의약품 시장 진출 적합성을 분석해주세요.

## 대상 품목
- 품목명: {drug.trade_name}
- 성분: {drug.ingredient}
- 함량: {drug.strength}
- 제형: {drug.dosage_form}
- 종류: {drug.drug_type}

## Product-specific decision context
{market_context}

## 크롤링 수집 데이터 요약
{crawl_summary}

## 가격 비교 데이터
{price_summary}

## 분석 요청
아래 JSON 형식으로만 응답하세요. 모든 설명 필드는 한국어 자연어만 사용하고 JSON/코드/키 이름을 본문에 넣지 마세요.
{{
  "verdict": "적합" | "조건부" | "부적합",
  "confidence": 0.0~1.0,
  "hs_code": "HS 3004 같은 문자열" | null,
  "case_type": "Case A" | "Case B" | "Case C" | null,
  "rationale": "판정 근거 (한국어, 3~5문장)",
  "key_factors": ["핵심 요인 1", "핵심 요인 2"],
  "pillars": {{
    "market_medical": "시장·의료·역학 (2~4문장)",
    "regulation": "규제·등록 (2~4문장)",
    "trade": "무역·관세 (2~4문장)",
    "procurement": "조달·입찰 (2~4문장)",
    "distribution": "유통·파트너 (2~4문장)"
  }},
  "strategy": {{
    "entry_channels": "진입 채널 전략 (단계별)",
    "price_positioning": "가격 포지셔닝",
    "distribution_partners": "유통 파트너",
    "risk_conditions": "리스크·조건 및 ※ 보조 메모 가능"
  }},
  "estimated_price_range": {{
    "min_sar": 숫자 또는 null,
    "max_sar": 숫자 또는 null,
    "avg_sar": 숫자 또는 null,
    "basis": "가격 산정 근거 설명"
  }}
}}

판정 기준:
- 적합: SFDA 등록 동일/유사 성분 존재 + 가격 데이터 확인
- 조건부: 유사 성분 존재하나 제형/함량 차이 또는 데이터 불충분
- 부적합: 관련 데이터 전무 또는 규제 장벽 확인"""

    try:
        from llm_client import MODEL_HAIKU
        resp = llm.ask(prompt, model=MODEL_HAIKU, max_tokens=4096)
        parsed = resp.parse_json()

        base_result["verdict"] = parsed.get("verdict", "분석실패")
        base_result["confidence"] = float(parsed.get("confidence", 0.0))
        base_result["rationale"] = parsed.get("rationale", "")
        base_result["key_factors"] = parsed.get("key_factors", [])
        hc = parsed.get("hs_code")
        base_result["hs_code"] = hc if isinstance(hc, str) and hc.strip() else None
        ct = parsed.get("case_type")
        base_result["case_type"] = ct if isinstance(ct, str) and ct.strip() else None
        pl = parsed.get("pillars")
        base_result["pillars"] = pl if isinstance(pl, dict) else {}
        st = parsed.get("strategy")
        base_result["strategy"] = st if isinstance(st, dict) else {}
        base_result["analysis_model"] = "claude"
        base_result["claude_model_id"] = resp.model

        epr = parsed.get("estimated_price_range")
        if epr:
            base_result["price_comparison"]["estimated"] = epr

    except Exception as e:
        base_result["analysis_error"] = "claude_failed"
        base_result["claude_error_detail"] = str(e)[:200]
        logger.warning("Claude 분석 실패 [%s]: %s", drug.id, e)

    return base_result


def _collect_price_data(drug: TargetDrug, search_data: Optional[dict] = None) -> dict:
    """DB + 크롤 결과에서 가격 데이터를 수집하여 비교 테이블 생성."""
    prices: list[dict] = []
    competitor_prices: list[dict] = []

    sb = _get_supabase()
    ingredient_key = drug.ingredient.split("+")[0].strip()
    ingredient_names = _registry.generate_search_keywords(drug).get("ingredient_names", [ingredient_key])

    # DB에서 동일 성분 가격 조회 (컬럼명·필터 호환)
    if sb:
        try:
            same_rows, same_err = _fetch_products_by_ingredient_flexible(sb, ingredient_key, limit=80)
            if same_err:
                logger.warning("가격 동일성분 DB 조회: %s", same_err)
            else:
                for row in same_rows:
                    annotated_row = _annotate_match(_shape_record_for_dashboard(row), ingredient_names)
                    if not annotated_row or annotated_row.get("match_quality") != "ingredient":
                        continue
                    pl = _row_price_local(row)
                    if pl is None:
                        continue
                    prices.append({
                        "trade_name": row.get("trade_name", ""),
                        "price": pl,
                        "currency": row.get("currency", "SAR"),
                        "source": row.get("source_name", ""),
                        "source_url": row.get("source_url") or row.get("url") or row.get("product_url") or "",
                        "ingredient": _row_ingredient_label(row),
                        "strength": row.get("strength", ""),
                        "type": "동일성분",
                    })

            # 동일 제형의 다른 약도 비교 대상으로 가져오기
            form_key = drug.dosage_form.lower().replace(".", "").strip()
            if form_key:
                resp2 = None
                try:
                    resp2 = (
                        sb.table("products")
                        .select("*")
                        .eq("country", "SA")
                        .ilike("dosage_form", f"%{form_key}%")
                        .not_.is_("price_local", "null")
                        .order("price_local", desc=False)
                        .limit(20)
                        .execute()
                    )
                except Exception:
                    try:
                        resp2 = (
                            sb.table("products")
                            .select("*")
                            .eq("country", "SA")
                            .ilike("dosage_form", f"%{form_key}%")
                            .order("crawled_at", desc=True)
                            .limit(80)
                            .execute()
                        )
                    except Exception as e2:
                        logger.debug("유사제형 dosage_form 필터 생략: %s", e2)
                cand: list[dict] = []
                if resp2 and resp2.data:
                    cand = [r for r in resp2.data if _row_price_local(r) is not None]
                if not cand and form_key:
                    try:
                        rwide = (
                            sb.table("products")
                            .select("*")
                            .eq("country", "SA")
                            .order("crawled_at", desc=True)
                            .limit(500)
                            .execute()
                        )
                        for r in rwide.data or []:
                            df = (r.get("dosage_form") or "").lower()
                            if form_key in df and _row_price_local(r) is not None:
                                cand.append(r)
                            if len(cand) >= 20:
                                break
                    except Exception as e3:
                        logger.debug("유사제형 폴백 조회 실패: %s", e3)
                for row in cand[:20]:
                    competitor_prices.append({
                        "trade_name": row.get("trade_name", ""),
                        "price": _row_price_local(row),
                        "ingredient": _row_ingredient_label(row),
                        "source": row.get("source_name", ""),
                        "source_url": row.get("source_url") or row.get("url") or row.get("product_url") or "",
                        "type": "유사제형",
                    })
        except Exception as e:
            logger.warning("가격 데이터 DB 조회 실패: %s", e)

    # search_data에서 추가 가격 추출
    if search_data and isinstance(search_data, dict):
        for sr in search_data.get("source_results", []):
            for m in sr.get("matches", []):
                if m.get("match_quality") and m.get("match_quality") != "ingredient":
                    continue
                p = m.get("price_sar") or m.get("price") or m.get("retail_price")
                if p is not None:
                    try:
                        prices.append({
                            "trade_name": m.get("trade_name", m.get("name", "")),
                            "price": float(p),
                            "currency": "SAR",
                            "source": sr.get("source_name", ""),
                            "source_url": m.get("source_url") or m.get("url") or sr.get("url") or "",
                            "type": "크롤링",
                        })
                    except (ValueError, TypeError):
                        pass

    _annotate_price_verification(prices, search_data)
    _annotate_price_verification(competitor_prices, search_data)

    verified_prices = [p for p in prices if p.get("is_verified_price")]
    verified_competitors = [p for p in competitor_prices if p.get("is_verified_price")]
    price_values = [p["price"] for p in verified_prices if p.get("price")]
    comp_values = [p["price"] for p in verified_competitors if p.get("price")]

    return {
        "same_ingredient": prices,
        "competitors": competitor_prices[:10],
        "summary": {
            "count": len(price_values),
            "raw_count": len(prices),
            "verified_count": len(verified_prices),
            "unverified_count": max(0, len(prices) - len(verified_prices)),
            "min": min(price_values) if price_values else None,
            "max": max(price_values) if price_values else None,
            "avg": round(sum(price_values) / len(price_values), 2) if price_values else None,
            "competitor_avg": round(sum(comp_values) / len(comp_values), 2) if comp_values else None,
            "competitor_verified_count": len(verified_competitors),
        },
        "estimated": None,
    }


def _summarize_crawl_data(drug: TargetDrug, search_data: Optional[dict] = None) -> str:
    """크롤 데이터를 Claude 프롬프트용 텍스트로 요약."""
    if not search_data:
        return "수집된 크롤링 데이터 없음"

    lines = []
    if search_data.get("source") == "database":
        rows = search_data.get("rows", [])
        lines.append(f"- DB 조회: {len(rows)}건 (동일/유사 성분)")
        for r in rows[:5]:
            tn = r.get("trade_name", "")
            pl = r.get("price_local")
            p = r.get("price") if r.get("price") is not None else pl
            src = r.get("source_name", "")
            lines.append(f"  · {tn} | 가격: {p} SAR | 소스: {src}")
    else:
        total = search_data.get("total_matches", 0)
        feas = search_data.get("export_feasibility", "")
        lines.append(f"- 총 매치: {total}건 | 수출 가능 판정: {feas}")
        for sr in search_data.get("source_results", [])[:5]:
            sname = sr.get("source_name", "")
            mc = len(sr.get("matches", []))
            err = sr.get("error", "")
            lines.append(f"  · {sname}: {mc}건{' (오류: ' + err[:50] + ')' if err else ''}")

    return "\n".join(lines) if lines else "데이터 없음"


def _summarize_price_data_legacy(price_data: dict) -> str:
    """가격 비교 데이터를 텍스트로 요약."""
    s = price_data.get("summary", {})
    if not s.get("count"):
        return "가격 데이터 없음 — 동일 성분 제품의 사우디 시장 가격을 찾지 못했습니다."

    lines = [
        f"- 동일 성분 가격 데이터: {s['count']}건",
        f"- 가격 범위: {s['min']:.2f} ~ {s['max']:.2f} SAR" if s.get("min") else "",
        f"- 평균 가격: {s['avg']:.2f} SAR" if s.get("avg") else "",
    ]
    if s.get("competitor_avg"):
        lines.append(f"- 유사 제형 경쟁제품 평균: {s['competitor_avg']:.2f} SAR")

    top_prices = price_data.get("same_ingredient", [])[:5]
    if top_prices:
        lines.append("- 주요 비교 제품:")
        for p in top_prices:
            lines.append(f"  · {p.get('trade_name', 'N/A')} — {p.get('price', 'N/A')} SAR ({p.get('source', '')})")

    return "\n".join(l for l in lines if l)


# ═══════════════════════════════════════════════════════════════════════════
# 참고문헌 검색 (Perplexity)
# ═══════════════════════════════════════════════════════════════════════════

def _summarize_price_data(price_data: dict) -> str:
    """Summarize only source-verified prices for the Claude prompt."""
    s = price_data.get("summary", {})
    if not s.get("count"):
        raw_count = int(s.get("raw_count") or 0)
        if raw_count:
            return (
                f"출처 URL로 검증된 가격 데이터 없음. "
                f"원시 가격 후보 {raw_count}건은 출처 확인 전 데이터로만 보관합니다."
            )
        return "가격 데이터 없음 - 동일 성분 제품의 사우디 시장 가격을 찾지 못했습니다."

    lines = [
        f"- 출처 URL 검증 가격 데이터: {s['count']}건",
        f"- 원시 가격 후보: {s.get('raw_count', s['count'])}건 / 미검증 제외: {s.get('unverified_count', 0)}건",
        f"- 가격 범위: {s['min']:.2f} ~ {s['max']:.2f} SAR" if s.get("min") else "",
        f"- 평균 가격: {s['avg']:.2f} SAR" if s.get("avg") else "",
    ]
    if s.get("competitor_avg"):
        lines.append(f"- 유사 제형 경쟁제품 평균: {s['competitor_avg']:.2f} SAR")

    top_prices = [
        p for p in price_data.get("same_ingredient", [])
        if p.get("is_verified_price")
    ][:5]
    if top_prices:
        lines.append("- 주요 검증 비교 제품:")
        for p in top_prices:
            lines.append(
                f"  - {p.get('trade_name', 'N/A')} / {p.get('price', 'N/A')} SAR "
                f"({p.get('source', '')}, {p.get('source_url', '')})"
            )

    return "\n".join(l for l in lines if l)


def _fetch_references(drug: TargetDrug) -> list[dict]:
    """Perplexity로 논문/참고자료 검색."""
    pplx = _get_pplx()
    if not pplx:
        return []

    refs: list[dict] = []
    try:
        queries = [
            f"{drug.ingredient} Saudi Arabia market pharmaceutical",
            f"{drug.trade_name} SFDA registration approval",
        ]
        for q in queries:
            try:
                result = pplx.search(
                    trade_name=drug.trade_name,
                    ingredients=drug.ingredient,
                    dosage_form=drug.dosage_form,
                    strength=drug.strength,
                )
                if isinstance(result, dict):
                    for src in result.get("sources", []):
                        refs.append({
                            "title": src.get("title", src.get("domain", "Unknown")),
                            "url": src.get("url", ""),
                            "source": src.get("category", "web"),
                            "reason": src.get("reason", "AI 검색 결과"),
                        })
                break
            except Exception:
                continue
    except Exception as e:
        logger.warning("참고문헌 검색 실패 [%s]: %s", drug.id, e)

    # 중복 URL 제거
    seen_urls: set[str] = set()
    unique_refs = []
    for r in refs:
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            unique_refs.append(r)

    return unique_refs


# ═══════════════════════════════════════════════════════════════════════════
# 보고서 생성
# ═══════════════════════════════════════════════════════════════════════════

def _generate_report_for_pipeline(
    drug: TargetDrug,
    analysis: dict,
    refs: list[dict],
    search_data: Optional[dict],
) -> Optional[str]:
    """기존 report_generator 활용하여 DOCX 보고서 생성."""
    try:
        from report_generator import generate_report

        if search_data and search_data.get("source") == "database":
            db_rows = search_data.get("rows", [])
            source_results = _database_rows_to_report_source_results(
                db_rows if isinstance(db_rows, list) else []
            )
            report_data = {
                "total_matches": search_data.get("count", 0),
                "source_results": source_results,
                "export_feasibility": analysis.get("verdict", ""),
                "feasibility_rationale": analysis.get("rationale", ""),
                "search_duration_sec": 0,
            }
        elif search_data and isinstance(search_data, dict):
            report_data = search_data.copy()
            report_data["export_feasibility"] = analysis.get("verdict", report_data.get("export_feasibility", ""))
            report_data["feasibility_rationale"] = analysis.get("rationale", report_data.get("feasibility_rationale", ""))
        else:
            report_data = {
                "total_matches": 0,
                "source_results": [],
                "export_feasibility": analysis.get("verdict", ""),
                "feasibility_rationale": analysis.get("rationale", ""),
                "search_duration_sec": 0,
            }

        dur = 0.0
        if search_data and isinstance(search_data, dict):
            try:
                dur = float(search_data.get("search_duration_sec") or 0)
            except (TypeError, ValueError):
                dur = 0.0

        report_meta = {
            "collection_finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "collection_method": os.environ.get(
                "REPORT_COLLECTION_METHOD",
                "L1 정적 seed (사용자 검증) + L2 조건부 크롤러",
            ),
            "freshness_note": os.environ.get(
                "REPORT_FRESHNESS_NOTE",
                "Phase 2 로드맵 — 해법 C (AI 2단계 게이트)",
            ),
            "llm_body_note": os.environ.get(
                "REPORT_LLM_BODY_NOTE",
                "규칙 기반 템플릿 + Claude Haiku 분석",
            ),
            "search_duration_sec": dur,
        }

        try:
            fx = _fetch_exchange_rates()
        except Exception:
            fx = None

        output_path = generate_report(
            drug,
            report_data,
            analysis=analysis,
            refs=refs or [],
            report_meta=report_meta,
            exchange_rates=fx,
        )
        return output_path.name
    except Exception as e:
        logger.warning("보고서 생성 실패 [%s]: %s", drug.id, e)
        return None


@app.post("/api/report")
async def create_report(run_analysis: bool = Query(False)):
    if _report_cache["running"]:
        raise HTTPException(409, "보고서 생성 중")
    _report_cache["running"] = True
    await _emit({"phase": "report", "message": "보고서 생성 시작"})

    async def _report_bg():
        try:
            drugs = _registry.list_drugs()
            for drug in drugs:
                sr = search_one_drug(drug) if run_analysis else None
                sd = sr.to_dict() if sr else None
                analysis = _analyze_single_product(drug, search_data=sd)
                refs = _fetch_references(drug)
                pdf = _generate_report_for_pipeline(drug, analysis, refs, sd)
                if pdf:
                    _report_cache["latest_pdf"] = pdf
                    await _emit({"phase": "report", "message": f"[{drug.trade_name}] 보고서 완료"})
            await _emit({"phase": "report", "message": "전체 보고서 생성 완료 ✓"})
        except Exception as e:
            await _emit({"phase": "log", "message": f"보고서 오류: {e}"})
        finally:
            _report_cache["running"] = False

    asyncio.create_task(_report_bg())
    return {"status": "started"}


@app.get("/api/report/status")
async def get_report_status():
    reports_dir = ROOT / "reports"
    pdf_count = len(list(reports_dir.glob("*.docx"))) if reports_dir.exists() else 0
    latest = _report_cache.get("latest_pdf")
    return {
        "running": _report_cache["running"],
        "latest_pdf": latest,
        "latest_report": latest,
        "pdf_count": pdf_count,
        "report_count": pdf_count,
    }


@app.get("/api/report/download")
async def download_report(filename: Optional[str] = Query(None)):
    reports_dir = ROOT / "reports"
    if filename:
        filepath = reports_dir / filename
    elif _report_cache.get("latest_pdf"):
        filepath = reports_dir / _report_cache["latest_pdf"]
    else:
        files = sorted(reports_dir.glob("*.docx"), key=lambda f: f.stat().st_mtime, reverse=True) if reports_dir.exists() else []
        if not files:
            raise HTTPException(404, "보고서 파일 없음")
        filepath = files[0]

    if not filepath.exists():
        raise HTTPException(404, "파일 없음")
    if not filepath.resolve().is_relative_to(reports_dir.resolve()):
        raise HTTPException(403, "접근 거부")

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filepath.name,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 거시지표
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/macro")
async def get_macro():
    """거시지표 카드 데이터. dashboard_data.json 또는 DB 기반."""
    json_path = ROOT / "dashboard_data.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            records = data.get("records", [])
            normal = sum(1 for r in records if not r.get("outlier"))
            outlier = sum(1 for r in records if r.get("outlier"))
            prices = [r["price"] for r in records if r.get("price") is not None]
            return {
                "total_products": len(records),
                "total_sources": len(SITES),
                "avg_price_sar": round(sum(prices) / len(prices), 2) if prices else None,
                "outlier_rate": round(outlier / len(records) * 100, 1) if records else 0,
                "normal_count": normal,
                "outlier_count": outlier,
                "data_freshness": data.get("generated_at", ""),
            }
        except Exception:
            pass

    sb = _get_supabase()
    if sb:
        try:
            resp = sb.table("products").select("price_local, trade_name", count="exact").eq("country", "SA").not_.is_("price_local", "null").execute()
            count = resp.count or 0
            prices = [float(r["price_local"]) for r in (resp.data or []) if r.get("price_local") is not None]
            return {
                "total_products": count,
                "total_sources": len(SITES),
                "avg_price_sar": round(sum(prices) / len(prices), 2) if prices else None,
                "outlier_rate": 0,
                "normal_count": count,
                "outlier_count": 0,
                "data_freshness": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            pass

    return {
        "total_products": 0,
        "total_sources": len(SITES),
        "avg_price_sar": None,
        "outlier_rate": 0,
        "normal_count": 0,
        "outlier_count": 0,
        "data_freshness": "",
    }


# ═══════════════════════════════════════════════════════════════════════════
# AI 발견 소스
# ═══════════════════════════════════════════════════════════════════════════

_ai_sources_cache: dict[str, list[dict]] = {}


@app.get("/api/ai-sources")
async def get_all_ai_sources():
    """AI 자율서칭으로 발견한 전체 URL 목록 (DB 기반)."""
    all_sources: list[dict] = []
    sb = _get_supabase()
    if sb:
        try:
            resp = (
                sb.table("ai_discovered_sources")
                .select("*")
                .eq("country", "SA")
                .order("relevance_score", desc=True)
                .limit(100)
                .execute()
            )
            all_sources = resp.data or []
        except Exception as e:
            logger.warning("AI 소스 전체 조회 실패: %s", e)

    # 캐시에 있는 것도 병합
    for key, sources in _ai_sources_cache.items():
        for s in sources:
            if s not in all_sources:
                all_sources.append(s)

    return {"sources": all_sources, "count": len(all_sources)}


@app.get("/api/ai-sources/{product_key}")
async def get_ai_sources(product_key: str):
    """특정 품목의 AI 발견 URL 목록."""
    if product_key in _ai_sources_cache:
        return {"product_key": product_key, "sources": _ai_sources_cache[product_key]}

    sb = _get_supabase()
    if sb:
        try:
            resp = (
                sb.table("ai_discovered_sources")
                .select("*")
                .eq("country", "SA")
                .order("relevance_score", desc=True)
                .limit(50)
                .execute()
            )
            sources = resp.data or []
            return {"product_key": product_key, "sources": sources}
        except Exception as e:
            logger.warning("AI 소스 조회 실패: %s", e)

    return {"product_key": product_key, "sources": []}


# ═══════════════════════════════════════════════════════════════════════════
# Perplexity 정적 데이터 분석
# ═══════════════════════════════════════════════════════════════════════════

_perplexity_cache: dict = {"result": None, "running": False}


@app.post("/api/perplexity/analyze")
async def perplexity_analyze(product_key: Optional[str] = Query(None)):
    """Perplexity Sonar로 사우디 시장 정적 데이터 분석."""
    if _perplexity_cache["running"]:
        raise HTTPException(409, "이미 분석 실행 중")

    pplx = _get_pplx()
    if not pplx:
        raise HTTPException(503, "PERPLEXITY_API_KEY 미설정")

    drug = None
    if product_key:
        drug = _registry.get_drug(product_key)

    _perplexity_cache["running"] = True
    await _emit({"phase": "perplexity", "message": "Perplexity 시장 분석 시작"})

    async def _pplx_bg():
        try:
            target = drug or _registry.list_drugs()[0] if _registry.list_drugs() else None
            if not target:
                _perplexity_cache["result"] = {"error": "분석 대상 품목 없음"}
                return

            result = pplx.search(
                trade_name=target.trade_name,
                ingredients=target.ingredient,
                dosage_form=target.dosage_form,
                strength=target.strength,
            )

            analysis = {
                "product_key": target.id,
                "trade_name": target.trade_name,
                "sources": [],
                "insights": "",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if isinstance(result, dict):
                analysis["sources"] = result.get("sources", [])
                analysis["insights"] = result.get("summary", result.get("text", ""))
                # AI 소스 캐시에 저장
                _ai_sources_cache[target.id] = result.get("sources", [])

            _perplexity_cache["result"] = analysis
            source_count = len(analysis["sources"])
            await _emit({"phase": "perplexity",
                          "message": f"Perplexity 분석 완료: {source_count}개 소스 발견"})
        except Exception as e:
            _perplexity_cache["result"] = {"error": str(e)}
            await _emit({"phase": "log", "message": f"Perplexity 분석 실패: {e}"})
        finally:
            _perplexity_cache["running"] = False

    asyncio.create_task(_pplx_bg())
    return {"status": "started"}


@app.get("/api/perplexity/result")
async def get_perplexity_result():
    if _perplexity_cache["running"]:
        return JSONResponse({"status": "running"}, status_code=202)
    if not _perplexity_cache["result"]:
        raise HTTPException(404, "분석 결과 없음")
    return _perplexity_cache["result"]


# ═══════════════════════════════════════════════════════════════════════════
# DB 크롤링 통계 (Supabase 적재분)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/db-stats")
async def get_db_stats():
    """Supabase에 쌓인 크롤링 데이터 통계."""
    sb = _get_supabase()
    if not sb:
        return {"connected": False, "message": "Supabase 미연결", "stats": {}}

    try:
        # 소스별 건수 — 컬럼 목록 최소화 후 실패 시 * (배포 스키마 차이)
        try:
            resp = sb.table("products").select("source_name, price_local, trade_name").eq("country", "SA").limit(8000).execute()
        except Exception:
            resp = sb.table("products").select("*").eq("country", "SA").limit(8000).execute()
        rows = resp.data or []

        source_stats: dict[str, dict] = {}
        total_with_price = 0
        for r in rows:
            sn = r.get("source_name", "unknown")
            source_stats.setdefault(sn, {"count": 0, "with_price": 0})
            source_stats[sn]["count"] += 1
            if _row_price_local(r) is not None:
                source_stats[sn]["with_price"] += 1
                total_with_price += 1

        # AI 발견 소스 건수
        ai_count = 0
        try:
            ai_resp = sb.table("ai_discovered_sources").select("url", count="exact").eq("country", "SA").execute()
            ai_count = ai_resp.count or len(ai_resp.data or [])
        except Exception:
            pass

        return {
            "connected": True,
            "total_records": len(rows),
            "total_with_price": total_with_price,
            "source_stats": source_stats,
            "ai_sources_count": ai_count,
        }
    except Exception as e:
        return {"connected": False, "message": str(e), "stats": {}}


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard Data (Chart.js용 원본 데이터)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/dashboard-data")
async def get_dashboard_data():
    """dashboard_data.json 원본 반환."""
    json_path = ROOT / "dashboard_data.json"
    if not json_path.exists():
        raise HTTPException(404, "dashboard_data.json 없음")
    return JSONResponse(json.loads(json_path.read_text(encoding="utf-8")))


# ═══════════════════════════════════════════════════════════════════════════
# 프론트엔드 호환: 환율 · API 키 상태 · 뉴스 (dashboard UI)
# ═══════════════════════════════════════════════════════════════════════════


# ── 환율 캐시 (yfinance 레이트 리밋 회피) ──────────────────────────
#   버튼을 빠르게 연타해도 1분 이내엔 캐시된 값을 반환.
_exchange_cache: dict = {
    "data": None,       # 마지막 성공 응답
    "fetched_at": 0.0,  # epoch seconds
    "ttl": 60.0,        # 1분 (fast_info 호출 횟수 제어)
}

# Fallback 근사값 — yfinance 전체 실패 시 사용 (서버가 죽지 않도록)
_EXCHANGE_FALLBACK = {
    "sar_krw": 392.64,   # USD/KRW 1472 ÷ USD/SAR 3.7515 기준 근사
    "usd_krw": 1472.0,
    "sar_usd": 0.2667,   # SAR 은 1 USD = 3.75 SAR 페그 고정
}

# Frankfurter 미지원 통화(SAR) 보완: SAMA USD 페그 근사 (Render 등에서 yfinance 지연·차단 대비)
_SAR_PER_USD_PEG = 3.75


def _fetch_exchange_http_ecb() -> dict | None:
    """Frankfurter(ECB) USD→KRW + SAR/USD 공식 페그. 클라우드에서 빠르게 응답."""
    try:
        import httpx

        r = httpx.get(
            "https://api.frankfurter.app/latest",
            params={"from": "USD", "to": "KRW"},
            timeout=6.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        krw = float((r.json().get("rates") or {}).get("KRW") or 0)
        if krw <= 0:
            return None
        sar_per_usd = _SAR_PER_USD_PEG
        sar_krw = krw / sar_per_usd
        sar_usd = 1.0 / sar_per_usd
        return {
            "ok": True,
            "sar_krw": round(sar_krw, 2),
            "usd_krw": round(krw, 2),
            "sar_usd": round(sar_usd, 4),
            "source": "http_ecb",
        }
    except Exception as e:
        logger.warning("Frankfurter 환율 조회 실패: %s", e)
        return None


def _fetch_exchange_rates() -> dict:
    """환율: HTTP(ECB 경유) 우선 → yfinance. 실패 시 캐시·fallback.

    ─ Yahoo Finance 에 SARKRW=X 티커가 없어 USD 경유 역산.
    ─ Render 등은 Yahoo 쪽이 지연·무응답인 경우가 많아 HTTP를 먼저 시도한다.
    """
    http_data = _fetch_exchange_http_ecb()
    if http_data is not None:
        return http_data

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 미설치 — fallback 값 반환")
        return {**_EXCHANGE_FALLBACK, "source": "fallback", "ok": False}

    try:
        usd_krw = float(yf.Ticker("USDKRW=X").fast_info.last_price)
        usd_sar = float(yf.Ticker("USDSAR=X").fast_info.last_price)
        if usd_krw <= 0 or usd_sar <= 0:
            raise ValueError(f"비정상 시세: USD/KRW={usd_krw}, USD/SAR={usd_sar}")

        sar_krw = usd_krw / usd_sar           # 파생: 1 SAR = ? KRW
        sar_usd = 1.0 / usd_sar               # 파생: 1 SAR = ? USD
        return {
            "ok": True,
            "sar_krw": round(sar_krw, 2),
            "usd_krw": round(usd_krw, 2),
            "sar_usd": round(sar_usd, 4),
            "source": "yfinance",
        }
    except Exception as e:
        logger.warning("yfinance 환율 조회 실패: %s", e)
        # 이전 캐시가 있으면 캐시값, 없으면 fallback
        if _exchange_cache["data"]:
            return {**_exchange_cache["data"], "source": "cache_stale"}
        return {**_EXCHANGE_FALLBACK, "source": "fallback", "ok": False}


@app.get("/api/exchange")
async def api_exchange():
    """대시보드 환율 카드 — yfinance 실시간 시세 (1분 캐시).

    반환 필드 (app.js loadExchange 가 소비):
      - sar_krw : 1 SAR 당 원화 (메인 숫자)
      - usd_krw : 1 USD 당 원화 (서브)
      - sar_usd : 1 SAR 당 USD (서브)
      - source  : http_ecb | yfinance | cache | cache_stale | fallback
    """
    now = time.time()
    cached = _exchange_cache["data"]
    if cached and (now - _exchange_cache["fetched_at"] < _exchange_cache["ttl"]):
        return {**cached, "source": "cache"}

    # 동기 I/O — 스레드 + 전체 타임아웃 (yfinance 무응답 시 UI 영구 대기 방지)
    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_fetch_exchange_rates),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        logger.warning("환율 조회 타임아웃")
        if _exchange_cache["data"]:
            data = {**_exchange_cache["data"], "source": "cache_stale"}
        else:
            data = {**_EXCHANGE_FALLBACK, "source": "fallback", "ok": False}

    # 성공 응답만 캐시
    if data.get("ok") and data.get("source") in ("yfinance", "http_ecb"):
        _exchange_cache["data"] = data
        _exchange_cache["fetched_at"] = now

    return data


@app.get("/api/keys/status")
async def api_keys_status():
    llm = _get_llm()
    return {
        "claude": llm is not None,
        "perplexity": _perplexity_key_configured(),
    }


@app.get("/api/news")
async def api_news():
    """시장 뉴스 카드. DB 소스가 있으면 사용, 없으면 안내용 항목."""
    items: list[dict] = []
    sb = _get_supabase()
    if sb:
        try:
            resp = (
                sb.table("ai_discovered_sources")
                .select("title,url,source,crawled_at")
                .eq("country", "SA")
                .order("crawled_at", desc=True)
                .limit(12)
                .execute()
            )
            for row in resp.data or []:
                t = row.get("title") or row.get("url") or "Source"
                u = row.get("url") or ""
                items.append(
                    {
                        "title": str(t)[:200],
                        "link": str(u) if u else "",
                        "source": str(row.get("source") or "DB"),
                        "date": str(row.get("crawled_at") or "")[:10],
                    }
                )
        except Exception:
            pass
    if not items:
        items = [
            {
                "title": "SFDA — Saudi Food & Drug Authority",
                "link": "https://www.sfda.gov.sa/en",
                "source": "SFDA",
                "date": "",
            },
            {
                "title": "Vision 2030 — Healthcare & life sciences",
                "link": "https://www.vision2030.gov.sa",
                "source": "Vision 2030",
                "date": "",
            },
        ]
    return {"ok": True, "items": items}


def _p3_base_domain_for_dedupe(url: str) -> str:
    """등록 가능 도메인 기준으로 중복 판별(.com.sa 같은 2단계 suffix 보정 포함)."""
    try:
        return registrable_domain_from_url(url.strip())
    except Exception:
        return ""


def _load_ai_discovered_sources_for_p3(sb) -> list[dict]:
    """Supabase ai_discovered_sources — 국가 필터 없음(SA 외 JP 등 팀 등록 URL 포함).

    기존 /api/news 는 country=SA 만 조회하지만, P3는 사용자가 넣은 해외 전시·디렉터리도 후보로 올린다.
    """
    rows: list[dict] = []
    try:
        resp = (
            sb.table("ai_discovered_sources")
            .select(
                "url,domain,category,relevance_score,"
                "has_price_data,has_product_listing,country,created_at"
            )
            .order("created_at", desc=True)
            .limit(120)
            .execute()
        )
        rows = resp.data or []
    except Exception as exc:
        logger.warning("P3 ai_discovered_sources 조회 실패: %s", exc)
        return []

    out: list[dict] = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        dom = (row.get("domain") or "").strip().lower() or urlparse(url).netloc.lower()
        title = dom or url
        rs = row.get("relevance_score")
        score = 0.96
        if rs is not None:
            try:
                score = max(0.96, min(1.0, float(rs)))
            except (TypeError, ValueError):
                pass
        out.append(
            {
                "url": url,
                "domain": dom,
                "title": title[:300],
                "description": "팀 DB(ai_discovered_sources) 등록 소스",
                "relevance_score": score,
                "category": str(row.get("category") or "registered"),
                "has_price_data": bool(row.get("has_price_data")),
                "has_product_listing": bool(row.get("has_product_listing")),
                "language": "",
            }
        )
    return out


def _normalize_p3_prospect(item: dict) -> dict:
    out = dict(item or {})
    url = str(out.get("url") or out.get("website") or out.get("contact") or "").strip()
    if url:
        out["url"] = url
        out.setdefault("website", url)

    title = str(
        out.get("title")
        or out.get("company")
        or out.get("name")
        or (urlparse(url).netloc if url else "")
    ).strip()
    if title:
        out.setdefault("title", title)
        out.setdefault("company", title)
        out.setdefault("name", title)

    if url and not out.get("domain"):
        out["domain"] = urlparse(url).netloc.lower()
    out.setdefault("country", "Saudi Arabia")
    if out.get("category") and not out.get("type"):
        out["type"] = out.get("category")
    return out


def _merge_p3_prospect_lists(db_items: list[dict], perplexity_items: list[dict]) -> list[dict]:
    """DB 등록 소스를 먼저 두고, 같은 도메인은 Perplexity 쪽에서 제외."""
    seen: set[str] = set()
    merged: list[dict] = []
    for item in db_items:
        item = _normalize_p3_prospect(item)
        u = str(item.get("url") or "")
        b = _p3_base_domain_for_dedupe(u)
        if not b or b in seen:
            continue
        item["base_domain"] = b
        seen.add(b)
        merged.append(item)
    for item in perplexity_items:
        item = _normalize_p3_prospect(item)
        u = str(item.get("url") or "")
        b = _p3_base_domain_for_dedupe(u)
        if not b or b in seen:
            continue
        item["base_domain"] = b
        seen.add(b)
        merged.append(item)
    return merged[:50]


_P3_BLOCKED_PROSPECT_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "wikipedia.org",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
}


def _p3_live_url_check(url: str) -> tuple[bool, int | None, str]:
    """Return (reachable, status_code, final_url) for a prospect website."""
    import httpx

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=6.0, headers=headers) as client:
            try:
                resp = client.head(url)
            except httpx.HTTPError:
                resp = client.get(url)
            if resp.status_code in {403, 405} or resp.status_code >= 500:
                try:
                    resp = client.get(url)
                except httpx.HTTPError:
                    pass
            return 200 <= resp.status_code < 400, resp.status_code, str(resp.url)
    except Exception:
        return False, None, url


def _verify_p3_prospect_items(
    items: list[dict],
    *,
    max_live_checks: int = 24,
) -> list[dict]:
    """Drop obvious hallucinated buyer candidates and annotate kept items."""
    verified: list[dict] = []
    live_checks = 0
    checked_at = datetime.now(timezone.utc).isoformat()

    for raw in items:
        item = _normalize_p3_prospect(raw)
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        base_domain = _p3_base_domain_for_dedupe(url)
        source = str(item.get("source") or "").strip()
        is_curated = source == "curated_saudi_buyer_seed"

        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not base_domain:
            continue
        if base_domain in _P3_BLOCKED_PROSPECT_DOMAINS:
            continue
        title = str(item.get("title") or item.get("company") or item.get("name") or "").strip()
        if not title:
            continue

        item["base_domain"] = base_domain
        item["verification_checked_at"] = checked_at
        item["candidate_origin"] = source or "ai_or_database"

        live_ok = False
        status_code: int | None = None
        final_url = url
        if live_checks < max_live_checks:
            live_checks += 1
            live_ok, status_code, final_url = _p3_live_url_check(url)

        item["verification_url"] = final_url
        if status_code is not None:
            item["verification_http_status"] = status_code

        if live_ok:
            item["verified"] = True
            item["verification_status"] = "verified_live_website"
        elif is_curated:
            item["verified"] = False
            item["needs_manual_verification"] = True
            item["verification_status"] = "curated_seed_domain_valid_live_check_failed"
        else:
            continue

        reasons = item.get("reasons")
        if isinstance(reasons, list):
            item["reasons"] = [
                *reasons,
                f"Buyer website verification: {item['verification_status']} ({item.get('verification_url', url)})",
            ]
        elif reasons:
            item["reasons"] = [
                str(reasons),
                f"Buyer website verification: {item['verification_status']} ({item.get('verification_url', url)})",
            ]
        else:
            item["reasons"] = [
                f"Buyer website verification: {item['verification_status']} ({item.get('verification_url', url)})"
            ]

        verified.append(item)

    return verified[:50]


@app.post("/api/p3/prospects")
async def api_p3_prospects(req: P3ProspectsRequest):
    """3공정: 바이어/파트너 후보 — Supabase 등록 소스 + Perplexity 검색 병합."""
    pplx = _get_pplx()

    drug: Optional[TargetDrug] = None
    if req.product_key:
        drug = _registry.get_drug(req.product_key)

    trade_name = (req.trade_name or (drug.trade_name if drug else "")).strip()
    ingredients = (req.ingredients or (drug.ingredient if drug else "")).strip()
    dosage_form = (req.dosage_form or (drug.dosage_form if drug else "")).strip()
    strength = (req.strength or (drug.strength if drug else "")).strip()

    if not trade_name and not ingredients:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "trade_name 또는 ingredients 중 하나는 필요합니다."},
        )

    excluded_domains: set[str] = set()
    for s in SITES:
        d = (s.get("domain") or "").strip().lower()
        if d:
            excluded_domains.add(d)

    drug_info = {
        "trade_name": trade_name,
        "ingredients": ingredients,
        "dosage_form": dosage_form,
        "strength": strength,
    }

    db_sources: list[dict] = []
    sb = _get_supabase()
    if sb:
        db_sources = _load_ai_discovered_sources_for_p3(sb)

    curated_sources = curated_buyer_candidates(drug_info, limit=30)
    if not pplx:
        merged = _merge_p3_prospect_lists(db_sources, curated_sources)
        verified = await asyncio.to_thread(_verify_p3_prospect_items, merged)
        return {
            "ok": True,
            "count": len(verified),
            "items": verified,
            "ai_count": 0,
            "curated_count": len(curated_sources),
            "db_count": len(db_sources),
            "unverified_dropped": len(merged) - len(verified),
            "warning": "PERPLEXITY_API_KEY is not configured; curated Saudi buyer seeds were used.",
        }

    try:
        items = await asyncio.to_thread(
            pplx.search_pharma_sources,
            drug_info,
            excluded_domains,
        )
        items_sorted = sorted(
            items,
            key=lambda x: float(x.get("relevance_score") or 0.0),
            reverse=True,
        )
        merged = _merge_p3_prospect_lists(db_sources, curated_sources + items_sorted)
        verified = await asyncio.to_thread(_verify_p3_prospect_items, merged)
        return {
            "ok": True,
            "count": len(verified),
            "items": verified,
            "ai_count": len(items_sorted),
            "curated_count": len(curated_sources),
            "db_count": len(db_sources),
            "unverified_dropped": len(merged) - len(verified),
        }
    except Exception as exc:
        logger.warning("P3 prospects Perplexity 실패, curated buyer seeds 사용: %s", exc)
        merged = _merge_p3_prospect_lists(db_sources, curated_sources)
        verified = await asyncio.to_thread(_verify_p3_prospect_items, merged)
        return {
            "ok": True,
            "count": len(verified),
            "items": verified,
            "ai_count": 0,
            "curated_count": len(curated_sources),
            "db_count": len(db_sources),
            "unverified_dropped": len(merged) - len(verified),
            "warning": f"Perplexity search failed; curated Saudi buyer seeds were used. {str(exc)[:120]}",
        }


@app.post("/api/p3/white-space")
async def api_p3_white_space(req: P3WhiteSpaceRequest):
    """Phase 3: 에이전트 × ATC 치료군 빈틈 분석.

    입력 INN 에 해당하는 ATC level3 치료군에서 강한 포트폴리오를 보유하지만
    해당 INN 은 취급하지 않는 에이전트를 정량 추출.
    """
    target_inn = (req.target_inn or "").strip()
    if not target_inn and not req.target_atc_level3:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "target_inn 또는 target_atc_level3 중 하나는 필요합니다."},
        )

    sb = _get_supabase()
    if not sb:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Supabase 미설정 — products 테이블 조회 불가."},
        )

    # products 전체(KSA) 조회 — 실제 DB 컬럼 기반 (agent_or_supplier → manufacturer 매핑)
    products: list[dict] = []
    _p3_fetch_note: Optional[str] = None
    try:
        resp = (
            sb.table("products")
            .select("product_id,trade_name,inn_name,manufacturer,source_tier,inn_id")
            .eq("country", "SA")
            .not_.is_("manufacturer", "null")
            .limit(max(100, min(req.product_limit, 10000)))
            .execute()
        )
        raw = resp.data or []
        # DB 컬럼 → analytics 모듈 기대 필드로 정규화
        products = [
            {
                "product_id": r.get("product_id"),
                "trade_name": r.get("trade_name"),
                "inn_name": r.get("inn_name"),
                "agent_or_supplier": r.get("manufacturer"),
                # atc_code: DB에 없으면 inn_id 앞 7자(A10BK01 형식)로 추정
                "atc_code": (r.get("inn_id") or "")[:7] or None,
                "source_tier": r.get("source_tier"),
            }
            for r in raw
            if r.get("manufacturer")
        ]
    except Exception as exc:
        logger.warning("P3 white-space DB 조회 실패: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"products 테이블 조회 실패: {str(exc)[:160]}"},
        )

    if not products:
        return {
            "ok": True,
            "target_inn": target_inn,
            "target_atc_level3": req.target_atc_level3,
            "total_agents": 0,
            "agents_in_atc": 0,
            "candidates": [],
            "notes": ["products 테이블에 manufacturer(유통사) 레코드가 없습니다."],
        }

    try:
        from analytics.agent_portfolio import analyze_white_space_for_inn

        result = await asyncio.to_thread(
            analyze_white_space_for_inn,
            products,
            target_inn,
            target_atc_level3=req.target_atc_level3,
            min_atc_products=int(req.min_atc_products),
            top_n=int(req.top_n),
        )
        result["ok"] = True
        result["products_scanned"] = len(products)
        if _p3_fetch_note:
            result.setdefault("notes", []).insert(0, _p3_fetch_note)
    except Exception as exc:
        logger.exception("P3 white-space 분석 실패")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"분석 실패: {str(exc)[:200]}"},
        )

    # ── Phase 4: Tender Power 병합 (백그라운드 점수, 실패해도 white-space 는 반환) ──
    if req.include_tender_power and result.get("candidates"):
        try:
            from analytics.tender_power import (
                compute_tender_power_for_agents,
                contracts_rows_to_records,
                nupco_awards_rows_to_records,
                score_band,
            )

            # contracts 테이블 fetch (Etimad API 낙찰 실적)
            contracts_rows: list[dict] = []
            try:
                c_resp = (
                    sb.table("contracts")
                    .select("supplier_name,contract_value,start_date,end_date,category")
                    .eq("country", "SA")
                    .not_.is_("supplier_name", "null")
                    .limit(5000)
                    .execute()
                )
                contracts_rows = c_resp.data or []
            except Exception as exc:
                logger.info("P3 tender-power: contracts 테이블 조회 스킵 (%s)", str(exc)[:100])

            # nupco_awards 테이블 fetch (아직 미존재시 조용히 skip)
            awards_rows: list[dict] = []
            try:
                a_resp = (
                    sb.table("nupco_awards")
                    .select("winner_name,award_value,award_date,category")
                    .eq("country", "SA")
                    .limit(5000)
                    .execute()
                )
                awards_rows = a_resp.data or []
            except Exception as exc:
                # nupco_awards 테이블이 아직 없을 수 있음 — 정상 케이스
                logger.debug("P3 tender-power: nupco_awards 스킵 (%s)", str(exc)[:100])

            if contracts_rows or awards_rows:
                all_records = (
                    contracts_rows_to_records(contracts_rows)
                    + nupco_awards_rows_to_records(awards_rows)
                )
                agent_names = [c.get("agent_name", "") for c in result["candidates"]]

                tp_dict = await asyncio.to_thread(
                    compute_tender_power_for_agents,
                    agent_names,
                    all_records,
                    target_atc_l3=result.get("target_atc_level3"),
                )
                meta = tp_dict.pop("__meta__", {})

                # 각 candidate 에 tender_power 필드 merge
                for cand in result["candidates"]:
                    name = cand.get("agent_name", "")
                    tp = tp_dict.get(name) or {
                        "score": 0.0, "count_last_2y": 0,
                        "total_value_mn_sar": 0.0,
                        "has_target_atc_match": False, "sources": {},
                    }
                    cand["tender_power"] = {
                        "score": tp.get("score", 0.0),
                        "band": score_band(float(tp.get("score") or 0)),
                        "count_last_2y": tp.get("count_last_2y", 0),
                        "total_value_mn_sar": tp.get("total_value_mn_sar", 0.0),
                        "has_target_atc_match": tp.get("has_target_atc_match", False),
                        "sources": tp.get("sources", {}),
                    }

                # Tender Power DESC 정렬 (plan 4-3) — missing 우선은 유지
                result["candidates"].sort(
                    key=lambda c: (
                        not c.get("missing_ingredient", False),          # missing=True 먼저
                        -float(c.get("tender_power", {}).get("score") or 0),  # score 내림차순
                        -float(c.get("portfolio_strength") or 0),        # tie-break: strength
                    )
                )

                result["tender_power_meta"] = {
                    "contracts_scanned": len(contracts_rows),
                    "awards_scanned": len(awards_rows),
                    "unmatched_supplier_count": meta.get("unmatched_supplier_count", 0),
                    "sort_applied": "tender_power_desc",
                }
            else:
                result["tender_power_meta"] = {
                    "contracts_scanned": 0,
                    "awards_scanned": 0,
                    "note": "공공조달 실적 데이터 없음 — Tender Power 계산 스킵",
                }
        except Exception as exc:
            logger.warning("P3 tender-power 병합 실패: %s", exc)
            result["tender_power_meta"] = {"error": str(exc)[:160]}

    return result


def _derive_competitor_filters(
    drug: Optional[TargetDrug],
    req: "P1CompetitorMapRequest",
) -> dict:
    """competitor-map 요청 → SFDA fetch 필터 도출.

    우선순위: target_inn > drug.ingredient > req.ingredients > req.trade_name
    """
    inn = (req.target_inn or "").strip()
    ingredients = (req.ingredients or (drug.ingredient if drug else "") or "").strip()
    trade = (req.trade_name or (drug.trade_name if drug else "") or "").strip()

    # 성분명 token: 쉼표/and/+/공백 기준 분리 후 필터용 키워드 리스트
    tokens: list[str] = []
    for raw in [inn, ingredients]:
        if not raw:
            continue
        for t in re.split(r"[,+&/]|\sand\s", raw, flags=re.IGNORECASE):
            t = t.strip().lower()
            if t and len(t) >= 3 and t not in tokens:
                tokens.append(t)

    return {
        "inn_tokens": tokens[:6],          # 쿼리 안전 상한
        "trade_name": trade,
        "target_atc_l3": (req.target_atc_level3 or "").upper().strip(),
    }


def _filter_products_by_inn_tokens(
    products: list[dict],
    inn_tokens: list[str],
    atc_l3: str = "",
) -> list[dict]:
    """products 리스트에서 INN 토큰 중 하나라도 포함된 레코드 필터.

    scientific_name + inn_name + trade_name 을 모두 스캔.
    atc_l3 가 주어지면 atc_code 첫 4자리 일치도 요구.
    """
    if not inn_tokens and not atc_l3:
        return list(products)

    tokens_lc = [t.lower() for t in inn_tokens]
    atc_pref = atc_l3.upper().strip()
    out: list[dict] = []
    for p in products:
        hay = " ".join([
            str(p.get("scientific_name") or ""),
            str(p.get("inn_name") or ""),
            str(p.get("trade_name") or ""),
        ]).lower()

        inn_ok = (not tokens_lc) or any(t in hay for t in tokens_lc)
        if not inn_ok:
            continue
        if atc_pref:
            atc = (p.get("atc_code") or "").upper()
            if atc[:4] != atc_pref:
                continue
        out.append(p)
    return out


@app.post("/api/p1/competitor-map")
async def api_p1_competitor_map(req: P1CompetitorMapRequest):
    """Phase 5: 경쟁사 유통 에이전트 역추적.

    주어진 약품의 동일 성분/치료군 경쟁 브랜드를 SFDA 에서 찾아
    유통 에이전트별 시장 시그널(브랜드 수/평균가/낙찰 실적)을 역산.
    """
    drug: Optional[TargetDrug] = None
    if req.product_key:
        drug = _registry.get_drug(req.product_key)

    filters = _derive_competitor_filters(drug, req)
    if not filters["inn_tokens"] and not filters["trade_name"] and not filters["target_atc_l3"]:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "product_key / trade_name / target_inn 중 하나는 필요합니다."},
        )

    sb = _get_supabase()
    if not sb:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Supabase 미설정 — products 조회 불가."},
        )

    # 1) SFDA 매칭 fetch (products 테이블, country=SA) — 실제 DB 컬럼 기반
    products: list[dict] = []
    _p5_fetch_note: Optional[str] = None
    try:
        resp = (
            sb.table("products")
            .select("product_id,trade_name,inn_name,manufacturer,"
                    "price,price_currency,registration_number,"
                    "dosage_form,strength,source_tier,source_url,inn_id")
            .eq("country", "SA")
            .not_.is_("manufacturer", "null")
            .limit(max(100, min(int(req.product_limit), 10000)))
            .execute()
        )
        raw = resp.data or []
        # DB 컬럼 → analytics 모듈 기대 필드로 정규화
        products = [
            {
                "product_id": r.get("product_id"),
                "trade_name": r.get("trade_name"),
                "inn_name": r.get("inn_name"),
                "agent_or_supplier": r.get("manufacturer"),
                "atc_code": (r.get("inn_id") or "")[:7] or None,
                "price_sar": r.get("price") if r.get("price_currency") in ("SAR", None) else None,
                "regulatory_id": r.get("registration_number"),
                "dosage_form": r.get("dosage_form"),
                "strength": r.get("strength"),
                "source_tier": r.get("source_tier"),
                "source_url": r.get("source_url"),
            }
            for r in raw
            if r.get("manufacturer")
        ]
    except Exception as exc:
        logger.warning("P1 competitor-map DB 조회 실패: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"products 테이블 조회 실패: {str(exc)[:160]}"},
        )

    # 2) INN/ATC 필터
    matched = _filter_products_by_inn_tokens(
        products,
        filters["inn_tokens"],
        atc_l3=filters["target_atc_l3"],
    )

    if not matched:
        return {
            "ok": True,
            "agents": [],
            "total_agents": 0,
            "total_brands": 0,
            "filters": filters,
            "products_scanned": len(products),
            "notes": ["경쟁 브랜드 매칭 없음 — INN/ATC 필터를 완화해 보세요."],
        }

    # 3) tender records (선택)
    tender_records = None
    if req.include_tender_power:
        try:
            from analytics.tender_power import (
                contracts_rows_to_records, nupco_awards_rows_to_records,
            )

            contracts_rows: list[dict] = []
            awards_rows: list[dict] = []
            try:
                c_resp = (
                    sb.table("contracts")
                    .select("supplier_name,contract_value,start_date,end_date,category")
                    .eq("country", "SA")
                    .not_.is_("supplier_name", "null")
                    .limit(5000)
                    .execute()
                )
                contracts_rows = c_resp.data or []
            except Exception as exc:
                logger.debug("competitor-map: contracts 스킵 (%s)", str(exc)[:80])

            try:
                a_resp = (
                    sb.table("nupco_awards")
                    .select("winner_name,award_value,award_date,category")
                    .eq("country", "SA")
                    .limit(5000)
                    .execute()
                )
                awards_rows = a_resp.data or []
            except Exception as exc:
                logger.debug("competitor-map: nupco_awards 스킵 (%s)", str(exc)[:80])

            if contracts_rows or awards_rows:
                tender_records = (
                    contracts_rows_to_records(contracts_rows)
                    + nupco_awards_rows_to_records(awards_rows)
                )
        except Exception as exc:
            logger.debug("competitor-map: tender 로드 실패 (%s)", str(exc)[:100])

    # 4) 경쟁사 맵 생성
    try:
        from analytics.competitor_map import build_competitor_map

        result = await asyncio.to_thread(
            build_competitor_map,
            matched,
            target_brand=filters["trade_name"] or None,
            target_agent=None,
            tender_records=tender_records,
            min_brand_count=1,
            top_n=int(req.top_n),
        )
        result["ok"] = True
        result["filters"] = filters
        result["products_scanned"] = len(products)
        result["products_matched"] = len(matched)
        result["tender_records_used"] = len(tender_records) if tender_records else 0
        if _p5_fetch_note:
            result.setdefault("notes", []).insert(0, _p5_fetch_note)
        return result
    except Exception as exc:
        logger.exception("P1 competitor-map 실패")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"경쟁사 맵 생성 실패: {str(exc)[:200]}"},
        )


@app.post("/api/p2/price-analyze")
async def api_p2_price_analyze(
    input_mode: str = Form(...),
    market_type: str = Form(...),
    report_id: Optional[str] = Form(default=None),
    report_data: Optional[str] = Form(default=None),
    overrides: Optional[str] = Form(default=None),
    manual_product: Optional[str] = Form(default=None),
    pdf: Optional[UploadFile] = File(default=None),
):
    """2공정 가격 분석 엔드포인트. 민간·공공 시장 FOB 역산 (`frontend/fob_private.py`)."""
    im = (input_mode or "").strip().lower()
    mt = (market_type or "").strip().lower()
    if im not in ("ai", "manual"):
        return JSONResponse(
            status_code=422,
            content={"ok": False, "detail": "input_mode는 ai 또는 manual 이어야 합니다."},
        )
    if mt not in ("public", "private"):
        return JSONResponse(
            status_code=422,
            content={"ok": False, "detail": "market_type는 public 또는 private 이어야 합니다."},
        )

    rid = (report_id or "").strip()
    report_payload: Optional[dict] = None
    overrides_payload: Optional[dict] = None
    man = (manual_product or "").strip()
    has_pdf = pdf is not None and bool((pdf.filename or "").strip())
    pdf_bytes: Optional[bytes] = None

    if report_data:
        try:
            parsed_report = json.loads(report_data)
            if not isinstance(parsed_report, dict):
                raise ValueError("report_data must be a JSON object")
            report_payload = parsed_report
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "report_data는 JSON 객체 문자열이어야 합니다."},
            )

    if overrides:
        try:
            parsed_overrides = json.loads(overrides)
            if not isinstance(parsed_overrides, dict):
                raise ValueError("overrides must be a JSON object")
            overrides_payload = parsed_overrides
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "overrides는 JSON 객체 문자열이어야 합니다."},
            )

    if mt == "private" and im == "manual":
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "detail": "민간 시장은 v1에서 직접 입력을 지원하지 않습니다. 저장된 1공정 보고서 또는 PDF를 사용하세요.",
            },
        )

    if mt == "private":
        if not report_payload and not has_pdf:
            detail = (
                "선택한 저장 보고서에 전체 데이터가 없습니다. 1공정을 다시 실행하거나 PDF를 업로드하세요."
                if rid
                else "민간 시장은 저장된 1공정 전체 보고서(report_data) 또는 PDF 업로드가 필요합니다."
            )
            return JSONResponse(status_code=400, content={"ok": False, "detail": detail})
    elif im == "ai":
        if not report_payload and not rid and not has_pdf:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "detail": "저장된 보고서를 선택해 주세요. JSON(report_data) 또는 PDF 업로드가 필요합니다.",
                },
            )

    # v1 제약: manual + private 금지
    if im == "manual" and mt == "private":
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "detail": "민간 시장 분석은 1공정 보고서(또는 PDF)가 필요합니다. 품목명만으로는 v1에서 지원하지 않습니다.",
            },
        )

    if im == "ai":
        if not report_payload and not rid and not has_pdf:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "저장된 보고서(report_data) 또는 PDF 업로드가 필요합니다."},
            )
    else:
        if not man:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "품목명(manual_product)을 입력하세요."},
            )

    if mt == "public" and im == "manual" and man and not report_payload:
        report_payload = {
            "trade_name": man,
            "inn": "",
            "dosage_form": "",
            "strength": "",
        }

    # PDF 크기/포맷 검증 + 바이트 수집
    if has_pdf:
        ctype = (pdf.content_type or "").lower()
        if ctype and "pdf" not in ctype and ctype != "application/octet-stream":
            return JSONResponse(
                status_code=400,
                content={"ok": False, "detail": "PDF 파일만 업로드할 수 있습니다."},
            )
        max_bytes = 8 * 1024 * 1024
        buf = bytearray()
        while True:
            chunk = await pdf.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"ok": False, "detail": "PDF는 8MB 이하만 허용됩니다."},
                )
        pdf_bytes = bytes(buf)

    if mt == "private":
        # 함수 내부에서 run_private_pipeline 을 다시 import 하면 해당 이름이 로컬로만 잡혀
        # 위쪽 호출에서 UnboundLocalError 가 난다. 모듈 상단 import 만 사용한다.
        if not report_payload and not pdf_bytes:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "detail": "저장된 1공정 보고서 본문(report_data)이 필요합니다. 구버전 보고서라면 1공정을 다시 실행하거나 PDF 업로드를 사용하세요.",
                },
            )
        try:
            fx = await asyncio.to_thread(_fetch_exchange_rates)
            llm = _get_llm()
            result = await asyncio.to_thread(
                run_private_pipeline,
                report_data=report_payload,
                pdf_bytes=pdf_bytes,
                overrides=overrides_payload,
                exchange_rates=fx,
                llm=llm,
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
        except Exception as exc:
            logger.exception("2공정 민간 시장 FOB 파이프라인 실패")
            return JSONResponse(
                status_code=500,
                content={"ok": False, "detail": f"민간 시장 FOB 역산 실패: {exc}"},
            )
        if not result.get("ok"):
            return JSONResponse(status_code=400, content=result)
        return result

    # public — NUPCO/SFDA 벤치마크 FOB (동일 역산 코어, 공공 시나리오 기본값)
    if not report_payload and not pdf_bytes:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "detail": "공공 시장 분석에는 저장된 1공정 보고서(report_data) 또는 PDF 업로드가 필요합니다.",
            },
        )
    try:
        fx = await asyncio.to_thread(_fetch_exchange_rates)
        llm = _get_llm()
        result = await asyncio.to_thread(
            run_public_pipeline,
            report_data=report_payload,
            pdf_bytes=pdf_bytes,
            overrides=overrides_payload,
            exchange_rates=fx,
            llm=llm,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
    except Exception as exc:
        logger.exception("2공정 공공 시장 FOB 파이프라인 실패")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "detail": f"공공 시장 FOB 역산 실패: {exc}"},
        )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)

    # P2 보고서 자동 생성
    try:
        from report_generator_p2 import generate_p2_report
        p2_path = await asyncio.to_thread(generate_p2_report, dict(result), ROOT / "reports")
        _p2_report_cache["latest"] = p2_path.name
    except Exception:
        logger.exception("P2 보고서 자동 생성 실패")

    return result


# ═══════════════════════════════════════════════════════════════════════════
# 커스텀 파이프라인 (신약 직접 분석)  ← Singapore /api/pipeline/custom 패턴
# ═══════════════════════════════════════════════════════════════════════════

class CustomPipelineRequest(BaseModel):
    trade_name: str
    inn: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None


@app.post("/api/pipeline/custom")
async def start_custom_pipeline(req: CustomPipelineRequest):
    """커스텀 신약 직접 분석 — 레지스트리 외 품목 파이프라인 실행."""
    if _pipeline_tasks.get("__custom__", {}).get("status") == "running":
        raise HTTPException(409, "커스텀 파이프라인 실행 중")

    drug = TargetDrug(
        id="__custom__",
        drug_type="manual",
        trade_name=req.trade_name,
        ingredient=req.inn or "",
        dosage_form=req.dosage_form or "",
        strength=req.strength or "",
        target_countries=["SA"],
        target_regions=["Middle East"],
    )
    _pipeline_tasks["__custom__"] = {
        "status": "running",
        "step": "db_load",
        "step_label": "DB 조회·크롤링",
        "result": None,
        "refs": None,
        "pdf": None,
        "started_at": time.time(),
    }

    asyncio.create_task(_run_pipeline_for_product("__custom__", drug))
    return {"status": "started", "trade_name": req.trade_name}


@app.get("/api/pipeline/custom/status")
async def get_custom_pipeline_status():
    task = _pipeline_tasks.get("__custom__")
    if not task:
        raise HTTPException(404, "커스텀 파이프라인 상태 없음")
    return {
        "status": task["status"],
        "step": task["step"],
        "step_label": task["step_label"],
        "has_result": task.get("result") is not None,
        "ref_count": len(task.get("refs") or []),
        "has_pdf": task.get("pdf") is not None,
    }


@app.get("/api/pipeline/custom/result")
async def get_custom_pipeline_result():
    task = _pipeline_tasks.get("__custom__")
    if not task:
        raise HTTPException(404, "커스텀 파이프라인 결과 없음")
    if task["status"] == "running":
        return JSONResponse({"status": "running", "step": task["step"]}, status_code=202)
    return {
        "result": task.get("result"),
        "refs": task.get("refs"),
        "pdf": task.get("pdf"),
        "ai_sources": _ai_sources_cache.get("__custom__", []),
        "perplexity_key_set": _perplexity_key_configured(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 바이어 발굴 비동기 래퍼  ← Singapore-style POST/status/result
# ═══════════════════════════════════════════════════════════════════════════

class BuyersRunRequest(BaseModel):
    product_key: Optional[str] = None
    trade_name: Optional[str] = None
    ingredients: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    active_criteria: Optional[list] = None
    target_country: Optional[str] = "Saudi Arabia"
    target_region: Optional[str] = "Middle East"


_buyer_task: dict = {"status": "idle", "result": None, "error": None}


@app.post("/api/buyers/run")
async def buyers_run(req: BuyersRunRequest):
    """바이어/파트너 발굴 비동기 실행 (Singapore pipeline 패턴)."""
    if _buyer_task.get("status") == "running":
        raise HTTPException(409, "바이어 발굴 실행 중")

    _buyer_task.update({"status": "running", "result": None, "error": None, "started_at": time.time()})

    async def _run():
        try:
            drug: Optional[TargetDrug] = None
            if req.product_key:
                drug = _registry.get_drug(req.product_key)

            trade_name = (req.trade_name or (drug.trade_name if drug else "")).strip()
            ingredients = (req.ingredients or (drug.ingredient if drug else "")).strip()
            dosage_form = (req.dosage_form or (drug.dosage_form if drug else "")).strip()
            strength = (req.strength or (drug.strength if drug else "")).strip()

            if not trade_name and not ingredients:
                _buyer_task.update({"status": "error", "error": "trade_name 또는 ingredients 필요"})
                return

            pplx = _get_pplx()

            excluded_domains: set[str] = set()
            for s in SITES:
                d = (s.get("domain") or "").strip().lower()
                if d:
                    excluded_domains.add(d)

            drug_info = {
                "trade_name": trade_name,
                "ingredients": ingredients,
                "dosage_form": dosage_form,
                "strength": strength,
            }

            db_sources: list[dict] = []
            sb = _get_supabase()
            if sb:
                db_sources = _load_ai_discovered_sources_for_p3(sb)

            curated_sources = curated_buyer_candidates(drug_info, limit=30)
            items_sorted: list[dict] = []
            warning: Optional[str] = None
            if pplx:
                try:
                    items = await asyncio.to_thread(
                        pplx.search_pharma_sources,
                        drug_info,
                        excluded_domains,
                    )
                    items_sorted = sorted(
                        items,
                        key=lambda x: float(x.get("relevance_score") or 0.0),
                        reverse=True,
                    )
                except Exception as exc:
                    logger.warning("Buyers Perplexity 검색 실패, curated buyer seeds 사용: %s", exc)
                    warning = f"Perplexity search failed; curated Saudi buyer seeds were used. {str(exc)[:120]}"
            else:
                warning = "PERPLEXITY_API_KEY is not configured; curated Saudi buyer seeds were used."

            merged = _merge_p3_prospect_lists(db_sources, curated_sources + items_sorted)
            verified = await asyncio.to_thread(_verify_p3_prospect_items, merged)
            result_payload = {
                "ok": True,
                "count": len(verified),
                "items": verified,
                "ai_count": len(items_sorted),
                "curated_count": len(curated_sources),
                "db_count": len(db_sources),
                "unverified_dropped": len(merged) - len(verified),
            }
            if warning:
                result_payload["warning"] = warning
            _buyer_task.update({
                "status": "done",
                "result": result_payload,
            })
            # P3 보고서 자동 생성
            try:
                from report_generator_p3 import generate_p3_report
                p3_path = generate_p3_report(verified, trade_name or ingredients, ROOT / "reports")
                _p3_report_cache["latest"] = p3_path.name
            except Exception:
                logger.exception("P3 보고서 자동 생성 실패")
        except Exception as exc:
            logger.warning("Buyers run 실패: %s", exc)
            _buyer_task.update({"status": "error", "error": str(exc)[:200]})

    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/api/buyers/status")
async def buyers_status():
    return {
        "status": _buyer_task.get("status", "idle"),
        "has_result": _buyer_task.get("result") is not None,
        "error": _buyer_task.get("error"),
    }


@app.get("/api/buyers/result")
async def buyers_result():
    if _buyer_task.get("status") == "running":
        return JSONResponse({"status": "running"}, status_code=202)
    result = _buyer_task.get("result")
    if not result:
        err = _buyer_task.get("error", "결과 없음")
        raise HTTPException(404, err)
    return result


# ───────────────────────────────────────────────────────────────────────────
# P2 / P3 보고서 다운로드 + 최종 합본 생성
# ───────────────────────────────────────────────────────────────────────────

@app.get("/api/p2/report/download")
async def p2_report_download(filename: Optional[str] = None):
    """SA_02 수출가격전략 DOCX 다운로드."""
    reports_dir = ROOT / "reports"
    if filename:
        target = reports_dir / filename
        if not target.is_file() or not target.is_relative_to(reports_dir):
            raise HTTPException(404, "파일을 찾을 수 없습니다.")
        return FileResponse(str(target), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=target.name)

    cached = _p2_report_cache.get("latest")
    if cached:
        target = reports_dir / cached
        if target.is_file():
            return FileResponse(str(target), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=target.name)

    candidates = sorted(reports_dir.glob("sa_02_*.docx"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not candidates:
        raise HTTPException(404, "SA_02 보고서가 없습니다. P2 가격 분석을 먼저 실행하세요.")
    return FileResponse(str(candidates[0]), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=candidates[0].name)


@app.get("/api/p3/report/download")
async def p3_report_download(filename: Optional[str] = None):
    """SA_03 바이어리스트 DOCX 다운로드."""
    reports_dir = ROOT / "reports"
    if filename:
        target = reports_dir / filename
        if not target.is_file() or not target.is_relative_to(reports_dir):
            raise HTTPException(404, "파일을 찾을 수 없습니다.")
        return FileResponse(str(target), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=target.name)

    cached = _p3_report_cache.get("latest")
    if cached:
        target = reports_dir / cached
        if target.is_file():
            return FileResponse(str(target), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=target.name)

    candidates = sorted(reports_dir.glob("sa_03_*.docx"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not candidates:
        raise HTTPException(404, "SA_03 보고서가 없습니다. 바이어 발굴을 먼저 실행하세요.")
    return FileResponse(str(candidates[0]), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=candidates[0].name)


class FinalReportRequest(BaseModel):
    trade_name: Optional[str] = None
    inn: Optional[str] = None
    hs_code: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    p1_filename: Optional[str] = None
    p2_filename: Optional[str] = None
    p3_filename: Optional[str] = None


@app.post("/api/report/final")
async def report_final(req: FinalReportRequest):
    """SA_최종 합본 DOCX 생성 후 다운로드."""
    from report_generator_final import generate_final_report

    reports_dir = ROOT / "reports"

    def _latest(pattern: str) -> Optional[Path]:
        files = sorted(reports_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        return files[0] if files else None

    p1_path = (reports_dir / req.p1_filename) if req.p1_filename else _latest("market_report_*.docx") or _latest("sa_01_*.docx")
    p2_path = (reports_dir / req.p2_filename) if req.p2_filename else _latest("sa_02_*.docx")
    p3_path = (reports_dir / req.p3_filename) if req.p3_filename else _latest("sa_03_*.docx")

    meta = {
        "trade_name":   req.trade_name or "",
        "inn":          req.inn or "",
        "hs_code":      req.hs_code or "",
        "dosage_form":  req.dosage_form or "",
        "strength":     req.strength or "",
    }

    try:
        output_path = await asyncio.to_thread(generate_final_report, p1_path, p2_path, p3_path, meta, reports_dir)
    except Exception as exc:
        logger.exception("최종 합본 생성 실패")
        raise HTTPException(500, f"최종 합본 생성 실패: {exc}")

    return FileResponse(
        str(output_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=output_path.name,
    )


# ═══════════════════════════════════════════════════════════════════════════
# P2 파이프라인 비동기 래퍼  ← Singapore-style POST/status/result
# ═══════════════════════════════════════════════════════════════════════════

class P2PipelineRequest(BaseModel):
    report_filename: Optional[str] = None
    market: Optional[str] = "public"
    report_data: Optional[dict] = None
    overrides: Optional[dict] = None


_p2_pipeline_task: dict = {"status": "idle", "result": None, "error": None}


@app.post("/api/p2/pipeline")
async def p2_pipeline_run(req: P2PipelineRequest):
    """P2 가격 분석 비동기 파이프라인 (Singapore 패턴)."""
    if _p2_pipeline_task.get("status") == "running":
        raise HTTPException(409, "P2 파이프라인 실행 중")

    if not req.report_data:
        raise HTTPException(400, "report_data 필드가 필요합니다.")

    _p2_pipeline_task.update({"status": "running", "result": None, "error": None, "started_at": time.time()})

    async def _run():
        try:
            mt = (req.market or "public").strip().lower()
            fx = await asyncio.to_thread(_fetch_exchange_rates)
            llm = _get_llm()
            pipeline_fn = run_public_pipeline if mt == "public" else run_private_pipeline
            result = await asyncio.to_thread(
                pipeline_fn,
                report_data=req.report_data,
                pdf_bytes=None,
                overrides=req.overrides,
                exchange_rates=fx,
                llm=llm,
            )
            _p2_pipeline_task.update({"status": "done", "result": result})
        except Exception as exc:
            logger.warning("P2 pipeline 실패: %s", exc)
            _p2_pipeline_task.update({"status": "error", "error": str(exc)[:200]})

    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/api/p2/pipeline/status")
async def p2_pipeline_status():
    return {
        "status": _p2_pipeline_task.get("status", "idle"),
        "has_result": _p2_pipeline_task.get("result") is not None,
        "error": _p2_pipeline_task.get("error"),
    }


@app.get("/api/p2/pipeline/result")
async def p2_pipeline_result():
    if _p2_pipeline_task.get("status") == "running":
        return JSONResponse({"status": "running"}, status_code=202)
    result = _p2_pipeline_task.get("result")
    if not result:
        err = _p2_pipeline_task.get("error", "결과 없음")
        raise HTTPException(404, err)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 통합 보고서 다운로드
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/report/combined")
async def get_combined_report():
    """최신 사우디 보고서(sa_*.docx) 반환. 여러 파일이면 가장 최신 파일 제공."""
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        raise HTTPException(404, "reports 디렉터리 없음")

    from report_generator_final import generate_final_report

    reports_dir.mkdir(parents=True, exist_ok=True)

    def _latest(pattern: str):
        files = sorted(reports_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
        return files[0] if files else None

    p1_path = _latest("market_report_*.docx") or _latest("sa_01_*.docx")
    p2_path = _latest("sa_02_*.docx")
    p3_path = _latest("sa_03_*.docx")

    if not p1_path and not p2_path and not p3_path:
        raise HTTPException(404, "생성된 보고서가 없습니다. 먼저 1공정 분석을 실행하세요.")

    try:
        output_path = await asyncio.to_thread(
            generate_final_report, p1_path, p2_path, p3_path, {}, reports_dir
        )
    except Exception as exc:
        logger.exception("최종 보고서 생성 실패")
        raise HTTPException(500, f"보고서 생성 실패: {exc}")

    if not output_path.resolve().is_relative_to(reports_dir.resolve()):
        raise HTTPException(403, "접근 거부")

    media = "application/pdf" if output_path.suffix == ".pdf" else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(output_path, media_type=media, filename=output_path.name)


# ═══════════════════════════════════════════════════════════════════════════
# 정적 파일 서빙
# ═══════════════════════════════════════════════════════════════════════════

STATIC_DIR = Path(__file__).resolve().parent / "static"

if not STATIC_DIR.is_dir():
    raise RuntimeError(
        f"frontend/static UI directory missing at {STATIC_DIR!s}. "
        "On Render: set Root Directory to the repository root (folder containing Dockerfile), "
        "not a subfolder; leave Start Command empty so Dockerfile CMD runs."
    )


@app.get("/healthz")
async def healthz():
    """Render·로드밸런서 헬스 체크용 (가벼운 JSON)."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(index.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════════════
# 직접 실행
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)
