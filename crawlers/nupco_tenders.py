"""
crawlers/nupco_tenders.py -- NUPCO 공공조달 텐더 크롤러

National Unified Procurement Company (https://www.nupco.com)의
공개 텐더 페이지를 파싱한다.

사이트 특성:
  - WordPress 기반
  - /en/tenders/ 에 텐더 목록 (HTML)
  - 각 텐더 → /en/tenders/{slug}/ 에 상세 (PDF 링크, 결과 등)
  - 로그인 필요 페이지 접근 금지 (sources.yaml 규정)
  - SAP 포탈(tenders.nupco.com)은 접속 불가 — 공개 페이지만 사용

수집 대상:
  - 텐더 제목, 게시일, 마감일
  - 첨부 PDF URL (다운로드하지 않고 URL만 기록)
  - 공개된 결과가 있는 경우 낙찰 정보
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua
from supabase_state import AuditLog, MetricsCollector, SourceReputation


def _normalize_date(raw: str | None) -> str | None:
    """공통 사우디 날짜 포맷 → ISO YYYY-MM-DD. 미인식 시 원문 반환.

    DD/MM/YYYY를 사우디 행정 관례로 MM/DD/YYYY보다 우선 적용한다.
    """
    if not raw:
        return raw
    from datetime import datetime
    formats = [
        "%Y-%m-%d",   # ISO (pass-through)
        "%d/%m/%Y",   # DD/MM/YYYY (사우디 행정 관례)
        "%m/%d/%Y",   # MM/DD/YYYY
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%d/%m/%y",
        "%m/%d/%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw

import httpx

logger = logging.getLogger("crawlers.nupco_tenders")

NUPCO_BASE = "https://www.nupco.com"
TENDERS_URL = f"{NUPCO_BASE}/en/tenders/"


class NUPCOClient:
    """NUPCO 공개 텐더 페이지 클라이언트."""

    def __init__(self, timeout: float = 20.0, delay: float = 1.0) -> None:
        self._delay = delay
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        self._last_request: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "NUPCOClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def _get_html(self, url: str) -> str:
        self._throttle()
        resp = self._http.get(url)
        resp.raise_for_status()
        return resp.text

    def get_tender_list(self, page: int = 1) -> str:
        """텐더 목록 페이지 HTML 반환."""
        if page == 1:
            return self._get_html(TENDERS_URL)
        return self._get_html(f"{TENDERS_URL}page/{page}/")

    def get_tender_detail(self, url: str) -> str:
        """개별 텐더 상세 페이지 HTML 반환."""
        return self._get_html(url)


def _parse_tender_links(html: str) -> list[dict]:
    """텐더 목록 페이지에서 텐더 링크 추출.

    WordPress 패턴: <a href="https://www.nupco.com/en/tenders/{slug}/">
    """
    tenders = []
    # 텐더 링크 패턴
    pattern = re.compile(
        r'<a\s+[^>]*href="(https?://(?:www\.)?nupco\.com/en/tenders/[^"/]+/)"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        url = match.group(1)
        title_raw = match.group(2)
        # HTML 태그 제거
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        if title and url not in [t["url"] for t in tenders]:
            tenders.append({"url": url, "title": title})
    return tenders


def _parse_tender_detail(html: str, url: str) -> dict:
    """텐더 상세 페이지에서 메타데이터 추출."""
    detail: dict[str, Any] = {"url": url}

    # 제목: <h1> 또는 <title>
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if h1_match:
        detail["title"] = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()

    # 날짜 패턴 (다양한 형식)
    date_patterns = [
        (r"(?:posting|publish|post)\s*(?:date)?[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", "posting_date"),
        (r"(?:closing|deadline|due)\s*(?:date)?[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", "closing_date"),
        (r"(\d{4}-\d{2}-\d{2})", "date_iso"),  # ISO 날짜
    ]
    for pattern, key in date_patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            detail[key] = m.group(1)

    # PDF 첨부파일 URL
    pdf_links = re.findall(
        r'href="([^"]*\.pdf(?:\?[^"]*)?)"',
        html,
        re.IGNORECASE,
    )
    if pdf_links:
        detail["pdf_urls"] = [
            urljoin(url, link) for link in pdf_links[:10]  # 최대 10개
        ]

    # 텐더 번호 (다양한 패턴)
    tender_no = re.search(
        r"(?:tender|ref|reference)\s*(?:no|number|#)?[:\s]*([A-Z0-9/-]+)",
        html,
        re.IGNORECASE,
    )
    if tender_no:
        detail["tender_number"] = tender_no.group(1).strip()

    return detail


def _has_next_page(html: str, current_page: int) -> bool:
    """WordPress 페이지네이션에서 다음 페이지 존재 여부 확인."""
    next_page = current_page + 1
    return f"/page/{next_page}/" in html or f"page/{next_page}" in html


def map_tender_to_schema(tender: dict, *, source_url: str) -> dict[str, Any]:
    """텐더 데이터를 DB 스키마용 dict로 변환."""
    tender_id = (
        tender.get("tender_number")
        or tender.get("url", "").rstrip("/").split("/")[-1]
        or "UNKNOWN"
    )

    return {
        "tender_id": f"NUPCO_{tender_id}",
        "country": "SA",
        "source_name": "nupco_tenders",
        "source_tier": 2,
        "source_url": tender.get("url", source_url),
        "title": tender.get("title"),
        "tender_number": tender.get("tender_number"),
        "posting_date": _normalize_date(tender.get("posting_date") or tender.get("date_iso")),
        "closing_date": _normalize_date(tender.get("closing_date")),
        "pdf_urls": tender.get("pdf_urls", []),
        "market_segment": "tender",
        "confidence": 0.70,
        "raw_payload": tender,
    }


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """NUPCO 텐더 크롤러.

    cfg 옵션:
      - max_pages: 최대 페이지 수 (기본 20)
      - max_details: 상세 페이지 최대 조회 수 (기본 50)
      - delay: 요청 간격 초 (기본 1.0)
    """
    inserted = 0
    updated = 0  # NOTE: Supabase upsert does not distinguish insert vs update; always 0.
    skipped = 0

    max_pages = cfg.get("max_pages", 20)
    max_details = cfg.get("max_details", 50)
    delay = cfg.get("delay", 1.0)
    source_url = TENDERS_URL

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "nupco_tenders", {"max_pages": max_pages})
    metrics.inc("crawl_attempts")

    logger.info(f"NUPCO 텐더 크롤링 시작 (max_pages={max_pages})")

    all_tenders: list[dict] = []
    details_fetched = 0  # with 블록 밖에서 초기화 (네트워크 예외 시 NameError 방지)

    with NUPCOClient(delay=delay) as client:
        # 1단계: 텐더 목록 수집
        for page_num in range(1, max_pages + 1):
            t_page = time.time()
            try:
                html = client.get_tender_list(page_num)
            except httpx.HTTPError as e:
                logger.warning(f"텐더 목록 {page_num}페이지 실패: {e}")
                break

            tenders = _parse_tender_links(html)
            if not tenders:
                logger.info(f"페이지 {page_num}: 텐더 없음, 종료")
                break

            all_tenders.extend(tenders)
            logger.info(f"페이지 {page_num}: {len(tenders)}건 텐더 발견")
            audit.log("page_fetched", "nupco_tenders", {
                "page": page_num, "tenders_found": len(tenders),
            })
            metrics.observe("page_fetch_sec", time.time() - t_page)

            if not _has_next_page(html, page_num):
                break

        logger.info(f"총 {len(all_tenders)}건 텐더 링크 수집")

        # 2단계: 상세 페이지 조회 (최신 max_details건)
        for tender in all_tenders[:max_details]:
            try:
                html = client.get_tender_detail(tender["url"])
                detail = _parse_tender_detail(html, tender["url"])
                tender.update(detail)
                details_fetched += 1
            except Exception as e:
                logger.warning(f"텐더 상세 조회 실패 ({tender['url']}): {e}")
                audit.log("error", "nupco_tenders", {
                    "url": tender["url"],
                    "error": str(e)[:200],
                })

        logger.info(f"{details_fetched}건 상세 페이지 조회 완료")

    # 3단계: DB 저장
    for tender in all_tenders:
        record = map_tender_to_schema(tender, source_url=source_url)

        # 소스 신뢰도 보정
        bonus = reputation.confidence_bonus("nupco_tenders")
        record["confidence"] = min(0.99, max(0.0, record["confidence"] + bonus))

        if not dry_run:
            try:
                resp = sb.table("tenders").upsert(
                    record, on_conflict="tender_id"
                ).execute()

                if getattr(resp, "data", None):
                    inserted += 1
            except Exception as e:
                skipped += 1
                logger.error(f"DB 저장 실패 (tender_id={record['tender_id']}): {e}")
        else:
            inserted += 1

        metrics.inc("records_processed")
        audit.log("record_processed", "nupco_tenders", {
            "tender_id": record.get("tender_id"),
            "has_pdf": bool(record.get("pdf_urls")),
            "confidence": record.get("confidence"),
        })

    # 완료
    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("nupco_tenders", True)

    audit.log("crawl_finished", "nupco_tenders", {
        "total_tenders": len(all_tenders),
        "details_fetched": details_fetched,
        "inserted": inserted,
        "skipped": skipped,
        "metrics": metrics.summary(),
    })

    logger.info(f"NUPCO 텐더 완료: {len(all_tenders)}건 수집, inserted={inserted}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
