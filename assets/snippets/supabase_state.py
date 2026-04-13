"""
supabase_state.py — Actions 환경에서 상태를 Supabase 테이블로 영속화

GitHub Actions 러너는 잡 종료와 함께 파일시스템·메모리가 증발한다.
`core/crawler.py`의 파일 기반 영속화(.failed_queue.json)는 여기서 쓸 수 없다.
대신 이 모듈이 Supabase 테이블을 상태 저장소로 사용한다.

요구 테이블 (assets/sql/state_tables.sql 참조):
- saudi_api_quota          토큰 버킷
- saudi_circuit_state      서킷 브레이커
- saudi_failed_urls        실패 큐
- saudi_selector_cache     동적 셀렉터 캐시
- saudi_crawl_runs         실행 이력

사용:
    from supabase import create_client
    from supabase_state import TokenBucket, CircuitBreaker, FailedQueue

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    bucket = TokenBucket(sb, domain="developer.sfda.gov.sa")
    if bucket.take():
        # 요청 진행
        ...
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

class CrawlErrorType(str, Enum):
    AUTH_FAIL = "AUTH_FAIL"
    WAF_DETECTED = "WAF_DETECTED"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN = "UNKNOWN"


# ─── 1. 토큰 버킷 (도메인별 레이트 리밋) ──────────────
class TokenBucket:
    """도메인별 초당 요청 수를 Supabase에서 공유 관리.

    - take(): 토큰이 있으면 차감하고 True, 없으면 refill 대기
    - 구현은 낙관적 update (잔량 > 0 조건부 update)
    - 고도의 동시성엔 부적합하지만 본 프로젝트 규모엔 충분
    """

    def __init__(self, supabase_client: Any, domain: str) -> None:
        self.sb = supabase_client
        self.domain = domain

    def _fetch_state(self) -> dict | None:
        resp = (
            self.sb.table("saudi_api_quota")
            .select("*")
            .eq("domain", self.domain)
            .maybe_single()
            .execute()
        )
        return resp.data if resp.data else None

    def _refill(self, state: dict) -> dict:
        """경과 시간만큼 토큰 리필하고 업데이트된 state 반환"""
        now = time.time()
        last = _parse_ts(state["last_refill"])
        elapsed = max(0.0, now - last)
        new_tokens = min(
            float(state["tokens_max"]),
            float(state["tokens_current"]) + elapsed * float(state["refill_rate"]),
        )
        self.sb.table("saudi_api_quota").update(
            {
                "tokens_current": new_tokens,
                "last_refill": _to_iso(now),
            }
        ).eq("domain", self.domain).execute()
        state["tokens_current"] = new_tokens
        return state

    def take(self, *, block: bool = True, max_wait: float = 30.0) -> bool:
        """토큰 1개 소비. 없으면 block=True일 때 최대 max_wait 초 대기.

        반환: 성공 True / 포기 False
        """
        deadline = time.time() + max_wait
        while True:
            state = self._fetch_state()
            if state is None:
                logger.warning("saudi_api_quota에 %s 엔트리 없음 — 통과", self.domain)
                return True

            state = self._refill(state)
            current = float(state["tokens_current"])
            if current >= 1.0:
                self.sb.table("saudi_api_quota").update(
                    {"tokens_current": current - 1.0}
                ).eq("domain", self.domain).execute()
                return True

            if not block:
                return False

            # 다음 토큰까지 대기 시간
            refill_rate = float(state["refill_rate"])
            wait = 1.0 / refill_rate if refill_rate > 0 else 5.0
            if time.time() + wait > deadline:
                logger.warning("토큰 대기 타임아웃: %s", self.domain)
                return False
            time.sleep(wait)


# ─── 2. 서킷 브레이커 ─────────────────────────────────
@dataclass
class CircuitState:
    state: str        # closed | open | half_open
    failures: int
    opened_at: Optional[float]


class CircuitBreaker:
    """도메인별 연속 실패 카운트. 5회 실패 → open, 10분 후 half_open"""

    FAIL_THRESHOLD = 5
    OPEN_DURATION_SEC = 600.0  # 10분

    def __init__(self, supabase_client: Any, domain: str) -> None:
        self.sb = supabase_client
        self.domain = domain

    def _get(self) -> CircuitState:
        resp = (
            self.sb.table("saudi_circuit_state")
            .select("*")
            .eq("domain", self.domain)
            .maybe_single()
            .execute()
        )
        if not resp.data:
            return CircuitState(state="closed", failures=0, opened_at=None)
        opened_at = _parse_ts(resp.data["opened_at"]) if resp.data.get("opened_at") else None
        return CircuitState(
            state=resp.data["state"],
            failures=int(resp.data["failures"]),
            opened_at=opened_at,
        )

    def _upsert(self, state: str, failures: int, opened_at: Optional[float]) -> None:
        self.sb.table("saudi_circuit_state").upsert(
            {
                "domain": self.domain,
                "state": state,
                "failures": failures,
                "opened_at": _to_iso(opened_at) if opened_at else None,
                "updated_at": _to_iso(time.time()),
            }
        ).execute()

    def allow_request(self) -> bool:
        """요청 보내도 되는지 판정.

        closed → 항상 True
        open → OPEN_DURATION 경과했으면 half_open 전환 후 True
        half_open → True (1회 시도 허용)
        """
        cs = self._get()
        if cs.state == "closed":
            return True
        if cs.state == "open":
            if cs.opened_at and (time.time() - cs.opened_at) > self.OPEN_DURATION_SEC:
                self._upsert("half_open", cs.failures, cs.opened_at)
                logger.info("서킷 %s: open → half_open", self.domain)
                return True
            return False
        # half_open
        return True

    def record_success(self) -> None:
        self._upsert("closed", 0, None)

    def force_open(self) -> None:
        """403(WAF) 등 즉시 open이 필요한 경우 강제 오픈."""
        cs = self._get()
        failures = max(self.FAIL_THRESHOLD, cs.failures)
        self._upsert("open", failures, time.time())

    def record_failure(self) -> None:
        cs = self._get()
        new_failures = cs.failures + 1
        if cs.state == "half_open":
            self._upsert("open", new_failures, time.time())
            logger.warning("서킷 %s: half_open → open (재실패)", self.domain)
        elif new_failures >= self.FAIL_THRESHOLD:
            self._upsert("open", new_failures, time.time())
            logger.warning("서킷 %s: closed → open (%d회 연속 실패)", self.domain, new_failures)
        else:
            self._upsert("closed", new_failures, None)


# ─── 3. 실패 URL 큐 (포이즌 필 포함) ──────────────────
class FailedQueue:
    """실패한 URL을 영속화. 3회 실패하면 dead 플래그"""

    POISON_THRESHOLD = 3

    def __init__(self, supabase_client: Any) -> None:
        self.sb = supabase_client

    def record_failure(
        self,
        url: str,
        source_name: str,
        error: str,
        *,
        error_type: CrawlErrorType = CrawlErrorType.UNKNOWN,
    ) -> None:
        """실패 1회 기록. Postgres RPC로 원자 증가.

        select→update 패턴은 동일 URL에 동시 실패가 몰릴 때 카운트가
        유실된다. state_tables.sql의 `saudi_failed_urls_bump()` 함수를
        호출해 단일 트랜잭션 내에서 upsert + 카운트 증가 + dead 플래그
        업데이트를 수행한다.
        """
        self.sb.rpc(
            "saudi_failed_urls_bump",
            {
                "p_url": url,
                "p_source_name": source_name,
                "p_error": error[:1000],
                "p_error_type": error_type.value,
                "p_poison_threshold": self.POISON_THRESHOLD,
            },
        ).execute()

    def record_success(self, url: str) -> None:
        """성공 시 큐에서 제거 (dead가 아닌 경우만)"""
        self.sb.table("saudi_failed_urls").delete().eq("url", url).eq(
            "dead", False
        ).execute()

    def is_dead(self, url: str) -> bool:
        resp = (
            self.sb.table("saudi_failed_urls")
            .select("dead")
            .eq("url", url)
            .maybe_single()
            .execute()
        )
        return bool(resp.data and resp.data.get("dead"))


# ─── 4. 소스 신뢰도 (SciSpace §6.3 EMA Reputation) ────
class SourceReputation:
    """소스별 과거 성공률 기반 신뢰도 점수.

    논문의 TrustAggregator의 Exponential Moving Average(EMA) 기반
    에이전트 평판 시스템을 단일 크롤러 소스에 맞게 축소 적용.

    점수 범위: 0.0 ~ 1.0 (초기값 0.5)
    confidence 보정: (score - 0.5) × 0.1 → ±0.05 범위

    Supabase 저장 없이 인메모리 운영도 가능하나,
    saudi_crawl_runs 히스토리에서 부트스트랩할 수 있다.
    """

    def __init__(
        self,
        supabase_client: Any = None,
        *,
        alpha: float = 0.1,
    ) -> None:
        self.sb = supabase_client
        self.alpha = alpha
        self._scores: dict[str, float] = {}

    def bootstrap_from_runs(self, limit: int = 50) -> None:
        """saudi_crawl_runs 최근 N건에서 소스별 성공률로 초기화."""
        if self.sb is None:
            return
        try:
            resp = (
                self.sb.table("saudi_crawl_runs")
                .select("workflow, status")
                .order("id", desc=True)
                .limit(limit)
                .execute()
            )
            if not resp.data:
                return
            # workflow별 성공/실패 집계
            counts: dict[str, dict[str, int]] = {}
            for row in resp.data:
                wf = row.get("workflow", "unknown")
                st = row.get("status", "failure")
                if wf not in counts:
                    counts[wf] = {"success": 0, "total": 0}
                counts[wf]["total"] += 1
                if st == "success":
                    counts[wf]["success"] += 1
            for wf, c in counts.items():
                self._scores[wf] = c["success"] / c["total"] if c["total"] > 0 else 0.5
            logger.info("소스 신뢰도 부트스트랩 완료: %s", self._scores)
        except Exception as e:
            logger.warning("소스 신뢰도 부트스트랩 실패: %s", e)

    def update(self, source: str, success: bool) -> float:
        """성공/실패 1건 반영 (EMA 업데이트).

        Returns:
            업데이트된 신뢰도 점수
        """
        current = self._scores.get(source, 0.5)
        target = 1.0 if success else 0.0
        new_score = current + self.alpha * (target - current)
        new_score = max(0.0, min(1.0, new_score))
        self._scores[source] = new_score
        return new_score

    def get(self, source: str) -> float:
        """소스의 현재 신뢰도 점수."""
        return self._scores.get(source, 0.5)

    def confidence_bonus(self, source: str) -> float:
        """confidence 필드에 더할 보정값. 범위 ±0.05."""
        return (self.get(source) - 0.5) * 0.1


# ─── 5. 실행 이력 추적 ────────────────────────────────
class CrawlRun:
    """매 실행마다 saudi_crawl_runs에 row 하나 생성·업데이트"""

    def __init__(
        self,
        supabase_client: Any,
        *,
        workflow: str,
        toggle_id: Optional[str] = None,
        github_run_id: Optional[str] = None,
    ) -> None:
        self.sb = supabase_client
        self.workflow = workflow
        self.toggle_id = toggle_id
        self.github_run_id = github_run_id
        self.run_id: Optional[int] = None
        self.rows_inserted = 0
        self.rows_updated = 0

    def start(self) -> None:
        resp = (
            self.sb.table("saudi_crawl_runs")
            .insert(
                {
                    "workflow": self.workflow,
                    "toggle_id": self.toggle_id,
                    "github_run_id": self.github_run_id,
                    "status": "running",
                }
            )
            .execute()
        )
        if resp.data:
            self.run_id = resp.data[0]["id"]

    def finish(self, status: str, error_summary: str | None = None) -> None:
        if self.run_id is None:
            return
        self.sb.table("saudi_crawl_runs").update(
            {
                "finished_at": _to_iso(time.time()),
                "status": status,
                "rows_inserted": self.rows_inserted,
                "rows_updated": self.rows_updated,
                "error_summary": (error_summary or "")[:2000],
            }
        ).eq("id", self.run_id).execute()


# ─── 6. 구조화 감사 로그 (SciSpace §4.2 Audit) ────────
class AuditLog:
    """JSON 구조화 감사 로그.

    논문의 9-Stage Pipeline 중 Stage 9 (Audit Logging) 구현.
    CrawlRun과 함께 사용하여 개별 이벤트를 추적한다.

    이벤트 타입:
      - crawl_started   : 크롤링 시작
      - page_fetched    : 페이지 1건 조회
      - record_processed: 레코드 1건 처리 (정규화/INN/이상치 결과 포함)
      - antibot_detected: Anti-bot 탐지
      - crawl_finished  : 크롤링 완료 (메트릭 포함)
      - error           : 오류 발생

    저장: 인메모리 리스트 → finish() 시 JSON으로 CrawlRun에 첨부
    """

    def __init__(self) -> None:
        self._events: list[dict] = []

    def log(self, event_type: str, source: str, details: dict | None = None) -> dict:
        """이벤트 1건 기록."""
        import time as _t
        entry = {
            "ts": _t.time(),
            "event": event_type,
            "source": source,
            "details": details or {},
        }
        self._events.append(entry)
        return entry

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def count_by_type(self) -> dict[str, int]:
        """이벤트 타입별 건수."""
        counts: dict[str, int] = {}
        for e in self._events:
            t = e["event"]
            counts[t] = counts.get(t, 0) + 1
        return counts

    def to_json(self) -> str:
        """JSON 직렬화 (CrawlRun.finish()에 첨부용)."""
        import json
        return json.dumps(self._events, default=str, ensure_ascii=False)


# ─── 7. 경량 메트릭 수집기 (SciSpace §7.2 Monitoring) ──
class MetricsCollector:
    """Prometheus 없이 CrawlRun에 첨부할 수 있는 경량 메트릭.

    수집 항목:
      - counters: crawl_attempts, crawl_success, records_processed,
                  outliers_detected, antibot_detected
      - timers:   crawl_duration_sec, page_fetch_sec
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._timers: dict[str, list[float]] = {}

    def inc(self, name: str, value: int = 1) -> None:
        """카운터 증가."""
        self._counters[name] = self._counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        """타이머/히스토그램에 값 추가."""
        self._timers.setdefault(name, []).append(value)

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def summary(self) -> dict:
        """전체 메트릭 요약 (CrawlRun.finish() 첨부용)."""
        import statistics as _st
        result: dict = {"counters": dict(self._counters)}
        for name, values in self._timers.items():
            if not values:
                continue
            sorted_vals = sorted(values)
            p95_idx = int(len(sorted_vals) * 0.95)
            result[name] = {
                "count": len(values),
                "mean": round(_st.mean(values), 3),
                "p95": round(sorted_vals[min(p95_idx, len(sorted_vals) - 1)], 3),
                "max": round(max(values), 3),
            }
        return result

    def to_json(self) -> str:
        import json
        return json.dumps(self.summary(), default=str, ensure_ascii=False)


# ─── 유틸 ──────────────────────────────────────────────
def _to_iso(ts: float) -> str:
    """epoch → ISO 8601 UTC"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_ts(value: Any) -> float:
    """Supabase timestamptz → epoch"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime
    # supabase-py는 보통 ISO 문자열 반환
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
