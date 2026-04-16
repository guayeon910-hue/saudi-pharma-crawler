# DATABASE.md — 사우디 크롤러 데이터베이스 구조

> Supabase (PostgreSQL) 기반. SQL 파일: `assets/sql/`
> 12개국 통일 스키마는 [team_schema.md](team_schema.md) 참고

## 테이블 전체 목록

```
┌──────────────────────────────────────────────────────────────┐
│                         Supabase                             │
│                                                              │
│  [데이터 테이블]                                               │
│  ┌─────────────────────┐  ┌─────────────────────────────┐    │
│  │ saudi_products       │  │ ai_discovered_sources       │    │
│  │ (메인 크롤링 데이터)  │  │ (AI가 발견한 소스 메타데이터) │    │
│  └─────────────────────┘  └─────────────────────────────┘    │
│                                                              │
│  [상태 테이블]                                                │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │ saudi_api_quota   │  │ saudi_circuit_state│                │
│  │ (토큰 버킷)       │  │ (서킷 브레이커)    │                │
│  └──────────────────┘  └──────────────────┘                  │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │ saudi_failed_urls │  │ saudi_selector_cache│               │
│  │ (실패 큐)         │  │ (셀렉터 캐시)      │                │
│  └──────────────────┘  └──────────────────┘                  │
│                                                              │
│  [이력 테이블]                                                │
│  ┌──────────────────┐  ┌──────────────────┐                  │
│  │ saudi_crawl_runs  │  │ saudi_robots_log  │                │
│  │ (크롤링 잡 이력)  │  │ (robots.txt 감사) │                │
│  └──────────────────┘  └──────────────────┘                  │
└──────────────────────────────────────────────────────────────┘
```

## 1. saudi_products — 메인 크롤링 데이터

> SQL: `assets/sql/schema.sql`

크롤러 10개 + AI 자율 서칭이 수집한 모든 의약품 데이터가 이 테이블에 적재된다.

### 컬럼

| 컬럼 | 타입 | 필수 | 설명 |
|------|------|------|------|
| `id` | uuid PK | auto | gen_random_uuid() |
| `product_id` | text | O | 소스별 고유 식별자 |
| `market_segment` | text | O | retail / tender / wholesale / combo_drug |
| `fob_estimated_usd` | numeric(12,4) | X | FOB 역산 모듈이 나중에 업데이트 |
| `confidence` | numeric(3,2) | O | 0.00~0.99 (1.00 금지). EMA 보정 후 값 |
| `crawled_at` | timestamptz | O | 크롤링 시점 (default now()) |
| `regulatory_id` | text | X | SFDA 등록번호 등 |
| `trade_name` | text | O | 제품명 |
| `scientific_name` | text | X | 성분명 |
| `strength` | text | X | 함량 원본 |
| `dosage_form` | text | X | 제형 원본 |
| `price_sar` | numeric(10,2) | X | 사우디 리얄 가격 |
| `manufacturer_or_marketing_company` | text | X | 제조사 |
| `agent_or_supplier` | text | X | 대리점/공급사 |
| `atc_code` | text | X | ATC 분류 코드 |
| `inn_name` | text | X | WHO INN 표준명 (inn_normalizer 결과) |
| `inn_id` | text | X | INN 참조 ID |
| `inn_match_type` | text | X | exact / partial / substring / fuzzy / brand / none |
| `source_url` | text | O | 원본 URL |
| `source_tier` | int | O | 1~5 |
| `source_name` | text | O | sfda_api / nahdi_web / ai_discovered:xxx |
| `raw_payload` | jsonb | X | API JSON 응답 (HTML 전문 저장 금지) |
| `outlier_flagged` | bool | X | IQR 이상치 여부 |
| `anomaly_reason` | text | X | 이상치 사유 |
| `toggle_id` | text | X | toggle_1 ~ toggle_8 |
| `deleted_at` | timestamptz | X | 소프트 삭제 |
| `deletion_reason` | text | X | 삭제 사유 |

