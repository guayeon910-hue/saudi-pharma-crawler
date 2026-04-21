"""crawlers/nupco_awards.py — NUPCO 낙찰 PDF 파싱.

기존 `nupco_tenders.py` 가 수집한 `tenders` 레코드의 `pdf_urls` 중
낙찰(award) 관련 PDF 를 휴리스틱으로 선별 → 본문 텍스트 추출(pdfplumber) →
낙찰자명 + 금액 regex 추출 → `nupco_awards` 테이블에 저장.

설계 원칙 (프로젝트 헌법 + plan):
    - 추가 비용 없음: pdfplumber 만 사용 (무료, MIT)
    - 스캔본 PDF 는 OCR 없이 best-effort, 실패 시 조용히 skip
    - 크롤러 run() 과 분리 → 기존 nupco_tenders 플로우 영향 없음
    - Arabic / English 양쪽 패턴 지원
"""

from __future__ import annotations

import io
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua  # noqa: E402
from supabase_state import AuditLog, MetricsCollector  # noqa: E402

logger = logging.getLogger("crawlers.nupco_awards")


# ─── 파일명 휴리스틱 ────────────────────────────────────────────────
# 낙찰 문서로 추정되는 PDF 파일명 키워드 (대소문자 무시, 영/아랍 병행)
_AWARD_FILENAME_KEYS_EN = (
    "award", "winner", "winning", "result", "granted",
    "contract_award", "ترسية", "فائز", "ترسيه",
)


def looks_like_award_pdf(url: str) -> bool:
    """PDF URL 의 파일명이 낙찰 문서 패턴을 포함하는지 판정.

    예: "/en/tenders/x/award-2024.pdf" → True
         "/en/tenders/x/specifications.pdf" → False
    """
    if not url:
        return False
    # query string 제거 후 파일명 추출
    tail = url.split("?")[0].rsplit("/", 1)[-1].lower()
    return any(k in tail for k in _AWARD_FILENAME_KEYS_EN)


# ─── 낙찰자 / 금액 / 날짜 정규식 ────────────────────────────────────
# 영문 + 아랍어 양쪽 지원. 우선순위: 더 구체적인 패턴 먼저.

_PAT_WINNER_EN = [
    re.compile(
        r"(?i)(?:winning\s+(?:bidder|supplier|company|vendor)|"
        r"awarded\s+to|winner|awardee)\s*[:\-]?\s*([A-Za-z0-9&.,'\- ]{3,120}?)"
        r"(?:\s*(?:\n|\r|  |for\s+|value|amount|sar|riyal))",
    ),
    re.compile(
        r"(?i)contract\s+(?:is\s+)?awarded\s+to\s+([A-Za-z0-9&.,'\- ]{3,120}?)"
        r"(?:\s*(?:\n|\r|  |for\s+|value|amount|sar|riyal))",
    ),
]

_PAT_WINNER_AR = [
    # "الشركة الفائزة : ..." / "الفائز : ..." / "ترسية على ..."
    re.compile(r"(?:الشركة\s*الفائزة|الفائز|المورد\s*الفائز)\s*[:\-]?\s*(.{3,120}?)(?:\n|\r|$)"),
    re.compile(r"تم\s*ترسية\s*.{0,40}?على\s+(.{3,120}?)(?:\n|\r|$)"),
    re.compile(r"ترسية\s+المناقصة\s*(?:على)?\s*(.{3,120}?)(?:\n|\r|$)"),
]

_PAT_VALUE = [
    # "Contract Value: 1,234,567.00 SAR" / "Value: SAR 2,500,000"
    re.compile(
        r"(?i)(?:contract\s+value|total\s+value|award(?:ed)?\s+amount|value|amount)\s*[:\-]?\s*"
        r"(?:sar\s*|ريال\s*)?([0-9]{1,3}(?:[,،][0-9]{3})*(?:\.[0-9]+)?)\s*(?:sar|ريال)?",
    ),
    # Arabic numeric value with 'ريال'
    re.compile(r"([0-9]{1,3}(?:[,،][0-9]{3})*(?:\.[0-9]+)?)\s*ريال"),
    # Standalone line starting with SAR
    re.compile(r"(?i)\bSAR\s*([0-9]{1,3}(?:[,،][0-9]{3})*(?:\.[0-9]+)?)"),
]

_PAT_DATE = [
    # ISO or sauidi formats
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),
    re.compile(r"(\d{1,2}-\d{1,2}-\d{4})"),
]


def _clean_winner_name(raw: str) -> str:
    """낙찰자명 후처리: 공백 축약·특수문자 제거·길이 제한."""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    # 맨 앞/뒤 구두점·콜론·따옴표 제거
    s = s.strip(" :,-\"'·•")
    # 150자 이상은 regex가 오버매치한 것으로 간주, 앞부분만 사용
    if len(s) > 150:
        s = s[:150]
    return s


