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
from targeted_search import search_one_drug, AggregatedResult
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
    api_key = os.environ.get("CLAUDE_API_KEY", "")
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
    pplx_key = os.environ.get("PERPLEXITY_API_KEY", "")
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
    for fld in ("price_local", "price", "retail_price"):
        v = row.get(fld)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _shape_record_for_dashboard(rec: dict) -> dict:
    """JSON 스냅샷(`price`, `outlier`)과 Supabase `products`(`price_local`, `outlier_flagged`) 병합 시 UI 필드 통일."""
    out = dict(rec)
    if out.get("price") is None and out.get("price_local") is not None:
        try:
            out["price"] = float(out["price_local"])
        except (TypeError, ValueError):
            pass
    if "outlier" not in out and "outlier_flagged" in out:
        out["outlier"] = bool(out.get("outlier_flagged"))
    return out


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

    prompt = f"""사우디아라비아(KSA) 의약품 시장 진출 적합성을 분석해주세요.

## 대상 품목
- 품목명: {drug.trade_name}
- 성분: {drug.ingredient}
- 함량: {drug.strength}
- 제형: {drug.dosage_form}
- 종류: {drug.drug_type}

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

    # DB에서 동일 성분 가격 조회 (컬럼명·필터 호환)
    if sb:
        try:
            same_rows, same_err = _fetch_products_by_ingredient_flexible(sb, ingredient_key, limit=80)
            if same_err:
                logger.warning("가격 동일성분 DB 조회: %s", same_err)
            else:
                for row in same_rows:
                    pl = _row_price_local(row)
                    if pl is None:
                        continue
                    prices.append({
                        "trade_name": row.get("trade_name", ""),
                        "price": pl,
                        "currency": row.get("currency", "SAR"),
                        "source": row.get("source_name", ""),
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
                        "type": "유사제형",
                    })
        except Exception as e:
            logger.warning("가격 데이터 DB 조회 실패: %s", e)

    # search_data에서 추가 가격 추출
    if search_data and isinstance(search_data, dict):
        for sr in search_data.get("source_results", []):
            for m in sr.get("matches", []):
                p = m.get("price_sar") or m.get("price") or m.get("retail_price")
                if p is not None:
                    try:
                        prices.append({
                            "trade_name": m.get("trade_name", m.get("name", "")),
                            "price": float(p),
                            "currency": "SAR",
                            "source": sr.get("source_name", ""),
                            "type": "크롤링",
                        })
                    except (ValueError, TypeError):
                        pass

    price_values = [p["price"] for p in prices if p.get("price")]
    comp_values = [p["price"] for p in competitor_prices if p.get("price")]

    return {
        "same_ingredient": prices,
        "competitors": competitor_prices[:10],
        "summary": {
            "count": len(price_values),
            "min": min(price_values) if price_values else None,
            "max": max(price_values) if price_values else None,
            "avg": round(sum(price_values) / len(price_values), 2) if price_values else None,
            "competitor_avg": round(sum(comp_values) / len(comp_values), 2) if comp_values else None,
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


def _summarize_price_data(price_data: dict) -> str:
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
            report_data = {
                "total_matches": search_data.get("count", 0),
                "source_results": [],
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

        output_path = generate_report(
            drug,
            report_data,
            analysis=analysis,
            refs=refs or [],
            report_meta=report_meta,
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
    """Perplexity 클라이언트와 동일하게 netloc 기준 2레벨 도메인으로 중복 판별."""
    try:
        netloc = urlparse(url.strip()).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        parts = netloc.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return netloc or ""
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


def _merge_p3_prospect_lists(db_items: list[dict], perplexity_items: list[dict]) -> list[dict]:
    """DB 등록 소스를 먼저 두고, 같은 도메인은 Perplexity 쪽에서 제외."""
    seen: set[str] = set()
    merged: list[dict] = []
    for item in db_items:
        u = str(item.get("url") or "")
        b = _p3_base_domain_for_dedupe(u)
        if not b or b in seen:
            continue
        seen.add(b)
        merged.append(item)
    for item in perplexity_items:
        u = str(item.get("url") or "")
        b = _p3_base_domain_for_dedupe(u)
        if not b or b in seen:
            continue
        seen.add(b)
        merged.append(item)
    return merged[:50]


@app.post("/api/p3/prospects")
async def api_p3_prospects(req: P3ProspectsRequest):
    """3공정: 바이어/파트너 후보 — Supabase 등록 소스 + Perplexity 검색 병합."""
    pplx = _get_pplx()
    if not pplx:
        raise HTTPException(503, "PERPLEXITY_API_KEY 미설정")

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
        merged = _merge_p3_prospect_lists(db_sources, items_sorted)
        return {"ok": True, "count": len(merged), "items": merged}
    except Exception as exc:
        logger.warning("P3 prospects Perplexity 실패: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"Perplexity 요청 실패: {str(exc)[:160]}"},
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
    return result


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
