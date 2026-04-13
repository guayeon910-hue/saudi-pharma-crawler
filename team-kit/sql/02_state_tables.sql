-- 02_state_tables.sql — 크롤러 런타임 상태 테이블 (12개국 공용)
-- 01_products.sql 실행 후 이것도 실행

-- ─── 1. 실패 큐 ─────────────────────────────────────────
create table if not exists failed_urls (
  country          text not null,
  url              text not null,
  source_name      text not null,
  fail_count       int  not null default 1,
  last_error       text,
  last_error_type  text,
  first_failed_at  timestamptz not null default now(),
  last_failed_at   timestamptz not null default now(),
  dead             boolean not null default false,
  primary key (country, url)
);

create index if not exists idx_failed_urls_retry
  on failed_urls (country, last_failed_at)
  where dead = false;


-- ─── 2. 동적 셀렉터 캐시 (TTL 1시간) ───────────────────
create table if not exists selector_cache (
  country        text not null,
  domain         text not null,
  page_type      text not null,
  selector       text not null,
  success_count  int  not null default 1,
  fail_count     int  not null default 0,
  last_used      timestamptz not null default now(),
  expires_at     timestamptz not null default (now() + interval '1 hour'),
  primary key (country, domain, page_type)
);


-- ─── 3. API 쿼터 / 토큰 버킷 ────────────────────────────
create table if not exists api_quota (
  country         text not null,
  domain          text not null,
  tokens_current  numeric not null,
  tokens_max      numeric not null,
  refill_rate     numeric not null,
  last_refill     timestamptz not null default now(),
  primary key (country, domain)
);


-- ─── 4. 서킷 브레이커 ───────────────────────────────────
create table if not exists circuit_state (
  country     text not null,
  domain      text not null,
  state       text not null default 'closed'
              check (state in ('closed','open','half_open')),
  failures    int  not null default 0,
  opened_at   timestamptz,
  updated_at  timestamptz not null default now(),
  primary key (country, domain)
);


-- ─── 5. 크롤링 실행 이력 ────────────────────────────────
create table if not exists crawl_runs (
  id            bigserial primary key,
  country       text not null,
  workflow      text not null,
  toggle_id     text,
  github_run_id text,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text check (status in ('running','succeeded','failed','partial')),
  rows_inserted int default 0,
  rows_updated  int default 0,
  error_summary text
);

create index if not exists idx_crawl_runs_country
  on crawl_runs (country, started_at desc);


-- ─── 6. 실패 큐 원자 증가 함수 ──────────────────────────
create or replace function failed_urls_bump(
    p_country text,
    p_url text,
    p_source_name text,
    p_error text,
    p_error_type text,
    p_poison_threshold int
) returns void
language plpgsql
as $$
begin
    insert into failed_urls (
        country, url, source_name, fail_count, last_error, last_error_type,
        first_failed_at, last_failed_at, dead
    ) values (
        p_country, p_url, p_source_name, 1, p_error, p_error_type,
        now(), now(), false
    )
    on conflict (country, url) do update set
        fail_count      = failed_urls.fail_count + 1,
        last_error      = excluded.last_error,
        last_error_type = excluded.last_error_type,
        last_failed_at  = now(),
        dead            = (failed_urls.fail_count + 1) >= p_poison_threshold;
end;
$$;