def _parse_amount_to_float(raw: str) -> Optional[float]:
    """'1,234,567.50' / '1٬234٬567' → 1234567.50 float 변환. 실패시 None."""
    if not raw:
        return None
    s = raw.replace(",", "").replace("،", "").replace("٬", "").strip()
    try:
        val = float(s)
    except (ValueError, TypeError):
        return None
    # 비상식적 범위 필터 (<= 0 또는 > 1e12 제외)
    if val <= 0 or val > 1e12:
        return None
    return val


def extract_award_from_text(text: str) -> dict:
    """PDF 본문 텍스트에서 낙찰자·금액·날짜 추출.

    Returns:
        {
          "winner_name": str | None,
          "award_value": float | None,
          "award_date":  str | None,
          "language":    "en" | "ar" | "mixed",
          "confidence":  0.0 ~ 1.0 (휴리스틱 매칭 품질),
        }
    """
    if not text:
        return {"winner_name": None, "award_value": None, "award_date": None,
                "language": None, "confidence": 0.0}

    # 언어 감지 (단순 휴리스틱: 아랍어 글자 비중)
    ar_chars = len(re.findall(r"[\u0600-\u06FF]", text))
    en_chars = len(re.findall(r"[A-Za-z]", text))
    language = "ar" if ar_chars > en_chars * 2 else ("en" if en_chars > ar_chars * 2 else "mixed")

    # winner 추출 — 영문 먼저, 실패 시 아랍어
    winner_name: Optional[str] = None
    winner_hits = 0
    for pat in _PAT_WINNER_EN:
        m = pat.search(text)
        if m:
            winner_name = _clean_winner_name(m.group(1))
            winner_hits += 1
            break
    if not winner_name:
        for pat in _PAT_WINNER_AR:
            m = pat.search(text)
            if m:
                winner_name = _clean_winner_name(m.group(1))
                winner_hits += 1
                break

    # value 추출 — 가장 먼저 매치되는 합리적 값
    award_value: Optional[float] = None
    for pat in _PAT_VALUE:
        for m in pat.finditer(text):
            val = _parse_amount_to_float(m.group(1))
            if val is not None and val >= 1000:   # 1000 SAR 미만은 노이즈 무시
                award_value = val
                break
        if award_value is not None:
            break

    # date 추출
    award_date: Optional[str] = None
    for pat in _PAT_DATE:
        m = pat.search(text)
        if m:
            award_date = m.group(1)
            break

    # confidence: winner 매치 + value 매치 + date 매치 중 몇 개인지
    hits = (1 if winner_name else 0) + (1 if award_value else 0) + (1 if award_date else 0)
    confidence = round(hits / 3.0, 2)

    return {
        "winner_name": winner_name,
        "award_value": award_value,
        "award_date": award_date,
        "language": language,
        "confidence": confidence,
    }


# ─── PDF 다운로드 + 텍스트 추출 ──────────────────────────────────────

