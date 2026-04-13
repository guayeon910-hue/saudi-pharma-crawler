# Schema — 공통 8필드 + 사우디 확장 + 정규화 규칙

## 철학

스키마는 **두 층**으로 나뉜다.

1. **공통 6컬럼** — 권역(동남아/중동/남미/기타)이 달라도 동일. 프로젝트 헌법상 변경 불가.
2. **사우디 확장 컬럼** — 사우디에서만 의미 있는 필드. 자유롭게 추가 가능.

그리고 실무적으로 "8필드 최소 공통 스키마"가 수집 레이어에서 먼저 채워지고, 그 뒤 공통 6컬럼으로 매핑된다. 즉 수집→매핑 2단계다.

## 공통 6컬럼 (헌법 — 변경 금지)

| 컬럼 | 타입 | 의미 | 예시 |
|---|---|---|---|
| `id` | uuid | 행 PK | `a3f...` |
| `product_id` | text | 품목 식별자 (프로젝트 내부 규칙) | `KUP-001-SA` |
| `market_segment` | text | 시장 분할 (`retail`/`tender`/`wholesale`/`combo`) | `retail` |
| `fob_estimated_usd` | numeric | FOB 역산 결과 (USD) | `3.42` |
| `confidence` | numeric | 데이터 신뢰도 0.0~1.0 | `0.85` |
| `crawled_at` | timestamptz | 수집 시각 (UTC) | `2026-04-08T03:12:00Z` |

### 주의

- `fob_estimated_usd`는 **이 스킬 범위 밖인 FOB 역산 모듈이 채운다.** 크롤러는 `null`로 삽입한다.
- `confidence`는 크롤러가 소스별로 차등 부여한다 (아래 "confidence 기준표" 참조).
- `crawled_at`은 DB default `now()`로 두지 말고 크롤러가 명시적으로 UTC ISO8601 문자열로 주입. Actions 러너 타임존이 바뀌어도 일관성 유지.

## 8필드 최소 공통 스키마 (수집 레이어)

SFDA Drugs List가 제공하는 핵심 필드와 정합되도록 설계. 다른 소스에서 수집할 때도 이 8필드는 반드시 채우거나 `null`로 남긴다.

| 필드 | 타입 | 필수 | 의미 |
|---|---|---|---|
| `regulatory_id` | text | 선택 | 등록번호 또는 바코드(GTIN). 1차 식별자 |
| `trade_name` | text | **필수** | 브랜드/상표명 |
| `scientific_name` | text | 선택 | 주성분명. 복합제는 원문 문자열 유지 |
| `strength` | text | 선택 | 함량 + 단위 (예: `"500 mg"`) |
| `dosage_form` | text | 선택 | 제형 (예: `"tablet"`, `"injection"`) |
| `price_sar` | numeric | 선택 | 가격 (사우디 리얄). 없으면 null |
| `manufacturer_or_marketing_company` | text | 선택 | 제조사 또는 마케팅사 |
| `agent_or_supplier` | text | 선택 | 에이전트(1/2/3차) 또는 공급처 |

`trade_name`만 필수인 이유: 이것 없이는 식별 자체가 불가능. 나머지는 소스 따라 결측 허용.

## 사우디 확장 컬럼

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `atc_code` | text | WHO ATC 분류 코드 (유사품 탐색용) |
| `package_size` | text | 패키지 규격 (예: `"28 tablets"`, `"100 mL"`) |
| `public_price_vat_included` | boolean | 가격이 VAT 포함인지 |
| `vat_rate` | numeric | 사우디 VAT 15% (2026 기준) |
| `promo_raw` | text | 프로모션 원문 (예: `"Save 30%"`) |
| `source_domain` | text | 수집 출처 도메인 |
| `source_url` | text | 수집 원본 URL |
| `source_type` | text | `api_official`/`html_retail`/`html_tender`/`pdf_tender`/`ai_extracted` |
| `outlier_flagged` | boolean | IQR 이상치 검증 결과 |
| `raw_fields` | jsonb | 정규화 전 원본 필드 덤프 (디버깅용) |

### raw_fields의 역할

`raw_fields`에 파싱 직후 원본을 JSONB로 통째로 저장한다. 나중에 정규화 로직이 바뀌어도 재처리 가능. 디스크 비용보다 **"데이터 재수집하러 다시 사이트 때리는 비용"**이 훨씬 크다. Supabase Pro 플랜에서 JSONB 10만 행은 우습다.

## DDL 예시

```sql
create table saudi_products (
    -- 공통 6컬럼
    id uuid primary key default gen_random_uuid(),
    product_id text not null,
    market_segment text not null check (market_segment in ('retail','tender','wholesale','combo')),
    fob_estimated_usd numeric,
    confidence numeric not null check (confidence between 0 and 1),
    crawled_at timestamptz not null,

    -- 8필드 최소 공통
    regulatory_id text,
    trade_name text not null,
    scientific_name text,
    strength text,
    dosage_form text,
    price_sar numeric,
    manufacturer_or_marketing_company text,
    agent_or_supplier text,

    -- 사우디 확장
    atc_code text,
    package_size text,
    public_price_vat_included boolean,
    vat_rate numeric default 0.15,
    promo_raw text,
    source_domain text not null,
    source_url text,
    source_type text not null,
    outlier_flagged boolean default false,
    raw_fields jsonb
);

-- 인덱스
create index on saudi_products (trade_name);
create index on saudi_products (regulatory_id) where regulatory_id is not null;
create index on saudi_products (crawled_at desc);
create index on saudi_products (source_type, crawled_at desc);
create index on saudi_products using gin (raw_fields);
```

