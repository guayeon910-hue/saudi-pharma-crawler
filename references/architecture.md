# Architecture — 사우디 크롤러 3레이어 파이프라인

## 설계 철학

이 크롤러는 "완벽한 데이터"가 아니라 **"수집이 멈추지 않는 것"**을 우선순위로 둔다. 최종 산출물이 보고서 3종(시장분석·FOB 제안서·파트너 리스트)이고, 그 뒷단에 Claude Haiku가 해석 레이어로 들어가기 때문에 중간 데이터에 일부 노이즈·결측이 있어도 보고서 품질에 치명적이지 않다. 대신 파이프라인 전체가 침묵하는 순간 캡스톤 과제 자체가 무너진다. 따라서 모든 설계 결정은 "어떻게 더 정확하게"보다 "어떻게 덜 멈출까"를 먼저 본다.

## 전체 파이프라인

```
┌─────────────────────────────────────────────────────────────┐
│ L0. GitHub Actions Orchestrator                             │
│     - cron 스케줄 (시간대 분산)                                │
│     - 유동 IP 회전 (Azure 러너 대역)                           │
│     - Secrets 주입 (SFDA/Claude/Slack 등)                    │
│     - 실패 시 Slack 알림                                      │
└─────────────────────────────────────────────────────────────┘
                            ↓
        ┌───────────────────┴───────────────────┐
        ↓                                       ↓
┌─────────────────────┐              ┌─────────────────────┐
│ L1. 고정 URL 수집     │              │ L1'. AI 판단 수집    │
│                     │              │                     │
│ - SFDA API          │              │ - 뉴스/시장조사 페이지 │
│ - NUPCO 텐더         │              │ - Claude Haiku이     │
│ - Etimad API        │              │   "가격·경쟁사·공급처  │
│ - Nahdi/Al-Dawaa    │              │    만 추출" 프롬프트   │
│   공개 상품 페이지    │              │                     │
│                     │              │ (본 프로젝트의        │
│ 스키마 알려짐 →       │              │  crawler.py 뉴스     │
│ 하드코딩 추출         │              │  엔진 재활용 지점)    │
└─────────────────────┘              └─────────────────────┘
        ↓                                       ↓
        └───────────────────┬───────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ L2. Normalizer & Identity Resolver                          │
│     - 성분명 토큰화 (아랍어/영어 Unicode NFKC)                  │
│     - 함량 단위 정규화 (500mg → "500 mg")                     │
│     - 제형 어휘 매핑 (tablet/capsule/inj/syrup)              │
│     - 통화 SAR 고정                                          │
│     - regulatory_id 매칭 (SFDA 검색으로 보강)                 │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ L3. Supabase 적재                                            │
│     - saudi_products 테이블 (공통 6컬럼 + 확장)               │
│     - IQR 이상치 검증 → outlier_flagged                      │
│     - confidence 점수 차등 부여                               │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ L4. (본 스킬 범위 밖) 보고서 3종 생성                          │
│     시장분석 / FOB 제안서 / 파트너 리스트                      │
└─────────────────────────────────────────────────────────────┘
```

## 왜 이 구조인가 — 결정 근거

### 결정 1. GitHub Actions를 유동 IP 회전기로 쓴다

**대안**: Render 상시 가동 + ScraperAPI Residential Proxy 월 3만원  
**선택**: GitHub Actions (무료) + Azure 러너 IP 자연 회전

**근거**:
- Actions 러너는 Azure 데이터센터 대역을 쓰며 IP가 고정되지 않는다. 잡마다 다른 egress IP가 나온다. 이는 원래 "고정 IP allowlist를 못 쓴다"는 단점이지만, 크롤러 입장에서는 **차단 회피의 1차 방어층**으로 뒤집어 쓸 수 있다.
- Free 플랜 월 2000분 + public repo 무제한으로 비용 0.
- 단점: 상태 공유 불가 (러너 휘발성). → 상태는 Supabase로 외부화해서 해결.

**주의**: API 키 기준 Rate Limit이 걸리는 소스(SFDA, Etimad)는 IP가 달라도 소용없다. 이쪽은 `sop.md`의 cron 시간대 분산으로 해결한다.

### 결정 2. 고정 URL 레이어와 AI 판단 레이어를 분리한다

**대안 1**: 전부 하드코딩 셀렉터 → 사이트 구조 변경 시 전부 깨짐  
**대안 2**: 전부 AI에 맡기기 → 토큰 비용 폭발 + Haiku 정확도 한계  
**선택**: 스키마 알려진 소스는 하드코딩, 비정형 시장조사는 AI

**근거**:
- SFDA처럼 필드가 명확히 정의된 API는 Haiku에게 추출시킬 이유가 없다. 결정론적 파싱이 더 빠르고 정확하고 공짜다.
- 반면 뉴스 기사, 시장 동향 페이지, 정부 보도자료처럼 **가격·경쟁사 정보가 본문에 파편적으로 흩어져 있는 경우** Haiku가 훨씬 유리하다. 이때 `crawler.py`의 텍스트 밀도 분석 + CSR 폴백 엔진이 본문을 뽑고, Haiku가 의미를 추출한다.
- 두 레이어는 같은 8필드 스키마로 수렴한다. 차이는 `confidence` 점수에서 반영:
  - 고정 URL (공식 API): 0.90 ~ 0.95
  - 고정 URL (소매 스크래핑): 0.70 ~ 0.85
  - AI 판단 레이어: 0.50 ~ 0.70
  - 정적 fallback (CSV 등): 0.30

