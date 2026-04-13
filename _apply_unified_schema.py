"""
Unified schema 적용 스크립트 (1회 실행 후 삭제)
Supabase Management API를 사용하여 12개국 통일 테이블을 생성한다.
"""
import httpx
import sys

PROJECT_REF = "oynefikqoibwtfpjlizv"
API_URL = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"
TOKEN = "sbp_a713d02e569a99e93101ca8ec3a6d184b5bd6ace"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# Each SQL statement as a separate string to avoid quoting conflicts
STATEMENTS = [
    # --- immutable_date function (needed for dedup index) ---
    """
    CREATE OR REPLACE FUNCTION immutable_date(ts timestamptz)
    RETURNS date
    LANGUAGE sql IMMUTABLE
    AS 'SELECT ts::date';
    """,

    # --- 1. products ---
    """
    CREATE TABLE IF NOT EXISTS products (
      id                    uuid primary key default gen_random_uuid(),
      country               text not null,
      product_id            text not null,
      trade_name            text not null,
      active_ingredient     text,
      inn_name              text,
      strength              text,
      strength_normalized   text,
      dosage_form           text,
      dosage_form_normalized text,
      manufacturer          text,
      price                 numeric(12,4),
      price_currency        text,
      price_usd             numeric(12,4),
      market_segment        text not null
                            check (market_segment in
                              ('retail','tender','wholesale','combo_drug')),
      registration_number   text,
      source_name           text not null,
      source_url            text not null,
      source_tier           int not null
                            check (source_tier between 1 and 5),
      confidence            numeric(3,2) not null
                            check (confidence >= 0.00 and confidence < 1.00),
      fob_estimated_usd     numeric(12,4),
      outlier_flagged       boolean default false,
      anomaly_reason        text,
      inn_id                text,
      inn_match_type        text
                            check (inn_match_type in
                              ('exact','partial','substring','fuzzy','brand','none')),
      country_specific      jsonb,
      raw_payload           jsonb,
      crawled_at            timestamptz not null default now(),
      created_at            timestamptz not null default now(),
      deleted_at            timestamptz,
      deletion_reason       text
    );
    """,

    # products indexes
    "CREATE INDEX IF NOT EXISTS idx_products_country ON products (country);",
    "CREATE INDEX IF NOT EXISTS idx_products_country_ingredient ON products (country, active_ingredient) WHERE deleted_at IS NULL;",
    "CREATE INDEX IF NOT EXISTS idx_products_country_source ON products (country, source_name, crawled_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_products_inn ON products (inn_name) WHERE inn_name IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_products_country_time ON products (country, crawled_at DESC);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_dedup_daily ON products (country, source_name, source_url, immutable_date(crawled_at)) WHERE deleted_at IS NULL;",

    # --- 2. sources ---
    """
    CREATE TABLE IF NOT EXISTS sources (
      id                    text primary key,
      country               text not null,
      name                  text not null,
      url                   text,
      tier                  int not null check (tier between 1 and 5),
      category              text,
      access_method         text,
      enabled               boolean default true,
      confidence_default    numeric(3,2),
      rate_limit_qps        numeric,
      workflow              text,
      created_at            timestamptz default now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_sources_country ON sources (country);",

    # --- 3. crawl_runs ---
    """
    CREATE TABLE IF NOT EXISTS crawl_runs (
      id                    bigserial primary key,
      country               text not null,
      workflow              text not null,
      toggle_id             text,
      github_run_id         text,
      status                text check (status in ('running','succeeded','failed','partial')),
      rows_inserted         int default 0,
      rows_updated          int default 0,
      error_summary         text,
      started_at            timestamptz not null default now(),
      finished_at           timestamptz
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_crawl_runs_country ON crawl_runs (country, started_at DESC);",

    # --- 4. circuit_state ---
    """
    CREATE TABLE IF NOT EXISTS circuit_state (
      domain                text primary key,
      country               text not null,
      state                 text not null default 'closed'
                            check (state in ('closed','open','half_open')),
      failures              int not null default 0,
      opened_at             timestamptz,
      updated_at            timestamptz not null default now()
    );
    """,

    # --- 5. api_quota ---
    """
    CREATE TABLE IF NOT EXISTS api_quota (
      domain                text primary key,
      country               text not null,
      tokens_current        numeric not null,
      tokens_max            numeric not null,
      refill_rate           numeric not null,
      last_refill           timestamptz not null default now()
    );
    """,

    # --- 6. failed_urls ---
    """
    CREATE TABLE IF NOT EXISTS failed_urls (
      url                   text primary key,
      country               text not null,
      source_name           text not null,
      fail_count            int not null default 1,
      last_error            text,
      last_error_type       text,
      first_failed_at       timestamptz not null default now(),
      last_failed_at        timestamptz not null default now(),
      dead                  boolean not null default false
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_failed_urls_country ON failed_urls (country, source_name, dead);",

    # --- 7. ai_discovered_sources ---
    """
    CREATE TABLE IF NOT EXISTS ai_discovered_sources (
      id                    uuid primary key default gen_random_uuid(),
      country               text not null,
      url                   text not null,
      domain                text not null,
      category              text,
      relevance_score       numeric(3,2),
      has_price_data        boolean default false,
      has_product_listing   boolean default false,
      scraper_xpaths        jsonb,
      last_crawled_at       timestamptz,
      crawl_count           int default 0,
      discovered_at         timestamptz not null default now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_sources_country ON ai_discovered_sources (country);",

    # --- 8. audit_log ---
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id                    bigserial primary key,
      country               text not null,
      event_type            text not null,
      source_name           text,
      payload               jsonb,
      created_at            timestamptz not null default now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_log_country ON audit_log (country, created_at DESC);",
]

# RPC function — separate because of $$ quoting
FUNC_SQL = (
    "CREATE OR REPLACE FUNCTION failed_urls_bump(\n"
    "    p_url text,\n"
    "    p_country text,\n"
    "    p_source_name text,\n"
    "    p_error text,\n"
    "    p_error_type text,\n"
    "    p_poison_threshold int default 3\n"
    ") RETURNS void\n"
    "LANGUAGE plpgsql\n"
    "AS $$\n"
    "BEGIN\n"
    "    INSERT INTO failed_urls (\n"
    "        url, country, source_name, fail_count, last_error, last_error_type,\n"
    "        first_failed_at, last_failed_at, dead\n"
    "    ) VALUES (\n"
    "        p_url, p_country, p_source_name, 1, p_error, p_error_type,\n"
    "        now(), now(), false\n"
    "    )\n"
    "    ON CONFLICT (url) DO UPDATE SET\n"
    "        fail_count     = failed_urls.fail_count + 1,\n"
    "        last_error     = excluded.last_error,\n"
    "        last_error_type = excluded.last_error_type,\n"
    "        last_failed_at = now(),\n"
    "        dead           = (failed_urls.fail_count + 1) >= p_poison_threshold;\n"
    "END;\n"
    "$$;"
)

# Saudi source seed data
SEED_SOURCES = [
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:sfda_api', 'SA', 'SFDA API', 'https://developer.sfda.gov.sa', 1, 'regulator', 'api', true, 0.92, 2.0, 'sa_api_light') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:sfda_drugs_list_html', 'SA', 'SFDA HTML', 'https://www.sfda.gov.sa', 1, 'regulator', 'html_scraping', true, 0.88, 0.5, 'sa_api_light') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:sfda_companies', 'SA', 'SFDA Companies', 'https://www.sfda.gov.sa', 1, 'regulator', 'html_scraping', true, 0.85, 0.5, 'sa_api_light') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:nupco_tenders', 'SA', 'NUPCO Tenders', 'https://www.nupco.com', 2, 'procurement', 'html_scraping', true, 0.80, 0.3, 'sa_procurement') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:etimad_api', 'SA', 'Etimad API', 'https://tenders.etimad.sa', 2, 'procurement', 'api', true, 0.80, 1.0, 'sa_procurement') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:nahdi_web', 'SA', 'Nahdi Pharmacy', 'https://www.nahdi.sa', 3, 'retail', 'html_scraping', true, 0.75, 0.3, 'sa_retail_mid') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:al_dawaa_web', 'SA', 'Al Dawaa Pharmacy', 'https://www.al-dawaa.com', 3, 'retail', 'html_scraping', true, 0.75, 0.3, 'sa_retail_mid') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:whites_web', 'SA', 'Whites Pharmacy', 'https://whites.sa', 3, 'retail', 'html_scraping', true, 0.70, 0.3, 'sa_retail_mid') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:tamer_group', 'SA', 'Tamer Group', 'https://www.tamergroup.com', 4, 'wholesale', 'html_scraping', true, 0.65, 0.2, 'sa_retail_mid') ON CONFLICT (id) DO NOTHING;",
    "INSERT INTO sources (id, country, name, url, tier, category, access_method, enabled, confidence_default, rate_limit_qps, workflow) VALUES ('SA:noon_saudi', 'SA', 'Noon Saudi', 'https://www.noon.com', 5, 'marketplace', 'html_scraping', false, 0.50, 0.2, 'sa_retail_mid') ON CONFLICT (id) DO NOTHING;",
]


def run():
    client = httpx.Client(timeout=30.0)
    all_sql = STATEMENTS + [FUNC_SQL] + SEED_SOURCES
    total = len(all_sql)
    success = 0
    errors = []

    for i, sql in enumerate(all_sql, 1):
        sql_clean = sql.strip()
        label = sql_clean[:60].replace("\n", " ")
        print(f"[{i}/{total}] {label}...")

        try:
            resp = client.post(API_URL, headers=HEADERS, json={"query": sql_clean})
            if resp.status_code == 201:
                success += 1
                print(f"  OK")
            else:
                print(f"  ERROR {resp.status_code}: {resp.text[:200]}")
                errors.append((i, label, resp.text[:200]))
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            errors.append((i, label, str(e)))

    print(f"\n{'='*50}")
    print(f"Result: {success}/{total} succeeded")
    if errors:
        print(f"\nFailed ({len(errors)}):")
        for idx, lbl, err in errors:
            print(f"  [{idx}] {lbl}: {err}")
        return 1
    else:
        print("All statements applied successfully!")
        return 0


if __name__ == "__main__":
    sys.exit(run())
