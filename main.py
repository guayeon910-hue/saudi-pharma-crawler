"""
main.py — 사우디 크롤링 토글 실행 엔트리포인트

GitHub Actions 워크플로(sa_api_light.yml, sa_procurement.yml, sa_retail_mid.yml)가
`python -m main` 형태로 호출한다. 환경변수로 파라미터를 받는다.

환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_KEY   (필수)
  TOGGLE_ID       toggle_1 ~ toggle_8  (필수)
  WORKFLOW        sa_api_light | sa_procurement | sa_retail_mid (필수)
  DRY_RUN         'true'면 DB 삽입 생략
  GITHUB_RUN_ID   Actions가 주입
  SOURCE_FILTER   'all' 또는 특정 source name (옵션, retail_mid matrix용)

책임:
  1. sources.yaml 로드 → 토글에 매핑된 소스 리스트 확정
  2. 현 워크플로에 해당하는 소스만 필터링
  3. 각 소스별로:
       - CircuitBreaker.allow_request 검사
       - TokenBucket.take 로 레이트 리밋 준수
       - dispatch table에서 크롤러 함수 조회 → 실행
       - 성공/실패를 CircuitBreaker, FailedQueue, CrawlRun에 기록
  4. 한 소스 실패가 다른 소스로 전파되지 않도록 소스 단위 try/except

책임 아닌 것:
  - 실제 파싱 로직 (각 크롤러 모듈에 위임)
  - 재시도 로직 (backoff_retry 데코레이터가 크롤러 내부에서 담당)
  - IP 로테이션 (Actions 러너 그 자체가 로테이터)
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
from typing import Any, Callable

import httpx
import yaml
from supabase import create_client

from antibot import AntiBotType, detect as detect_antibot, get_countermeasure
from backoff_retry import RetryExhausted
from supabase_state import CircuitBreaker, CrawlErrorType, CrawlRun, FailedQueue, TokenBucket


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


SOURCES_YAML = Path(__file__).parent / "sources.yaml"


# ─── 크롤러 dispatch table ─────────────────────────────
# 각 소스 이름 → 실제 크롤러 호출 함수.
# 함수 시그니처: (supabase_client, source_config, dry_run) -> dict
#   리턴 dict: {"rows_inserted": int, "rows_updated": int}
# 아직 구현되지 않은 소스는 stub으로 두고, 구현 완료 시점에 교체한다.
CrawlerFn = Callable[[Any, dict, bool], dict]


def _run_sfda_api(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """SFDA Developer Portal API 크롤러"""
    from crawlers.sfda_api import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_sfda_drugs_list_html(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """SFDA 의약품 목록 HTML fallback 크롤러"""
    from crawlers.sfda_drugs_list_html import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_sfda_companies(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """SFDA 제약사/대리점 마스터 크롤러"""
    from crawlers.sfda_companies import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_nupco_tenders(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """NUPCO 공공조달 텐더 크롤러"""
    from crawlers.nupco_tenders import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_etimad_api(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Etimad 공공조달 API 크롤러"""
    from crawlers.etimad_api import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_nahdi_web(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Nahdi 소매약국 가격 크롤러"""
    from crawlers.nahdi_web import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_al_dawaa_web(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Al Dawaa 소매약국 가격 크롤러 (Cloudflare 차단 가능)"""
    from crawlers.al_dawaa_web import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_whites_web(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Whites Pharmacy 소매 가격 크롤러"""
    from crawlers.whites_web import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_tamer_group(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Tamer Group 도매유통 공급처 마스터 크롤러"""
    from crawlers.tamer_group import run as _run
    return _run(sb, cfg, dry_run=dry_run)


def _run_noon_saudi(sb: Any, cfg: dict, dry_run: bool) -> dict:
    """Noon Saudi 대형 상거래 가격 크롤러 (Cloudflare 차단)"""
    from crawlers.noon_saudi import run as _run
    return _run(sb, cfg, dry_run=dry_run)


CRAWLERS: dict[str, CrawlerFn] = {
    "sfda_api":             _run_sfda_api,
    "sfda_drugs_list_html": _run_sfda_drugs_list_html,
    "sfda_companies":       _run_sfda_companies,
    "nupco_tenders":        _run_nupco_tenders,
    "etimad_api":           _run_etimad_api,
    "nahdi_web":            _run_nahdi_web,
    "al_dawaa_web":         _run_al_dawaa_web,
    "whites_web":           _run_whites_web,
    "tamer_group":          _run_tamer_group,
    "noon_saudi":           _run_noon_saudi,
}


# ─── 유틸 ──────────────────────────────────────────────
def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        logger.error("필수 환경변수 누락: %s", key)
        sys.exit(2)
    return val or ""


def _load_sources() -> dict:
    with SOURCES_YAML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_targets(
    config: dict,
    toggle_id: str,
    workflow: str,
    source_filter: str,
) -> list[tuple[str, dict]]:
    """토글 + 워크플로 + source_filter로 실행 대상 확정."""
    toggle = config.get("toggles", {}).get(toggle_id)
    if not toggle:
        logger.error("토글 %s 를 sources.yaml에서 찾을 수 없음", toggle_id)
        sys.exit(2)

    source_names: list[str] = toggle["sources"]
    all_sources: dict = config["sources"]

    targets: list[tuple[str, dict]] = []
    for name in source_names:
        cfg = all_sources.get(name)
        if not cfg:
            logger.warning("소스 %s 설정 없음 — 스킵", name)
            continue
        # enabled: false 인 소스는 제외 (예: noon_saudi)
        if cfg.get("enabled") is False:
            logger.info("소스 %s 는 enabled=false — 스킵", name)
            continue
        # 현 워크플로 소속 소스만
        if cfg.get("workflow") != workflow:
            continue
        # source_filter 적용 (retail_mid matrix에서 단일 소스 실행용)
        if source_filter and source_filter != "all" and source_filter != name:
            continue
        targets.append((name, cfg))

    return targets


def _domain_of(cfg: dict) -> str:
    """url_seed에서 호스트만 추출. TokenBucket/CircuitBreaker 키로 사용."""
    from urllib.parse import urlparse
    return urlparse(cfg.get("url_seed", "")).netloc or cfg.get("workflow", "unknown")


# ─── 실행 ──────────────────────────────────────────────
def main() -> int:
    supabase_url = _env("SUPABASE_URL", required=True)
    supabase_key = _env("SUPABASE_SERVICE_KEY", required=True)
    toggle_id    = _env("TOGGLE_ID", required=True)
    workflow     = _env("WORKFLOW", required=True)
    dry_run      = _env("DRY_RUN", "false").lower() == "true"
    github_run   = _env("GITHUB_RUN_ID", "local")
    source_filter = _env("SOURCE_FILTER", "all")

    logger.info(
        "시작: toggle=%s workflow=%s dry_run=%s run_id=%s filter=%s",
        toggle_id, workflow, dry_run, github_run, source_filter,
    )

    sb = create_client(supabase_url, supabase_key)
    config = _load_sources()
    targets = _resolve_targets(config, toggle_id, workflow, source_filter)

    if not targets:
        logger.warning("실행 대상 소스가 없음 — 정상 종료")
        return 0

    logger.info("실행 대상: %s", [n for n, _ in targets])

    failed_q = FailedQueue(sb)
    run = CrawlRun(
        sb,
        workflow=workflow,
        toggle_id=toggle_id,
        github_run_id=github_run,
    )
    run.start()

    overall_status = "success"
    error_lines: list[str] = []

    for name, cfg in targets:
        domain = _domain_of(cfg)
        breaker = CircuitBreaker(sb, domain=domain)
        bucket = TokenBucket(sb, domain=domain)

        if not breaker.allow_request():
            logger.warning("서킷 open: %s (%s) — 스킵", name, domain)
            error_lines.append(f"{name}: circuit_open")
            overall_status = "partial"
            continue

        if not bucket.take(block=True, max_wait=30.0):
            logger.warning("토큰 고갈: %s (%s) — 스킵", name, domain)
            error_lines.append(f"{name}: quota_exhausted")
            overall_status = "partial"
            continue

        crawler = CRAWLERS.get(name)
        if crawler is None:
            logger.error("dispatch table에 %s 없음", name)
            error_lines.append(f"{name}: no_dispatch")
            overall_status = "partial"
            continue

        logger.info("[%s] 실행 시작 (domain=%s)", name, domain)
        try:
            result = crawler(sb, cfg, dry_run)
            run.rows_inserted += int(result.get("rows_inserted", 0))
            run.rows_updated  += int(result.get("rows_updated", 0))
            breaker.record_success()
            logger.info(
                "[%s] 성공: inserted=%s updated=%s",
                name, result.get("rows_inserted"), result.get("rows_updated"),
            )
        except RetryExhausted as e:
            logger.error("[%s] 재시도 소진: %s", name, e)
            breaker.record_failure()
            failed_q.record_failure(
                cfg.get("url_seed", name),
                name,
                str(e),
                error_type=CrawlErrorType.UNKNOWN,
            )
            error_lines.append(f"{name}: retry_exhausted")
            overall_status = "partial"
        except httpx.TimeoutException as e:
            tb = traceback.format_exc(limit=3)
            logger.error("[%s] 타임아웃: %s\n%s", name, e, tb)
            breaker.record_failure()
            failed_q.record_failure(
                cfg.get("url_seed", name),
                name,
                f"{e}\n{tb}",
                error_type=CrawlErrorType.NETWORK_TIMEOUT,
            )
            error_lines.append(f"{name}: timeout")
            overall_status = "partial"
        except httpx.HTTPStatusError as e:
            tb = traceback.format_exc(limit=3)
            status = e.response.status_code if e.response is not None else None

            # ── Anti-bot 탐지 (SciSpace §6.2) ──
            resp_body = ""
            try:
                resp_body = e.response.text[:2000]
            except Exception:
                pass
            resp_headers = dict(e.response.headers) if e.response else {}
            ab_type = detect_antibot(status or 0, resp_body, resp_headers)
            cm = get_countermeasure(ab_type)

            error_type = CrawlErrorType.UNKNOWN
            if status == 401:
                error_type = CrawlErrorType.AUTH_FAIL
            elif ab_type in (AntiBotType.CLOUDFLARE, AntiBotType.IP_BLOCK,
                             AntiBotType.RECAPTCHA, AntiBotType.WAF_GENERIC):
                error_type = CrawlErrorType.WAF_DETECTED
            elif status == 429:
                error_type = CrawlErrorType.RATE_LIMIT

            logger.error(
                "[%s] HTTP 오류: status=%s antibot=%s type=%s %s\n%s",
                name, status, ab_type.value, error_type.value, e, tb,
            )

            # Anti-bot 대응: circuit break 대상이면 force_open
            if cm.should_circuit_break:
                breaker.force_open()
            else:
                breaker.record_failure()

            failed_q.record_failure(
                cfg.get("url_seed", name),
                name,
                f"{e}\n{tb}",
                error_type=error_type,
            )
            error_lines.append(f"{name}: http_{status}")
            overall_status = "partial"
        except Exception as e:  # noqa: BLE001  소스 하나 실패가 전체를 죽이지 않음
            tb = traceback.format_exc(limit=3)
            logger.error("[%s] 예외: %s\n%s", name, e, tb)
            breaker.record_failure()
            failed_q.record_failure(
                cfg.get("url_seed", name),
                name,
                f"{e}\n{tb}",
                error_type=CrawlErrorType.UNKNOWN,
            )
            error_lines.append(f"{name}: {type(e).__name__}")
            overall_status = "partial"

    # 전 소스 실패면 failure, 일부 실패면 partial, 전부 성공이면 success
    if overall_status == "partial" and run.rows_inserted == 0 and run.rows_updated == 0:
        overall_status = "failure"

    run.finish(overall_status, error_summary="; ".join(error_lines) if error_lines else None)
    logger.info(
        "종료: status=%s inserted=%d updated=%d",
        overall_status, run.rows_inserted, run.rows_updated,
    )

    # Actions가 Slack 알림을 띄우려면 non-zero exit 필요
    return 0 if overall_status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
