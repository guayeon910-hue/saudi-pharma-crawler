-- =============================================================================
-- supabase_bootstrap.sql — Supabase SQL Editor에서 프로젝트당 1회 실행
-- = team-kit/sql/01_products.sql + state_tables + migration + 04_auxiliary_tables
-- =============================================================================

-- 01_products.sql — 12개국 통합 의약품 테이블
-- Supabase Pro SQL 에디터에서 1회 실행
-- 모든 나라 크롤러가 이 테이블에 INSERT한다
--
-- ⚠️ 이 테이블은 팀 전체 계약이다. 컬럼 추가는 PM 승인 필요.
--    각 나라 전용 필드는 raw_payload(JSONB)에 넣을 것.

create table if not exists products (
  -- ─── 공통 6컬럼 (프로젝트 헌법 — 변경 금지) ──────────
  id                                 uuid primary key default gen_random_uuid(),
  product_id                         text not null,
  market_segment                     text not null
                                     check (market_segment in
                                       ('retail','tender','wholesale','combo_drug')),
  fob_estimated_usd                  numeric(12,4),
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  crawled_at                         timestamptz not null default now(),

  -- ─── 나라 식별 ────────────────────────────────────────
  country                            text not null,      -- ISO 3166-1 alpha-2: SA, SG, VN, EG...
  currency                           text not null,      -- ISO 4217: SAR, SGD, VND, EGP...

  -- ─── 의약품 공통 ─────────────────────────────────────
  regulatory_id                      text,               -- 각 나라 규제기관 등록번호
  trade_name                         text not null,

  -- ─── 8필드 공통 스키마 ────────────────────────────────
  scientific_name                    text,
  strength                           text,               -- "500 mg" 형식
  dosage_form                        text,               -- "tablet", "capsule" 등
  price_local                        numeric(12,2),      -- 현지 통화 가격
  manufacturer_or_marketing_company  text,
  agent_or_supplier                  text,
  atc_code                           text,

  -- ─── INN 정규화 결과 (inn_normalizer.py 출력) ─────────
  inn_name                           text,               -- WHO INN 표준명
  inn_id                             text,               -- ATC 코드 등 참조 ID
  inn_match_type                     text
                                     check (inn_match_type is null or
                                       inn_match_type in
                                       ('exact','partial','substring','fuzzy','brand','none')),

  -- ─── 메타 ─────────────────────────────────────────────
  source_url                         text not null,
  source_tier                        int not null check (source_tier between 1 and 5),
  source_name                        text not null,      -- "sfda_api", "hsa_api", "nahdi_web" 등
  raw_payload                        jsonb,              -- 나라별 확장 필드는 여기에

  outlier_flagged                    boolean default false,
  anomaly_reason                     text,
  toggle_id                          text,

  deleted_at                         timestamptz,
  deletion_reason                    text
);

-- ─── 인덱스 ──────────────────────────────────────────────
-- 나라별 조회 (가장 빈번)
create index if not exists idx_products_country
  on products (country, crawled_at desc);

-- 나라 + 소스별 조회
create index if not exists idx_products_country_source
  on products (country, source_name, crawled_at desc);

-- FOB 교차비교: 같은 INN 12개국 가격 비교 (프로젝트 핵심 쿼리)
create index if not exists idx_products_inn_country
  on products (inn_name, country)
  where inn_name is not null and deleted_at is null;

-- 규제 등록번호
create index if not exists idx_products_regid
  on products (country, regulatory_id)
  where regulatory_id is not null and deleted_at is null;

-- 상품명 검색
create index if not exists idx_products_trade_name
  on products (trade_name) where deleted_at is null;

-- product_id 유니크 조회
create index if not exists idx_products_product_id
  on products (product_id) where deleted_at is null;

-- ATC 코드 범위 검색
create index if not exists idx_products_atc
  on products (atc_code) where atc_code is not null;

-- 가격 이상치 분석 (같은 INN + 나라 내)
create index if not exists idx_products_price
  on products (inn_name, country, price_local)
  where price_local is not null and deleted_at is null;

-- 일일 중복 방지
create unique index if not exists idx_products_dedup_daily
  on products (country, source_name, source_url, (crawled_at::date))
  where deleted_at is null;


-- ─── 코멘트 ──────────────────────────────────────────────
comment on table products is
  '12개국 의약품 통합 수집 테이블. 공통 6컬럼 불변. 나라별 확장은 raw_payload에.';

comment on column products.country is
  'ISO 3166-1 alpha-2. SA=사우디, SG=싱가포르, VN=베트남 등';

comment on column products.currency is
  'ISO 4217. SAR, SGD, VND, EGP 등. price_local의 통화 단위';

comment on column products.price_local is
  '현지 통화 가격. USD 환산은 fob_estimated_usd가 담당 (2공정)';

comment on column products.raw_payload is
  'API JSON 응답 OK. HTML 전문 저장 금지 (저작권). 나라별 확장 필드도 여기에';

-- PostgREST upsert(..., on_conflict=product_id) 에 필수 (UNIQUE)
create unique index if not exists idx_products_product_id_unique on products (product_id);

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

-- 01_add_error_type_and_update_rpc.sql
-- Idempotent migration: add error_type tracking + update RPC signature