### 결정 3. 상태는 Supabase에 외부화한다

**이유**: Actions 러너가 휘발성이라 `crawler.py`가 자랑하는 `.failed_queue.json` 파일락, 동적 셀렉터 1시간 캐시, 토큰 버킷 인스턴스 카운터가 전부 증발한다.

**해결**: 아래 테이블들을 Supabase에 둔다.

| 테이블 | 역할 |
|---|---|
| `saudi_products` | 최종 수집 데이터 (공통 6컬럼 + 사우디 확장) |
| `crawl_jobs` | Actions 잡 단위 실행 이력 (toggle_id, started_at, status, error) |
| `failed_urls` | 재시도 큐 (url, fail_count, last_error, next_retry_at) |
| `dead_urls` | 3회 이상 실패 영구 폐기 (포이즌 필) |
| `source_selectors` | 동적 셀렉터 캐시 (source_domain, selector_id, selector_class, ttl_until) |
| `rate_limit_state` | API 키별 최근 호출 시각 (key_hash, last_call_at, call_count_1h) |

`crawler.py` 원본 엔진의 방어 로직은 그대로 쓰되, 파일 I/O 부분만 Supabase 호출로 바꾼다.

## 기술 스택 권장안

### 언어·프레임워크

| 레이어 | 선택 | 근거 |
|---|---|---|
| Orchestrator (Actions) | YAML + Python entry | Actions 네이티브 지원 |
| Fetcher (API) | `httpx[http2]` | HTTP/2, async, 커넥션 풀 |
| Fetcher (HTML 정적) | `httpx` + `selectolax` | lxml보다 빠름, 사우디 소매 페이지는 대부분 정적 |
| Fetcher (HTML 동적) | Playwright + stealth | 최후 수단. 러너 내부 Chromium 설치 |
| Parser | `selectolax` + 정규식 + JSON path | 본문/테이블 추출 |
| AI 추출 레이어 | Claude Haiku via Anthropic SDK | Sonnet 금지 |
| DB 클라이언트 | `supabase-py` | Service Role Key로 INSERT |
| 재시도 | `tenacity` | 지수 백오프 + 지터 |
| 로깅 | `loguru` | stdout으로 Actions 로그에 찍힘 |

### 의존성 최소화 원칙

Actions 러너 콜드 스타트 시간을 줄이기 위해 `pip install` 의존성을 가능한 적게. Scrapy 풀 스택은 과하다 — `httpx + selectolax + tenacity + supabase` 4개로 대부분 커버된다. Playwright는 필요한 워크플로우에만 별도 설치.

## 모듈 분리

```
saudi_crawler/
├── core/
│   ├── fetcher.py          # httpx 래퍼 + 재시도 + 유저에이전트
│   ├── parser.py           # selectolax 래퍼 + 폴백 로직
│   ├── normalizer.py       # 성분/함량/제형/통화 정규화
│   ├── resolver.py         # SFDA 매칭으로 regulatory_id 보강
│   └── state.py            # Supabase 상태 테이블 래퍼
├── sources/
│   ├── sfda.py             # SFDA Developer API
│   ├── nupco.py            # NUPCO 공개 텐더
│   ├── etimad.py           # Etimad API (구독 필요)
│   ├── nahdi.py            # Nahdi Online 상품 페이지
│   ├── aldawaa.py          # Al-Dawaa Pharmacies
│   └── whites.py           # Whites
├── orchestrator/
│   ├── toggles.py          # 8개 토글 → source 매핑
│   └── runner.py           # Actions entrypoint
└── ai_layer/
    └── extractor.py        # Claude Haiku 기반 비정형 추출
```

각 `sources/*.py` 파일은 단일 책임: `async def fetch(query: dict) -> list[RawRecord]`. 반환된 `RawRecord`는 `normalizer` → `resolver` → `state.insert()` 순으로 흐른다.

## 예상 실패 모드와 대응

| 실패 모드 | 빈도 | 영향 | 대응 |
|---|---|---|---|
| SFDA API 429 | 중간 | 단일 소스 중단 | `tenacity` 백오프 + Retry-After 존중 |
| SFDA 토큰 24h 만료 | 확정 | 인증 실패 | 매 잡 시작 시 토큰 재발급 |
| Nahdi DOM 변경 | 분기 1~2회 | 파싱 결측 | 동적 셀렉터 캐시 TTL 만료 후 재탐색 |
| Actions 러너 타임아웃 | 드물게 | 잡 실패 | 잡당 15분 목표, Playwright 잡 분리 |
| Supabase 일시 장애 | 드물게 | 적재 실패 | 로컬 JSONL 버퍼 후 다음 잡에서 재적재 |
| Claude API 쿼터 초과 | 테스트 시 | AI 레이어 중단 | 제품 1~2개로 제한, 루프 금지 |

자세한 방어 로직은 `crawler_patterns.md` 참조.
