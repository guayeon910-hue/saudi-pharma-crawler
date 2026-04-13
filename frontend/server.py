"""
frontend/server.py -- 사우디 제약 크롤러 대시보드 서버

대시보드 UI 서빙 + SSE 실시간 이벤트 + 단일 품목 파이프라인 실행.
기존 targeted_search / ai_search / report_generator 모듈을 재활용한다.

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
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── 프로젝트 루트 계산 ──
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "products.db"

# dotenv
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# snippets + 루트 경로
sys.path.insert(0, str(ROOT / "assets" / "snippets"))
sys.path.insert(0, str(ROOT))

from drug_registry import DrugRegistry, TargetDrug
from targeted_search import search_one_drug, AggregatedResult
from frontend.dashboard_sites import SITES, get_initial_states

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


def _get_pplx():
    global _pplx_client
    if _pplx_client is not None:
        return _pplx_client
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
    logger.info("Frontend server 시작")
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
                    result = search_one_drug(drug)
                    sr_dict = result.to_dict()
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
    # DB에서 소스별 크롤링 통계 조회 (GitHub Actions 결과 반영)
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
            sfda_records = data.get("records", [])
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
                sfda_records.extend(rows)
        except Exception as e:
            logger.warning("DB products 조회 실패: %s", e)

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
        "step": "crawl",
        "step_label": "크롤링",
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
    }


# ═══════════════════════════════════════════════════════════════════════════
# 파이프라인 내부: 4단계 실행
# ═══════════════════════════════════════════════════════════════════════════

async def _run_pipeline_for_product(product_key: str, drug: TargetDrug) -> None:
    task = _pipeline_tasks[product_key]
    try:
        # ── Step 1: Crawl ──
        task["step"] = "crawl"
        task["step_label"] = "크롤링"
        _state["running"] = True
        _reset_site_states()
        await _emit({"phase": "pipeline", "step": "crawl",
                      "message": f"[{drug.trade_name}] 크롤링 시작"})

        search_result = None
        sb = _get_supabase()
        if sb:
            try:
                ingredient_key = drug.ingredient.split("+")[0].strip()
                resp = (
                    sb.table("products").select("*")
                    .eq("country", "SA")
                    .ilike("active_ingredient", f"%{ingredient_key}%")
                    .order("crawled_at", desc=True)
                    .limit(50)
                    .execute()
                )
                if resp.data:
                    search_result = {"source": "database", "rows": resp.data, "count": len(resp.data)}
                    await _emit({"phase": "pipeline", "step": "crawl",
                                  "message": f"DB에서 {len(resp.data)}건 조회"})
            except Exception as e:
                await _emit({"phase": "log", "message": f"DB 조회 실패, 직접 크롤링: {e}"})

        if not search_result:
            try:
                result = search_one_drug(drug)
                sr_dict = result.to_dict()
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
                await _emit({"phase": "pipeline", "step": "crawl",
                              "message": f"크롤링 완료: {total}건"})
            except Exception as e:
                await _emit({"phase": "log", "message": f"크롤링 실패: {e}"})
                search_result = {"total_matches": 0, "source_results": [], "error": str(e)}

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
아래 JSON 형식으로 응답해주세요:
{{
  "verdict": "적합" | "조건부" | "부적합",
  "confidence": 0.0~1.0,
  "rationale": "판정 근거 (한국어, 3~5문장)",
  "key_factors": ["핵심 요인 1", "핵심 요인 2", ...],
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
        from llm_client import MODEL_SONNET
        resp = llm.ask(prompt, model=MODEL_SONNET, max_tokens=2048)
        parsed = resp.parse_json()

        base_result["verdict"] = parsed.get("verdict", "분석실패")
        base_result["confidence"] = float(parsed.get("confidence", 0.0))
        base_result["rationale"] = parsed.get("rationale", "")
        base_result["key_factors"] = parsed.get("key_factors", [])
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

    # DB에서 동일 성분 가격 조회
    if sb:
        try:
            # 동일 성분 제품
            resp = (
                sb.table("products")
                .select("trade_name, price, price_currency, source_name, active_ingredient, strength, dosage_form")
                .eq("country", "SA")
                .ilike("active_ingredient", f"%{ingredient_key}%")
                .not_.is_("price", "null")
                .order("crawled_at", desc=True)
                .limit(50)
                .execute()
            )
            if resp.data:
                for row in resp.data:
                    prices.append({
                        "trade_name": row.get("trade_name", ""),
                        "price": float(row["price"]) if row.get("price") else None,
                        "currency": row.get("price_currency", "SAR"),
                        "source": row.get("source_name", ""),
                        "ingredient": row.get("active_ingredient", ""),
                        "strength": row.get("strength", ""),
                        "type": "동일성분",
                    })

            # 동일 제형의 다른 약도 비교 대상으로 가져오기
            form_key = drug.dosage_form.lower().replace(".", "").strip()
            if form_key:
                resp2 = (
                    sb.table("products")
                    .select("trade_name, price, price_currency, source_name, active_ingredient, dosage_form")
                    .eq("country", "SA")
                    .ilike("dosage_form", f"%{form_key}%")
                    .not_.is_("price", "null")
                    .order("price", desc=False)
                    .limit(20)
                    .execute()
                )
                if resp2.data:
                    for row in resp2.data:
                        competitor_prices.append({
                            "trade_name": row.get("trade_name", ""),
                            "price": float(row["price"]) if row.get("price") else None,
                            "ingredient": row.get("active_ingredient", ""),
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
            p = r.get("price", "")
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

        output_path = generate_report(drug, report_data)
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
    return {
        "running": _report_cache["running"],
        "latest_pdf": _report_cache.get("latest_pdf"),
        "pdf_count": pdf_count,
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
            resp = sb.table("products").select("price, trade_name", count="exact").eq("country", "SA").not_.is_("price", "null").execute()
            count = resp.count or 0
            prices = [float(r["price"]) for r in (resp.data or []) if r.get("price")]
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
# DB 크롤링 통계 (GitHub Actions 결과)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/db-stats")
async def get_db_stats():
    """Supabase에 쌓인 크롤링 데이터 통계."""
    sb = _get_supabase()
    if not sb:
        return {"connected": False, "message": "Supabase 미연결", "stats": {}}

    try:
        # 소스별 건수
        resp = sb.table("products").select("source_name, price, active_ingredient, trade_name").eq("country", "SA").execute()
        rows = resp.data or []

        source_stats: dict[str, dict] = {}
        total_with_price = 0
        for r in rows:
            sn = r.get("source_name", "unknown")
            source_stats.setdefault(sn, {"count": 0, "with_price": 0})
            source_stats[sn]["count"] += 1
            if r.get("price"):
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
# 정적 파일 서빙
# ═══════════════════════════════════════════════════════════════════════════

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