def fetch_pdf_text(
    pdf_url: str,
    *,
    http_client: Optional[httpx.Client] = None,
    max_bytes: int = 20_000_000,
    max_pages: int = 30,
) -> Optional[str]:
    """PDF 다운로드 + pdfplumber 로 텍스트 추출. 실패 시 None.

    Args:
        pdf_url: 대상 PDF URL
        http_client: 재사용 가능한 httpx.Client (선택)
        max_bytes: 최대 다운로드 크기 (스캔본 대용량 방지)
        max_pages: 최대 처리 페이지 수

    Notes:
        - 스캔본 이미지 PDF 는 pdfplumber 가 빈 문자열 반환 → None 처리
        - 404/401 은 조용히 None (정상 케이스, 페이지 구조상 흔함)
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        logger.warning("pdfplumber 미설치 → PDF 파싱 불가. `pip install pdfplumber` 후 재시도.")
        return None

    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": pick_ua(), "Accept": "application/pdf,*/*"},
        follow_redirects=True,
    )
    try:
        resp = client.get(pdf_url)
        if resp.status_code in (401, 403, 404):
            return None
        resp.raise_for_status()

        content = resp.content
        if len(content) > max_bytes:
            logger.info("PDF 크기 초과 (%d bytes > %d) — skip: %s", len(content), max_bytes, pdf_url)
            return None
        if not content or len(content) < 500:  # 500 bytes 미만은 에러 페이지 가능성
            return None

        # Content-Type 체크 (일부 서버가 PDF 를 text/html 로 리다이렉트)
        ctype = (resp.headers.get("content-type") or "").lower()
        if "pdf" not in ctype and not content[:4].startswith(b"%PDF"):
            return None

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages_to_read = pdf.pages[: max_pages]
            text_parts: list[str] = []
            for p in pages_to_read:
                try:
                    t = p.extract_text() or ""
                except Exception as exc:  # pdfplumber 버그 방어
                    logger.debug("pdfplumber page extract 실패: %s", exc)
                    t = ""
                if t:
                    text_parts.append(t)
            text = "\n".join(text_parts).strip()
            return text or None

    except httpx.HTTPError as exc:
        logger.debug("PDF HTTP 실패: %s (%s)", pdf_url, exc)
        return None
    except Exception as exc:
        logger.debug("PDF 파싱 실패: %s (%s)", pdf_url, exc)
        return None
    finally:
        if owns_client:
            client.close()


def parse_award_pdf_url(
    pdf_url: str,
    *,
    http_client: Optional[httpx.Client] = None,
) -> Optional[dict]:
    """단일 PDF URL → 낙찰 정보 dict. 없거나 파싱 실패시 None.

    Returns 시 반환값은 extract_award_from_text 동일 + `source_pdf_url`.
    """
    text = fetch_pdf_text(pdf_url, http_client=http_client)
    if not text:
        return None
    result = extract_award_from_text(text)
    # confidence 0.33 미만(=3항목 중 1개 이하)이면 드롭
    if result["confidence"] < 0.33 or not result.get("winner_name"):
        return None
    result["source_pdf_url"] = pdf_url
    return result


# ─── Supabase 통합 실행기 ──────────────────────────────────────────

def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """`tenders` 테이블 스캔 → PDF 낙찰 파싱 → `nupco_awards` 저장.

    cfg 옵션:
      - max_tenders: 스캔할 최대 텐더 수 (기본 200)
      - delay: PDF 간 요청 간격 (기본 1.5초)
      - source_name_filter: 소스 필터 (기본 "nupco_tenders")
    """
    inserted = 0
    skipped = 0
    pdf_scanned = 0
    award_found = 0

    max_tenders = int(cfg.get("max_tenders", 200))
    delay = float(cfg.get("delay", 1.5))
    source_filter = str(cfg.get("source_name_filter", "nupco_tenders"))

    audit = AuditLog()
    metrics = MetricsCollector()

    t_start = time.time()
    audit.log("crawl_started", "nupco_awards", {"max_tenders": max_tenders})
    metrics.inc("crawl_attempts")

    # tenders 테이블에서 pdf_urls 를 가진 레코드 fetch
    try:
        resp = (
            sb.table("tenders")
            .select("tender_id,title,pdf_urls,posting_date,closing_date")
            .eq("source_name", source_filter)
            .limit(max_tenders)
            .execute()
        )
        tenders = resp.data or []
    except Exception as exc:
        logger.error("tenders 조회 실패: %s", exc)
        audit.log("error", "nupco_awards", {"stage": "db_query", "error": str(exc)[:200]})
        return {
            "rows_inserted": 0, "rows_updated": 0, "rows_skipped": 0,
            "audit_log": audit.to_json(), "metrics": metrics.to_json(),
        }

    logger.info("NUPCO 낙찰 스캔 시작 (tenders=%d)", len(tenders))

    client = httpx.Client(
        timeout=30.0,
        headers={"User-Agent": pick_ua(), "Accept": "application/pdf,*/*"},
        follow_redirects=True,
    )
    try:
        for tender in tenders:
            tender_id = str(tender.get("tender_id", ""))
            pdf_urls: Iterable[str] = tender.get("pdf_urls") or []
            if not pdf_urls:
                continue

            award_candidates = [u for u in pdf_urls if looks_like_award_pdf(u)]
            if not award_candidates:
                continue

            for pdf_url in award_candidates:
                pdf_scanned += 1
                try:
                    parsed = parse_award_pdf_url(pdf_url, http_client=client)
                except Exception as exc:
                    logger.debug("parse_award_pdf_url 예외: %s (%s)", pdf_url, exc)
                    parsed = None

                time.sleep(delay)

                if not parsed:
                    continue

                award_found += 1
                record = {
                    "tender_id": tender_id,
                    "winner_name": parsed["winner_name"],
                    "award_value": parsed["award_value"],
                    "award_date": parsed.get("award_date") or tender.get("posting_date"),
                    "currency": "SAR",
                    "language": parsed.get("language"),
                    "confidence": parsed.get("confidence", 0.0),
                    "source_pdf_url": parsed["source_pdf_url"],
                    "country": "SA",
                }

                if dry_run:
                    inserted += 1
                    continue

                try:
                    sb.table("nupco_awards").upsert(
                        record, on_conflict="tender_id,source_pdf_url"
                    ).execute()
                    inserted += 1
                except Exception as exc:
                    skipped += 1
                    logger.warning("nupco_awards upsert 실패 (%s): %s", tender_id, exc)

            metrics.inc("tenders_scanned")
    finally:
        client.close()

    metrics.observe("crawl_duration_sec", time.time() - t_start)
    metrics.inc("crawl_success" if award_found > 0 else "crawl_partial")

    audit.log("crawl_finished", "nupco_awards", {
        "tenders_scanned": len(tenders),
        "pdf_scanned": pdf_scanned,
        "award_found": award_found,
        "inserted": inserted,
        "skipped": skipped,
    })

    logger.info(
        "NUPCO 낙찰 완료: tenders=%d pdf=%d award=%d inserted=%d",
        len(tenders), pdf_scanned, award_found, inserted,
    )

    return {
        "rows_inserted": inserted,
        "rows_updated": 0,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
