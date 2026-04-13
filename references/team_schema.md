# 12개국 통일 Supabase 스키마

> 유나이티드제약 해외 시장조사 자동화 — 전 팀 공통 DB 구조
> 작성: 사우디 크롤러 기준 (가장 성숙한 구현체에서 추출)
> Supabase 1개 계정으로 12개국 데이터를 통합 관리한다

## 설계 원칙

```
1. 1테이블 + country 컬럼 (국가별 테이블 분리 금지)
   → 쿼리 1번으로 12개국 비교 가능
   → UI에서 국가 필터만 바꾸면 됨

2. 공통 컬럼은 불변 (각 국가가 마음대로 추가/삭제 금지)
   → 국가 고유 필드는 country_specific jsonb에 넣을 것

3. country 코드는 ISO 3166-1 alpha-2 (대문자)
   → SA, IN, EG, VN, PH, ID, KE, NG, MX, BR, PK, BD 등
```

---

## 1. products — 메인 테이블

**모든 국가의 크롤링 데이터가 이 테이블 하나에 적재된다.**

```sql
create table if not exists products (
  -- ─── 식별 ──────────────────────────────────────
  id                    uuid primary key default gen_random_uuid(),
  country               text not null,              -- ISO 3166-1: SA, IN, EG ...
  product_id            text not null,               -- 소스별 고유 ID

  -- ─── 의약품 공통 필드 ──────────────────────────
  trade_name            text not null,               -- 제품명 (브랜드)
  active_ingredient     text,                        -- 성분명 원본
  inn_name              text,                        -- WHO INN 표준명
  strength              text,                        -- 함량 원본 (500mg)
  strength_normalized   text,                        -- 정규화 (500 mg)
  dosage_form           text,                        -- 제형 원본 (Cap.)
  dosage_form_normalized text,                       -- 정규화 (capsule)
  manufacturer          text,                        -- 제조사

  -- ─── 가격 ──────────────────────────────────────
  price                 numeric(12,4),               -- 현지 통화 가격
  price_currency        text,                        -- SAR, INR, EGP ...
  price_usd             numeric(12,4),               -- USD 환산 (비교용)

  -- ─── 분류 ──────────────────────────────────────
  market_segment        text not null                -- retail / tender / wholesale / combo_drug
                        check (market_segment in
                          ('retail','tender','wholesale','combo_drug')),
  registration_number   text,                        -- 각국 허가번호

  -- ─── 소스 정보 ─────────────────────────────────
  source_name           text not null,               -- sfda_api, cdsco_web, ai_discovered:xxx
  source_url            text not null,
  source_tier           int not null                 -- 1=정부, 2=공공조달, 3=소매, 4=도매, 5=마켓플레이스
                        check (source_tier between 1 and 5),

  -- ─── 품질 지표 ─────────────────────────────────
  confidence            numeric(3,2) not null        -- 0.00~0.99
                        check (confidence >= 0.00 and confidence < 1.00),
  fob_estimated_usd     numeric(12,4),               -- FOB 역산 (나중에 업데이트)
  outlier_flagged       boolean default false,
  anomaly_reason        text,

  -- ─── INN 매칭 결과 ─────────────────────────────
  inn_id                text,
  inn_match_type        text
                        check (inn_match_type in
                          ('exact','partial','substring','fuzzy','brand','none')),

  -- ─── 국가별 특수 필드 (이것만 자유) ────────────
  country_specific      jsonb,
  -- 예시:
  --   SA: {"sfda_reg_no": "...", "atc_code": "C10AA01", "agent": "..."}
  --   IN: {"cdsco_id": "...", "mrp": 150.0, "dpco_ceiling": 140.0}
  --   EG: {"eda_reg_no": "...", "eda_category": "A"}

  -- ─── 메타 ──────────────────────────────────────
  raw_payload           jsonb,                       -- API JSON OK, HTML 전문 저장 금지
  crawled_at            timestamptz not null default now(),
  created_at            timestamptz not null default now(),
  deleted_at            timestamptz,
  deletion_reason       text
);

-- ─── 인덱스 ──────────────────────────────────────
-- 국가별 조회 (기본)
create index if not exists idx_products_country
  on products (country);

-- 국가 + 성분 검색
create index if not exists idx_products_country_ingredient
  on products (country, active_ingredient)
  where deleted_at is null;

-- 국가 + 소스별 최신
create index if not exists idx_products_country_source
  on products (country, source_name, crawled_at desc);

-- INN 기준 12개국 교차 비교 (핵심!)
create index if not exists idx_products_inn
  on products (inn_name)
  where inn_name is not null;

-- 시계열 가격 추이
create index if not exists idx_products_country_time
  on products (country, crawled_at desc);

-- 일일 멱등 upsert
create unique index if not exists idx_products_dedup_daily
  on products (country, source_name, source_url, (crawled_at::date))
  where deleted_at is null;
```

