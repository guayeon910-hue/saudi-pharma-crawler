"""Saudi Drugs Information System (SDI) crawler.

SDI is hosted at https://sdi.sfda.gov.sa/ and is the public SFDA portal for
registered drug product information.  The same SDI product index is also
exposed through SFDA's unauthenticated JSON endpoints on www.sfda.gov.sa, which
are more reliable from server environments that reset direct SDI TLS sessions.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent / "assets" / "snippets"))

from inn_normalizer import INNNormalizer
from normalizer import normalize_record
from outlier_detector import flag_record
from sfda_web import SFDAWebClient, map_web_to_schema
from supabase_state import AuditLog, MetricsCollector, SourceReputation

logger = logging.getLogger("crawlers.sdi_sfda")

SDI_SEARCH_URL = "https://sdi.sfda.gov.sa/Home/DrugSearch"
SDI_RESULT_URL = "https://sdi.sfda.gov.sa/home/Result"

try:
    _inn_norm = INNNormalizer()
    _inn_norm.load_reference()
except Exception as exc:  # pragma: no cover - defensive startup path
    logger.warning("INN reference load failed for SDI crawler: %s", exc)
    _inn_norm = None


def map_sdi_to_schema(item: dict[str, Any], *, source_url: str = SDI_SEARCH_URL) -> dict[str, Any]:
    """Map one SDI/SFDA public endpoint item into the products schema."""
    record = map_web_to_schema(item, source_url=source_url)
    reg_no = str(item.get("registerNumber") or "").strip()
    record.update(
        {
            "product_id": f"SDI_{reg_no or 'UNKNOWN'}",
            "source_name": "sdi_sfda",
            "source_tier": 1,
            "source_url": source_url,
            "confidence": 0.90,
            "raw_payload": {
                **item,
                "sdi_search_url": source_url,
                "sdi_result_url": f"{SDI_RESULT_URL}?drugId={item.get('drugId')}"
                if item.get("drugId")
                else None,
                "data_source": "Saudi Drugs Information System (SDI)",
            },
        }
    )
    return record


class SDISFDAClient:
    """Search SDI records using SFDA's public JSON mirror endpoints."""

    def __init__(self, *, delay: float = 0.5, timeout: float = 20.0) -> None:
        self._client = SFDAWebClient(delay=delay, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SDISFDAClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search(
        self,
        *,
        trade_name: str = "",
        scientific_name: str = "",
        reg_no: str = "",
        max_pages: int | None = 3,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        while True:
            data = self._client.search(
                trade_name=trade_name,
                scientific_name=scientific_name,
                reg_no=reg_no,
                page=page,
            )
            items = data.get("results") or []
            for item in items:
                key = str(item.get("registerNumber") or item.get("drugId") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.append(item)

            page_count = int(data.get("pageCount") or 0)
            if not items or page >= page_count:
                break
            if max_pages and page >= max_pages:
                break
            page += 1
        return out


def run(sb: Any, cfg: dict, dry_run: bool = False) -> dict:
    """Crawl SDI search results into Supabase products.

    cfg options:
      - mode: search | full
      - keyword: trade/scientific keyword for search mode
      - search_field: scientific_name | trade_name | reg_no
      - start_page/max_pages/delay for full mode compatibility
    """
    inserted = 0
    skipped = 0
    mode = str(cfg.get("mode") or "search").lower()
    keyword = str(cfg.get("keyword") or "").strip()
    search_field = str(cfg.get("search_field") or "scientific_name").lower()
    start_page = int(cfg.get("start_page") or 1)
    max_pages = cfg.get("max_pages")
    max_pages = int(max_pages) if max_pages else None
    delay = float(cfg.get("delay") or 0.5)

    audit = AuditLog()
    metrics = MetricsCollector()
    reputation = SourceReputation(sb)
    reputation.bootstrap_from_runs(limit=50)
    t_start = time.time()
    audit.log("crawl_started", "sdi_sfda", {"mode": mode, "keyword": keyword})
    metrics.inc("crawl_attempts")

    existing_prices: list[float] = []
    if not dry_run:
        try:
            rows = (
                sb.table("products")
                .select("price_local")
                .eq("country", "SA")
                .not_.is_("price_local", "null")
                .is_("deleted_at", "null")
                .execute()
            )
            existing_prices = [float(r["price_local"]) for r in (rows.data or [])]
        except Exception as exc:
            logger.warning("Existing price load failed for SDI crawler: %s", exc)

    with SFDAWebClient(delay=delay) as web_client:
        if mode == "full":
            page_iter = web_client.iter_all(start_page=start_page, max_pages=max_pages)
        else:
            if not keyword:
                return {
                    "rows_inserted": 0,
                    "rows_updated": 0,
                    "rows_skipped": 0,
                    "audit_log": audit.to_json(),
                    "metrics": metrics.to_json(),
                }
            if search_field == "trade_name":
                page_iter = web_client.iter_search(trade_name=keyword, max_pages=max_pages)
            elif search_field == "reg_no":
                page_iter = web_client.iter_search(reg_no=keyword, max_pages=max_pages)
            else:
                page_iter = web_client.iter_search(scientific_name=keyword, max_pages=max_pages)

        for page_num, items in page_iter:
            audit.log("page_fetched", "sdi_sfda", {"page": page_num, "items": len(items)})
            for item in items:
                record = map_sdi_to_schema(item)
                record = normalize_record(record)
                if _inn_norm is not None:
                    record = _inn_norm.normalize_record(record)
                record = flag_record(record, existing_prices)
                record["confidence"] = min(
                    0.99,
                    max(0.0, (record.get("confidence") or 0.90) + reputation.confidence_bonus("sdi_sfda")),
                )

                if dry_run:
                    inserted += 1
                else:
                    try:
                        resp = sb.table("products").upsert(record, on_conflict="product_id").execute()
                        if getattr(resp, "data", None):
                            inserted += 1
                            price = record.get("price_local")
                            if price is not None and not record.get("outlier_flagged"):
                                existing_prices.append(float(price))
                    except Exception as exc:
                        skipped += 1
                        logger.error("SDI DB save failed: %s", exc)

                metrics.inc("records_processed")
                audit.log(
                    "record_processed",
                    "sdi_sfda",
                    {
                        "product_id": record.get("product_id"),
                        "regulatory_id": record.get("regulatory_id"),
                        "confidence": record.get("confidence"),
                    },
                )

    metrics.inc("crawl_success")
    metrics.observe("crawl_duration_sec", time.time() - t_start)
    reputation.update("sdi_sfda", True)
    audit.log("crawl_finished", "sdi_sfda", {"inserted": inserted, "skipped": skipped})
    return {
        "rows_inserted": inserted,
        "rows_updated": 0,
        "rows_skipped": skipped,
        "audit_log": audit.to_json(),
        "metrics": metrics.to_json(),
    }
