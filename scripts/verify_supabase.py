"""
Supabase 연결 및 주요 테이블 행 수 확인.
프로젝트 루트에서:  python scripts/verify_supabase.py

필요: 환경변수 SUPABASE_URL, SUPABASE_SERVICE_KEY (.env 또는 export)
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY 가 설정되어 있지 않습니다.", file=sys.stderr)
        return 1

    try:
        from supabase import create_client
    except ImportError:
        print("supabase 패키지가 없습니다. pip install supabase", file=sys.stderr)
        return 1

    sb = create_client(url, key)

    def count_products_sa() -> object:
        return (
            sb.table("products")
            .select("*", count="exact")
            .eq("country", "SA")
            .limit(1)
            .execute()
        )

    checks: list[tuple[str, Callable[[], object]]] = [
        ("products (country=SA)", count_products_sa),
        ("saudi_crawl_runs", lambda: sb.table("saudi_crawl_runs").select("*", count="exact").limit(1).execute()),
        ("companies", lambda: sb.table("companies").select("*", count="exact").limit(1).execute()),
        ("tenders", lambda: sb.table("tenders").select("*", count="exact").limit(1).execute()),
        ("ai_discovered_sources", lambda: sb.table("ai_discovered_sources").select("*", count="exact").limit(1).execute()),
    ]

    print("Supabase:", url[:48] + "…" if len(url) > 48 else url)
    ok = True
    for name, fn in checks:
        try:
            r = fn()
            n = getattr(r, "count", None)
            print(f"  {name}: {n if n is not None else 0} rows")
        except Exception as e:
            ok = False
            print(f"  {name}: ERROR - {e}")

    if ok:
        print("\nOK. If a table is missing later, run assets/sql/supabase_bootstrap.sql in SQL Editor.")
    else:
        print("\nSome tables missing or error - run assets/sql/supabase_bootstrap.sql (or 04_auxiliary_tables.sql) in SQL Editor.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
