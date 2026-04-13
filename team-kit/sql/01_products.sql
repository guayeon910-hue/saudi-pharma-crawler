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