-- 1) Add nullable column to avoid conflicts with existing rows.
alter table if exists public.saudi_failed_urls
  add column if not exists last_error_type text;

-- 2) Prevent RPC overloading by dropping exact signatures.
drop function if exists public.saudi_failed_urls_bump(text, text, text, text, integer);
drop function if exists public.saudi_failed_urls_bump(text, text, text, integer);

-- 3) Recreate RPC with error_type parameter.
create function public.saudi_failed_urls_bump(
    p_url text,
    p_source_name text,
    p_error text,
    p_error_type text,
    p_poison_threshold integer
) returns void
language plpgsql
as $$
begin
    insert into public.saudi_failed_urls (
        url,
        source_name,
        fail_count,
        last_error,
        last_error_type,
        first_failed_at,
        last_failed_at,
        dead
    ) values (
        p_url,
        p_source_name,
        1,
        p_error,
        p_error_type,
        now(),
        now(),
        false
    )
    on conflict (url) do update set
        fail_count       = public.saudi_failed_urls.fail_count + 1,
        last_error       = excluded.last_error,
        last_error_type  = excluded.last_error_type,
        last_failed_at   = now(),
        dead             = (public.saudi_failed_urls.fail_count + 1) >= p_poison_threshold;
end;
$$;


-- 04_auxiliary_tables.sql — 크롤러가 products 외에 쓰는 보조 테이블
-- supabase_bootstrap.sql 안에서 products + state 이후에 실행됨

-- ─── SFDA 제약사 마스터 ───────────────────────────────────
create table if not exists companies (
  company_id                         text primary key,
  country                            text not null default 'SA',
  company_register                   text,
  company_name                       text,
  company_address                    text,
  drug_type                          text,
  production_line                    text,
  country_desc                       text,
  agent_name                         text,
  agent_address                      text,
  source_url                         text not null,
  source_name                        text not null,
  source_tier                        int not null default 1,
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  raw_payload                        jsonb,
  crawled_at                         timestamptz not null default now()
);

create index if not exists idx_companies_country on companies (country, crawled_at desc);


-- ─── NUPCO 텐더 ─────────────────────────────────────────
create table if not exists tenders (
  tender_id                          text primary key,
  country                            text not null default 'SA',
  source_name                        text not null,
  source_tier                        int not null default 2,
  source_url                         text,
  title                              text,
  tender_number                      text,
  posting_date                       timestamptz,
  closing_date                       timestamptz,
  pdf_urls                           jsonb default '[]'::jsonb,
  market_segment                     text not null default 'tender',
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  raw_payload                        jsonb,
  crawled_at                         timestamptz not null default now()
);

create index if not exists idx_tenders_country on tenders (country, crawled_at desc);


-- ─── Etimad 공공 계약 ────────────────────────────────────
create table if not exists contracts (
  contract_id                        text primary key,
  country                            text not null default 'SA',
  source_name                        text not null,
  source_tier                        int not null default 2,
  market_segment                     text not null default 'tender',
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  title                              text,
  supplier_name                      text,
  contract_value                     numeric(18,2),
  currency                           text not null default 'SAR',
  start_date                         text,
  end_date                           text,
  status                             text,
  category                           text,
  source_url                         text,
  raw_payload                        jsonb,
  crawled_at                         timestamptz not null default now()
);

create index if not exists idx_contracts_country on contracts (country, crawled_at desc);


-- ─── Tamer Group 등 공급사 마스터 ─────────────────────────
create table if not exists suppliers (
  supplier_id                        text primary key,
  country                            text not null default 'SA',
  supplier_name                      text,
  supplier_type                      text,
  parent_company                     text,
  sector                             text,
  source_url                         text not null,
  source_name                        text not null,
  source_tier                        int not null default 4,
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  raw_payload                        jsonb,
  crawled_at                         timestamptz not null default now()
);

create index if not exists idx_suppliers_country on suppliers (country, crawled_at desc);


-- ─── AI 자율 서칭: 발견 소스 메타 ─────────────────────────
create table if not exists ai_discovered_sources (
  id                                 uuid primary key default gen_random_uuid(),
  country                            text not null default 'SA',
  url                                text not null,
  domain                             text,
  category                           text,
  relevance_score                    numeric(5,4),
  has_price_data                     boolean default false,
  has_product_listing                boolean default false,
  scraper_xpaths                     jsonb,
  last_crawled_at                    timestamptz,
  crawl_count                        int not null default 0,
  created_at                         timestamptz not null default now()
);

create unique index if not exists idx_ai_sources_url on ai_discovered_sources (url);
create index if not exists idx_ai_sources_country on ai_discovered_sources (country, last_crawled_at desc);


-- ─── AI 자율 서칭: 추출 레코드 (느슨한 스키마) ─────────────
create table if not exists ai_discovered_products (
  id                                 uuid primary key default gen_random_uuid(),
  country                            text not null default 'SA',
  product_name                       text,
  price                              text,
  manufacturer                       text,
  active_ingredient                  text,
  strength                           text,
  strength_normalized                text,
  source                             text,
  source_url                         text,
  confidence                         numeric(3,2)
                                     check (confidence is null or (confidence >= 0.00 and confidence <= 1.00)),
  inn_name                           text,
  crawled_at                         timestamptz not null default now()
);

create index if not exists idx_ai_products_country on ai_discovered_products (country, crawled_at desc);

