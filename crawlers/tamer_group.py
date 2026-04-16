"""
crawlers/tamer_group.py -- Tamer Group 도매유통 공급처 마스터 크롤러

Tamer Group (https://tamergroup.com)의 헬스케어/FMCG 유통 페이지에서
공급처 정보를 수집한다.

⚠️ 상태: 403 반환 (2026-04-12 확인)
   → 정적 회사 페이지로 자주 변경되지 않음
   → Cloudflare/WAF 가능성
   → 월 1회 시도, 실패 시 서킷 브레이커 자동 차단

수집 대상: agent_or_supplier (공급처 마스터)
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))
from antibot import pick_ua, detect as detect_antibot, AntiBotType
from supabase_state import AuditLog, MetricsCollector, SourceReputation

import httpx

logger = logging.getLogger("crawlers.tamer_group")

TAMER_BASE = "https://tamergroup.com"
TAMER_HEALTHCARE_URL = f"{TAMER_BASE}/sectors/distribution-healthcare-fmcg"


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Tamer Group 공급처 마스터 크롤러.

    정적 회사 페이지이므로 단일 페이지만 조회.
    브랜드/유통사 목록을 추출하여 supplier_master에 저장.
    """
    inserted = 0
    updated = 0
    skipped = 0

    delay = cfg.get("delay", 1.0)

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)

    t_start = time.time()
    audit.log("crawl_started", "tamer_group", {})
    metrics.inc("crawl_attempts")

    logger.info("Tamer Group 크롤링 시작")

    try:
        client = httpx.Client(
            timeout=20.0,
            headers={
                "User-Agent": pick_ua(),
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
        )
        resp = client.get(TAMER_HEALTHCARE_URL)

        # Anti-bot 감지
        ab_type = detect_antibot(
            resp.status_code,
            resp.text[:2000],
            dict(resp.headers),
        )
        if ab_type != AntiBotType.NONE:
            logger.warning(f"Tamer Group anti-bot 감지: {ab_type.value}")
            audit.log("antibot_detected", "tamer_group", {"type": ab_type.value})
            raise httpx.HTTPStatusError(
                f"Anti-bot: {ab_type.value}",
                request=resp.request,
                response=resp,
            )

        resp.raise_for_status()
        html = resp.text

        # 브랜드/파트너 목록 추출
        brands = _extract_brands(html)
        logger.info(f"Tamer Group: {len(brands)}개 브랜드/파트너 발견")

        for brand in brands:
            record = {
                "supplier_id": f"TAMER_{brand['slug']}",
                "country": "SA",
                "supplier_name": brand["name"],
                "supplier_type": "distributor",
                "parent_company": "Tamer Group",
                "sector": brand.get("sector", "healthcare"),
                "source_url": TAMER_HEALTHCARE_URL,
                "source_name": "tamer_group",
                "source_tier": 4,
                "confidence": 0.60,
                "raw_payload": brand,
            }

            bonus = reputation.confidence_bonus("tamer_group")
            record["confidence"] = min(0.99, max(0.0, record["confidence"] + bonus))

            if not dry_run:
                try:
                    sb.table("suppliers").upsert(
                        record, on_conflict="supplier_id"
                    ).execute()
                    inserted += 1
                except Exception as e:
                    skipped += 1
                    logger.error(f"DB 저장 실패: {e}")
            else:
                inserted += 1

            metrics.inc("records_processed")

        client.close()

    except httpx.HTTPStatusError:
        raise  # main.py의 anti-bot 핸들러가 처리
    except httpx.TimeoutException:
        raise  # main.py의 타임아웃 핸들러가 처리
    except Exception as e:
        logger.error(f"Tamer Group 크롤링 실패: {e}")
        skipped += 1

    metrics.inc("crawl_success" if inserted > 0 else "crawl_partial")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("tamer_group", inserted > 0)

    audit.log("crawl_finished", "tamer_group", {
        "inserted": inserted, "skipped": skipped,
    })

    logger.info(f"Tamer Group 완료: inserted={inserted}")
    return {
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }


def _extract_brands(html: str) -> list[dict]:
    """HTML에서 브랜드/파트너사 목록 추출."""
    brands = []
    seen = set()

    # 패턴 1: 이미지 alt 태그에 브랜드명
    for match in re.finditer(r'<img[^>]*alt="([^"]+)"[^>]*/?>',  html, re.IGNORECASE):
        name = match.group(1).strip()
        if name and len(name) > 2 and name.lower() not in seen:
            seen.add(name.lower())
            slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
            brands.append({"name": name, "slug": slug})

    # 패턴 2: h3/h4 태그 내 브랜드명
    for match in re.finditer(r'<h[34][^>]*>(.*?)</h[34]>', html, re.DOTALL | re.IGNORECASE):
        name = re.sub(r'<[^>]+>', '', match.group(1)).strip()
        if name and len(name) > 2 and name.lower() not in seen:
            seen.add(name.lower())
            slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
            brands.append({"name": name, "slug": slug, "sector": "healthcare"})

    return brands
