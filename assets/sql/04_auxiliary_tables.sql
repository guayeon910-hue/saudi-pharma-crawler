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