### country_specific 사용 규칙

```
DO:
  {"sfda_reg_no": "SFDA-2024-001234"}     ← 각국 허가번호 형식
  {"mrp": 150.0, "dpco_ceiling": 140.0}   ← 인도 최대소매가격/약가상한
  {"eda_category": "A"}                    ← 이집트 EDA 분류

DON'T:
  {"product_name": "Panadol"}              ← 공통 컬럼에 이미 있음
  {"price": 12.50}                         ← 공통 컬럼에 이미 있음
  {"huge_html_blob": "<html>..."}          ← raw_payload에도 HTML 금지
```

---

## 2. sources — 소스 마스터

```sql
create table if not exists sources (
  id                    text primary key,            -- SA:sfda_api, IN:cdsco_web
  country               text not null,
  name                  text not null,               -- 소스 표시 이름
  url                   text,                        -- 베이스 URL
  tier                  int not null check (tier between 1 and 5),
  category              text,                        -- 공공조달 / 민간 / 논문
  access_method         text,                        -- api / html_scraping / ai_discovered
  enabled               boolean default true,
  confidence_default    numeric(3,2),
  rate_limit_qps        numeric,
  workflow              text,                        -- Actions 워크플로명
  created_at            timestamptz default now()
);

create index if not exists idx_sources_country
  on sources (country);
```

### 각 팀이 해야 할 것

소스를 등록해라. 예시:

```sql
-- 사우디 (10개)
insert into sources values ('SA:sfda_api', 'SA', 'SFDA 의약품 API', 'https://developer.sfda.gov.sa', 1, '공공조달', 'api', true, 0.92, 2.0, 'sa_api_light');
insert into sources values ('SA:nahdi_web', 'SA', 'Nahdi 약국', 'https://www.nahdi.sa', 3, '민간', 'html_scraping', true, 0.75, 0.3, 'sa_retail_mid');

-- 인도 (예시)
insert into sources values ('IN:cdsco_web', 'IN', 'CDSCO 의약품 DB', 'https://cdsco.gov.in', 1, '공공조달', 'html_scraping', true, 0.90, 0.5, 'in_regulator');
insert into sources values ('IN:1mg_web', 'IN', '1mg 온라인약국', 'https://www.1mg.com', 3, '민간', 'html_scraping', true, 0.70, 0.3, 'in_retail');
```

---

## 3. crawl_runs — 크롤링 실행 이력

```sql
create table if not exists crawl_runs (
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

create index if not exists idx_crawl_runs_country
  on crawl_runs (country, started_at desc);
```

---

## 4. circuit_state — 서킷 브레이커

```sql
create table if not exists circuit_state (
  domain                text primary key,
  country               text not null,
  state                 text not null default 'closed'
                        check (state in ('closed','open','half_open')),
  failures              int not null default 0,
  opened_at             timestamptz,
  updated_at            timestamptz not null default now()
);
```

---

## 5. api_quota — 토큰 버킷

```sql
create table if not exists api_quota (
  domain                text primary key,
  country               text not null,
  tokens_current        numeric not null,
  tokens_max            numeric not null,
  refill_rate           numeric not null,
  last_refill           timestamptz not null default now()
);
```

---

## 6. failed_urls — 실패 URL 큐

```sql
create table if not exists failed_urls (
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

create index if not exists idx_failed_urls_country
  on failed_urls (country, source_name, dead);
```

---

## 7. ai_discovered_sources — AI 발견 소스

```sql
create table if not exists ai_discovered_sources (
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

create index if not exists idx_ai_sources_country
  on ai_discovered_sources (country);
```

---

## 8. audit_log — 감사 로그