### 인덱스

| 인덱스 | 대상 | 용도 |
|--------|------|------|
| `idx_saudi_products_regid` | regulatory_id | 허가번호 검색 |
| `idx_saudi_products_trade_name` | trade_name | 제품명 검색 |
| `idx_saudi_products_crawled_at` | crawled_at DESC | 시계열 조회 |
| `idx_saudi_products_source` | (source_name, crawled_at DESC) | 소스별 최신 |
| `idx_saudi_products_product_id` | product_id | 중복 검사 |
| `idx_saudi_products_atc` | atc_code | ATC 범위 검색 |
| `idx_saudi_products_inn` | inn_name | INN 기준 교차 비교 |
| `idx_saudi_products_price` | (regulatory_id, price_sar) | 가격 이상치 분석 |
| `idx_saudi_products_dedup_daily` | (source_name, source_url, date) UNIQUE | 일일 멱등 upsert |

### 공통 6컬럼 규칙

`id`, `product_id`, `market_segment`, `fob_estimated_usd`, `confidence`, `crawled_at` — 이 6개는 **프로젝트 전체 계약(불변)**. 12개국 공통.

## 2. 상태 테이블 (6개)

> SQL: `assets/sql/state_tables.sql`

GitHub Actions 러너는 휘발성이므로 런타임 상태를 전부 DB에 저장한다.

### 2-1. saudi_failed_urls — 실패 URL 큐

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `url` | text PK | 실패한 URL |
| `source_name` | text | 소스명 |
| `fail_count` | int | 누적 실패 횟수 |
| `last_error` | text | 마지막 에러 메시지 |
| `last_error_type` | text | WAF_DETECTED 등 |
| `dead` | bool | fail_count >= 3 이면 true (재시도 안함) |

**RPC 함수**: `saudi_failed_urls_bump(url, source, error, type, threshold)` — 원자적 upsert + 카운터 증가

### 2-2. saudi_api_quota — 토큰 버킷

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `domain` | text PK | sfda.gov.sa 등 |
| `tokens_current` | numeric | 현재 토큰 수 |
| `tokens_max` | numeric | 최대 토큰 |
| `refill_rate` | numeric | 초당 충전율 |
| `last_refill` | timestamptz | 마지막 충전 시점 |

초기 시드: SFDA 2.0 qps, 소매사이트 0.3 qps

### 2-3. saudi_circuit_state — 서킷 브레이커

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `domain` | text PK | |
| `state` | text | closed / open / half_open |
| `failures` | int | 연속 실패 횟수 |
| `opened_at` | timestamptz | open 전환 시점 |

임계값: 5회 실패 → open, 600초 후 half_open

### 2-4. saudi_selector_cache — 동적 셀렉터 캐시 (TTL 1시간)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `domain` | text | |
| `page_type` | text | article / product / list |
| `selector` | text | CSS 셀렉터 |
| `success_count` | int | 성공 횟수 |
| `expires_at` | timestamptz | 만료 시점 |

### 2-5. saudi_crawl_runs — 크롤링 잡 이력

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | bigserial PK | |
| `workflow` | text | sa_api_light 등 |
| `toggle_id` | text | |
| `github_run_id` | text | Actions run ID |
| `status` | text | running / succeeded / failed / partial |
| `rows_inserted` | int | |
| `rows_updated` | int | |
| `error_summary` | text | |

### 2-6. saudi_robots_log — robots.txt 감사

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | bigserial PK | |
| `domain` | text | |
| `allowed_paths` | text[] | |
| `disallowed_paths` | text[] | |
| `crawl_delay` | int | |

## 3. AI 발견 소스 테이블 (✅ 적용 완료)

> ai_search.py Phase A에서 발견한 소스 메타데이터. 12개국 통일 스키마(`ai_discovered_sources`)로 적용됨.

