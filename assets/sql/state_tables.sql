-- state_tables.sql — 크롤러 런타임 상태 저장용 부가 테이블
-- GitHub Actions 러너는 휘발성이므로 상태는 전부 Supabase에 있어야 한다
-- (로컬 파일, 메모리 캐시는 잡 종료와 함께 증발)

-- ─── 1. 실패 큐 (재시도 영속화) ──────────────────────
create table if not exists saudi_failed_urls (
  url              text not null unique primary key,
  source_name      text not null,
  fail_count       int  not null default 1,
  last_error       text,
  last_error_type  text,
  first_failed_at  timestamptz not null default now(),
  last_failed_at   timestamptz not null default now(),
  dead             boolean not null default false  -- fail_count >= 3
);

create index if not exists idx_failed_urls_source
  on saudi_failed_urls (source_name, dead);

create index if not exists idx_failed_urls_retry
  on saudi_failed_urls (last_failed_at)
  where dead = false;


-- ─── 2. 동적 셀렉터 캐시 (TTL 1시간) ─────────────────
create table if not exists saudi_selector_cache (
  domain         text not null,
  page_type      text not null,        -- 'article' | 'product' | 'list'
  selector       text not null,
  success_count  int  not null default 1,
  fail_count     int  not null default 0,
  last_used      timestamptz not null default now(),
  expires_at     timestamptz not null default (now() + interval '1 hour'),
  primary key (domain, page_type)
);

create index if not exists idx_selector_cache_expires
  on saudi_selector_cache (expires_at);


-- ─── 3. API 쿼터 / 토큰 버킷 ─────────────────────────
create table if not exists saudi_api_quota (
  domain          text primary key,
  tokens_current  numeric not null,
  tokens_max      numeric not null,
  refill_rate     numeric not null,    -- tokens per second
  last_refill     timestamptz not null default now()
);

-- 초기값 시드 (원하는 값으로 수정)
insert into saudi_api_quota (domain, tokens_current, tokens_max, refill_rate)
values
  ('developer.sfda.gov.sa', 10, 10, 2.0),
  ('www.sfda.gov.sa',       5, 5, 0.5),
  ('www.nahdionline.com',   3, 3, 0.3),
  ('www.al-dawaa.com',      3, 3, 0.3),
  ('www.whites.sa',         3, 3, 0.3),
  ('www.nupco.com',         3, 3, 0.3),
  ('apiportal.etimad.sa',   5, 5, 0.5)
on conflict (domain) do nothing;


-- ─── 4. 서킷 브레이커 상태 ────────────────────────────
create table if not exists saudi_circuit_state (
  domain      text primary key,
  state       text not null default 'closed'
              check (state in ('closed','open','half_open')),
  failures    int  not null default 0,
  opened_at   timestamptz,
  updated_at  timestamptz not null default now()
);


-- ─── 5. robots.txt 확인 로그 (컴플라이언스 증빙) ────
create table if not exists saudi_robots_log (
  id                bigserial primary key,
  domain            text not null,
  checked_at        timestamptz not null default now(),
  user_agent        text,
  allowed_paths     text[],
  disallowed_paths  text[],
  crawl_delay       int,
  raw_content       text
);

create index if not exists idx_robots_log_domain
  on saudi_robots_log (domain, checked_at desc);


-- ─── 6. 크롤링 잡 실행 이력 ──────────────────────────
create table if not exists saudi_crawl_runs (
  id            bigserial primary key,
  workflow      text not null,             -- 'sa_api_light' 등
  toggle_id     text,
  github_run_id text,                      -- GitHub Actions run ID
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text check (status in ('running','succeeded','failed','partial')),
  rows_inserted int default 0,
  rows_updated  int default 0,
  error_summary text
);

create index if not exists idx_crawl_runs_workflow
  on saudi_crawl_runs (workflow, started_at desc);


-- ─── 정기 정리 (cron jobs) ────────────────────────────
-- 만료된 셀렉터 캐시 삭제 (매시간)
-- 90일 지난 failed_urls 중 dead가 아닌 것만 정리
-- 90일 지난 robots_log 정리
-- → pg_cron 또는 Supabase scheduled function으로 구현

-- 예시 (pg_cron 확장 필요):
-- select cron.schedule(
--   'cleanup-selector-cache',
--   '0 * * * *',
--   $$ delete from saudi_selector_cache where expires_at < now() $$
-- );


-- ─────────────────────────────────────────────────────────
-- saudi_failed_urls_bump
-- FailedQueue.record_failure가 호출하는 원자 증가 함수.
-- upsert + fail_count 증가 + dead 플래그 갱신을 단일 트랜잭션으로 처리한다.
-- ─────────────────────────────────────────────────────────
create or replace function saudi_failed_urls_bump(
    p_url text,
    p_source_name text,
    p_error text,
    p_error_type text,
    p_poison_threshold int
) returns void
language plpgsql
as $$
begin
    insert into saudi_failed_urls (
        url, source_name, fail_count, last_error, last_error_type,
        first_failed_at, last_failed_at, dead
    ) values (
        p_url, p_source_name, 1, p_error, p_error_type,
        now(), now(), false
    )
    on conflict (url) do update set
        fail_count     = saudi_failed_urls.fail_count + 1,
        last_error     = excluded.last_error,
        last_error_type = excluded.last_error_type,
        last_failed_at = now(),
        dead           = (saudi_failed_urls.fail_count + 1) >= p_poison_threshold;
end;
$$;