## confidence 기준표

```
0.95  SFDA API 직접 반환 (regulatory_id 매칭까지 완료)
0.90  SFDA API 직접 반환 (regulatory_id 있음)
0.85  NUPCO 공식 텐더 결과 문서
0.80  소매 HTML + SFDA 역매칭 성공 + 함량·제형 일치
0.75  소매 HTML + SFDA 역매칭 부분 성공
0.70  소매 HTML (매칭 실패, trade_name만)
0.60  도매 유통 브랜드 매핑
0.50  AI 레이어 (Haiku 추출, 출처 명확)
0.40  AI 레이어 (Haiku 추출, 출처 흐림)
0.30  정적 fallback (CSV, 캐시, 정부 오픈데이터 덤프)
```

규칙:
- 어떤 소스도 `1.0`을 주지 않는다. 완벽한 데이터는 없다.
- `confidence < 0.50`은 보고서에서 "근거 약함" 플래그로 표시된다.
- FOB 역산 모듈은 `confidence >= 0.70`인 데이터만 입력으로 받는 것을 권장.

## 정규화 규칙 (Normalizer)

수집된 원본 필드를 8필드 형식으로 맞추는 단계. `core/normalizer.py`에 구현.

### 1. trade_name

- 공백 trim, 연속 공백 단일화
- 아랍어/영어 혼용 시 Unicode NFKC 정규화
- 대소문자는 **원문 유지** (브랜드명은 대소문자가 의미)
- 특수문자 `®`, `™`은 제거

### 2. scientific_name

- 복합제 구분자: `+`, `/`, `,`, `;` 모두 허용, 원문 유지
- 토큰화는 별도 `scientific_tokens` JSONB 컬럼에 저장 (선택)
- 아랍어 성분명이 나오면 영어 번역본을 Haiku로 보강 (단, 비용 고려)

### 3. strength

- 형식: `"<숫자> <단위>"` (숫자와 단위 사이 공백 1칸)
- 단위 허용: `mg`, `g`, `mcg`, `µg`, `IU`, `mL`, `%`, `% w/v`, `mg/mL`
- `500mg` → `"500 mg"` 변환
- `0.5g` → `"0.5 g"` 유지 (단위 변환 금지, 원문 존중)
- 복합제 예: `"5 mg + 10 mg"`

### 4. dosage_form

영어 소문자 표준 어휘로 매핑:

| 원문 | 표준 |
|---|---|
| Tablet, TAB, tab, Tablets | `tablet` |
| Capsule, CAP, caps | `capsule` |
| Injection, Inj, IV, IM | `injection` |
| Syrup, Sirop | `syrup` |
| Cream, Ointment, Gel | 각각 `cream`, `ointment`, `gel` |
| Eye drops, Ophthalmic | `eye_drops` |

표준 어휘에 없는 값은 `dosage_form_raw`에 원본 저장 + `dosage_form`은 `null`.

### 5. price_sar

- 숫자만. 통화 기호 `SR`, `SAR`, `ر.س` 제거
- VAT 포함/미포함 명시 시 `public_price_vat_included` 컬럼에 반영
- VAT 미표시 시 사우디 표준 15% 가정 후 플래그 `vat_assumed=true`

### 6. manufacturer_or_marketing_company

- 회사명 표준화는 **하지 않는다.** 원문 유지. (동일 회사 별칭 매핑은 파트너 매칭 모듈 몫)

### 7. agent_or_supplier

- SFDA: `"First agent: X / Second: Y / Third: Z"` 형식으로 pipe 구분
- NUPCO 낙찰: 낙찰사명만
- 소매: 대부분 null

### 8. regulatory_id 매칭 (Identity Resolver)

소매·뉴스·AI 레이어에서 수집된 데이터는 대부분 `regulatory_id`가 비어 있다. Resolver가 SFDA API를 호출해 채운다.

**매칭 점수** = 0.3 × trade_name 유사도 + 0.3 × scientific_name 유사도 + 0.2 × strength 근접도 + 0.1 × dosage_form 일치 + 0.1 × ATC 일치

- 0.85 이상: 자동 확정
- 0.70~0.85: 자동 확정하되 `resolver_confidence` 확장 컬럼에 점수 기록
- 0.70 미만: `regulatory_id` null 유지
- SFDA에서 후보 0건: `no_sfda_match=true` 플래그

**캐싱**: 같은 (trade_name, strength, dosage_form) 튜플은 Supabase `resolver_cache` 테이블에 24시간 캐싱. 동일 쿼리 반복으로 SFDA API 쿼터 태우지 않는다.

## 이상치 검증 (IQR)

적재 후 배치로 돌린다.

```
같은 scientific_name + strength + dosage_form 그룹 내에서
Q1, Q3 계산 → IQR = Q3 - Q1
lower = Q1 - 1.5 * IQR
upper = Q3 + 1.5 * IQR

price_sar < lower OR price_sar > upper  → outlier_flagged = true
```

플래그만 찍고 삭제하지 않는다. 프로모션·패키지 차이일 수 있다. 보고서 생성 모듈이 판단.

## 매칭 실패 시 처리

`trade_name`까지 비어 있으면 적재 거부 (Supabase NOT NULL 제약). 이외에는 null 허용하고 적재. 완전성보다 "적재가 멈추지 않는 것"이 우선.
