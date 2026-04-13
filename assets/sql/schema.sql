-- schema.sql — 사우디 의약품 데이터 메인 테이블
-- Supabase/PostgreSQL에서 바로 실행 가능
-- 공통 6컬럼은 프로젝트 전체 계약이라 절대 변경 금지

create table if not exists saudi_products (
  -- ─── 공통 6컬럼 (프로젝트 헌법 — 불변) ──────────────
  id                                 uuid primary key default gen_random_uuid(),
  product_id                         text not null,
  market_segment                     text not null
                                     check (market_segment in
                                       ('retail','tender','wholesale','combo_drug')),
  fob_estimated_usd                  numeric(12,4),
  confidence                         numeric(3,2) not null
                                     check (confidence >= 0.00 and confidence < 1.00),
  crawled_at                         timestamptz not null default now(),

  -- ─── 의약품 도메인 공통 2컬럼 ────────────────────
  regulatory_id                      text,
  trade_name                         text not null,

  -- ─── 사우디 확장 컬럼 ─────────────────────────────
  scientific_name                    text,
  strength                           text,
  dosage_form                        text,
  price_sar                          numeric(10,2),
  manufacturer_or_marketing_company  text,
  agent_or_supplier                  text,
  atc_code                           text,

  -- ─── INN 정규화 결과 (inn_normalizer.py 출력) ─────
  inn_name                           text,          -- WHO INN 표준명
  inn_id                             text,          -- ATC 코드 등 참조 ID
  inn_match_type                     text
                                     check (inn_match_type in
                                       ('exact','partial','substring','fuzzy','brand','none')),

  source_url                         text not null,
  source_tier                        int  not null check (source_tier between 1 and 5),
  source_name                        text not null,
  raw_payload                        jsonb,

  outlier_flagged                    boolean default false,
  anomaly_reason                     text,
  toggle_id                          text check (toggle_id ~ '^toggle_[1-8]$'),

  deleted_at                         timestamptz,
  deletion_reason                    text
);

-- 조회 인덱스
create index if not exists idx_saudi_products_regid
  on saudi_products (regulatory_id) where deleted_at is null;

create index if not exists idx_saudi_products_trade_name
  on saudi_products (trade_name) where deleted_at is null;

create index if not exists idx_saudi_products_crawled_at
  on saudi_products (crawled_at desc);

create index if not exists idx_saudi_products_source
  on saudi_products (source_name, crawled_at desc);

create index if not exists idx_saudi_products_product_id
  on saudi_products (product_id) where deleted_at is null;

-- ATC 코드 범위 검색용
create index if not exists idx_saudi_products_atc
  on saudi_products (atc_code) where atc_code is not null;

-- INN 기준 교차 비교용 (FOB 역산에서 같은 INN끼리 가격 비교)
create index if not exists idx_saudi_products_inn
  on saudi_products (inn_name) where inn_name is not null;

-- 가격 이상치 분석용
create index if not exists idx_saudi_products_price
  on saudi_products (regulatory_id, price_sar)
  where price_sar is not null and deleted_at is null;


-- ─── 멱등 upsert 지원 뷰 ─────────────────────────────
-- 동일 (source_name, source_url, crawled_at::date)는 재삽입 금지.
-- UNIQUE 제약이 아닌 이유: 동일 URL에서 시계열 추적이 필요할 수 있음
create unique index if not exists idx_saudi_products_dedup_daily
  on saudi_products (source_name, source_url, (crawled_at::date))
  where deleted_at is null;


-- ─── 코멘트 ──────────────────────────────────────────
comment on table saudi_products is
  '사우디 의약품 수집 데이터. 공통 6컬럼 불변, 의약품 2컬럼 공통, 사우디 확장';

comment on column saudi_products.confidence is
  '0.00~0.99. 1.00 금지. 소스 tier + 매칭 결과로 차등 부여';

comment on column saudi_products.fob_estimated_usd is
  '크롤러는 null로 두고, FOB 역산 모듈이 나중에 업데이트';

comment on column saudi_products.raw_payload is
  'API JSON 응답은 OK. HTML 전문 저장 금지 (저작권)';