```sql
create table if not exists audit_log (
  id                    bigserial primary key,
  country               text not null,
  event_type            text not null,
  source_name           text,
  payload               jsonb,
  created_at            timestamptz not null default now()
);

create index if not exists idx_audit_log_country
  on audit_log (country, created_at desc);
```

---

## RPC 함수

```sql
-- 실패 URL 원자적 증가 (각 국가 크롤러 공용)
create or replace function failed_urls_bump(
    p_url text,
    p_country text,
    p_source_name text,
    p_error text,
    p_error_type text,
    p_poison_threshold int default 3
) returns void
language plpgsql
as $$
begin
    insert into failed_urls (
        url, country, source_name, fail_count, last_error, last_error_type,
        first_failed_at, last_failed_at, dead
    ) values (
        p_url, p_country, p_source_name, 1, p_error, p_error_type,
        now(), now(), false
    )
    on conflict (url) do update set
        fail_count     = failed_urls.fail_count + 1,
        last_error     = excluded.last_error,
        last_error_type = excluded.last_error_type,
        last_failed_at = now(),
        dead           = (failed_urls.fail_count + 1) >= p_poison_threshold;
end;
$$;
```

---

## 팀원 확인 사항 (회신 필요)

이 스키마를 기준으로 작업합니다. 아래 3가지만 회신해주세요:

### 1. 국가별 특수 필드

`country_specific` jsonb 컬럼에 넣을 국가 고유 데이터가 있으면 알려주세요.

예시:
```
SA (사우디): sfda_reg_no, atc_code, agent_or_supplier
IN (인도):   cdsco_id, mrp, dpco_ceiling, schedule_category
EG (이집트): eda_reg_no, eda_category
```

### 2. 크롤링 소스 리스트

각 국가의 크롤링 소스를 아래 형식으로 보내주세요:

```
국가: XX
소스명 | URL | tier (1~5) | 카테고리 | 접근 방식
─────────────────────────────────────────────────
1. ??? 의약품 DB | https://... | 1 | 공공조달 | api
2. ??? 온라인약국 | https://... | 3 | 민간 | html_scraping
```

tier 기준:
- 1: 정부/규제기관 (SFDA, CDSCO, EDA 등)
- 2: 공공조달 (NUPCO, GeM 등)
- 3: 소매약국 (Nahdi, 1mg, Seif 등)
- 4: 도매/유통 (Tamer, Zuellig 등)
- 5: 마켓플레이스 (Noon, Amazon 등)

### 3. 현재 진행률

이미 DB 구조를 만들었거나 크롤러를 작성했으면 알려주세요.
기존 구조가 이 스키마와 다르면 마이그레이션을 도와드립니다.

---

## price_currency 코드 (참고)

| 국가 | 코드 | 통화 |
|------|------|------|
| SA | SAR | Saudi Riyal |
| IN | INR | Indian Rupee |
| EG | EGP | Egyptian Pound |
| VN | VND | Vietnamese Dong |
| PH | PHP | Philippine Peso |
| ID | IDR | Indonesian Rupiah |
| KE | KES | Kenyan Shilling |
| NG | NGN | Nigerian Naira |
| MX | MXN | Mexican Peso |
| BR | BRL | Brazilian Real |
| PK | PKR | Pakistani Rupee |
| BD | BDT | Bangladeshi Taka |

---

## 마이그레이션 참고: 사우디 기존 → 통일 스키마

사우디 크롤러(`saudi_products`)에서 통일 스키마(`products`)로 전환 시:

```sql
-- 기존 데이터 마이그레이션
insert into products (
  country, product_id, trade_name, active_ingredient, inn_name,
  strength, dosage_form, manufacturer, price, price_currency,
  market_segment, registration_number, source_name, source_url,
  source_tier, confidence, fob_estimated_usd, outlier_flagged,
  inn_id, inn_match_type, raw_payload, crawled_at,
  country_specific
)
select
  'SA', product_id, trade_name, scientific_name, inn_name,
  strength, dosage_form, manufacturer_or_marketing_company,
  price_sar, 'SAR',
  market_segment, regulatory_id, source_name, source_url,
  source_tier, confidence, fob_estimated_usd, outlier_flagged,
  inn_id, inn_match_type, raw_payload, crawled_at,
  jsonb_build_object(
    'atc_code', atc_code,
    'agent_or_supplier', agent_or_supplier,
    'toggle_id', toggle_id
  )
from saudi_products
where deleted_at is null;
```