### ai_discovered_sources

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | uuid PK | gen_random_uuid() |
| `country` | text | SA, IN, EG 등 (ISO 3166-1) |
| `url` | text | 발견된 URL |
| `domain` | text | |
| `category` | text | pharma_retailer / distributor / regulator / ... |
| `relevance_score` | numeric(3,2) | LLM 판별 점수 (0.0~1.0) |
| `has_price_data` | bool | |
| `has_product_listing` | bool | |
| `scraper_xpaths` | jsonb | 합성된 XPath (auto_scraper 결과) |
| `last_crawled_at` | timestamptz | |
| `crawl_count` | int | 재방문 횟수 |
| `discovered_at` | timestamptz | |

## 4. 데이터 흐름

```
[크롤러 10개]                    [AI 자율 서칭]
     │                               │
     ▼                               ▼
normalize()                    _normalize_records()
     │                               │
     ▼                               ▼
inn_normalizer.normalize()     inn_normalizer.normalize()
     │                               │
     ▼                               ▼
outlier_detector.check()       (이상치 검사)
     │                               │
     ▼                               ▼
SourceReputation.update()      confidence 가변 (INN 매칭 반영)
     │                               │
     ▼                               ▼
┌─── products ─────────┐     ┌── (선택) ai_discovered_products ─┐
│  upsert by           │     │  레거시/감사용 — 현재 ai_search는  │
│  product_id          │     │  `products`에 source_name=       │
│  (고정 크롤러·AI 공통) │     │  ai_discovered 로 upsert        │
└──────────────────────┘     └───────────────────────────────────┘
```

## 4-1. 12개국 통일 스키마 (✅ 적용 완료)

> 상세 설계: `team-kit/sql/01_products.sql` 참고.

사우디 전용 테이블(`saudi_*`)과 별도로, 12개국 데이터를 통합 관리하는 테이블이 적용되어 있다.

| 테이블 | 용도 |
|--------|------|
| `products` | 12개국 크롤링 데이터 통합 (country 컬럼으로 구분) |
| `sources` | 소스 마스터 (SA:sfda_api, IN:cdsco_web 등) |
| `crawl_runs` | 크롤링 실행 이력 (국가별) |
| `circuit_state` | 서킷 브레이커 (국가별) |
| `api_quota` | 토큰 버킷 (국가별) |
| `failed_urls` | 실패 URL 큐 (국가별) |
| `ai_discovered_sources` | AI 발견 소스 (국가별) |
| `audit_log` | 감사 로그 (국가별) |

**RPC**: `failed_urls_bump(url, country, source, error, type, threshold)` — 원자적 upsert

사우디 소스 10개가 시드 데이터로 등록 완료. 다른 국가 팀원은 자기 소스만 INSERT하면 됨.

## 5. 새 Supabase 프로젝트에 스키마 넣기 (필수)

런타임 크롤러는 **`products`**(통합) + **`assets/sql/state_tables.sql`** 의 `saudi_*` 테이블을 사용한다.  
한 번에 적용하려면 SQL Editor에서 **`assets/sql/supabase_bootstrap.sql`** 전체를 실행한다 (`team-kit/sql/01_products.sql` + state + 마이그레이션 + `04_auxiliary_tables.sql`).

- `upsert(..., on_conflict=product_id)` 는 **`product_id`에 UNIQUE 인덱스**가 있어야 동작한다 (`01_products.sql` 끝에 포함됨).
- 적재 확인: `python scripts/verify_supabase.py`

레거시 **`saudi_products`**(`assets/sql/schema.sql`)만 있으면 현재 크롤러는 **`products`에 쓰지 않으므로** Table Editor에서 비어 보일 수 있다.

## 6. 적용 상태 (참고)

```
→ 1. supabase_bootstrap.sql 실행 (또는 위 파일들을 순서대로)
→ 2. .env / GitHub Secrets: SUPABASE_URL, SUPABASE_SERVICE_KEY
→ 3. 워크플로 실행 → products 등에 적재
```
