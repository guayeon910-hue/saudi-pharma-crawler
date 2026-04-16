# Saudi Pharma Crawler

사우디아라비아 의약품 시장조사 자동화 시스템.
유나이티드제약 수출 대상 의약품의 시장 데이터를 7개 사우디 공신력 사이트에서 수집하고, AI 분석 + 시장조사 보고서(DOCX)를 자동 생성한다.

> **핵심 목적**: 등록 여부 판정이 아닌, **시장 진입을 위한 경쟁 환경 정보 수집**. 동일 성분 제품의 가격·제조사·유통 현황을 긁어모아 수출 전략 수립을 지원한다.

## 시스템 구조

```
              ┌──────────────────────────────────────────┐
              │          로컬 실행 (대시보드 서버)           │
              │                                          │
[7개 소스 크롤링] ─→ [Supabase DB 적재] → [이력 비교/패턴 학습]
              │             ↓                            │
[AI 자율 서칭] ──→  재크롤링 시 이전 데이터 참조             │
              │    (가격 변동, 신규 등록 감지)              │
              │             ↓                            │
              │  frontend/server.py → 대시보드             │
              │  Claude 분석 + Perplexity 인사이트          │
              │  report_generator.py → DOCX               │
              └──────────────────────────────────────────┘
```

- **Anti-bot·레이트리밋**: 탐지, 소스별 신뢰도 보정, 감사로그, 경량 메트릭
- **LLM 스크래퍼 합성**: `auto_scraper`로 XPath 생성 — 새 URL 발견 시 추출 자동화
- **DB = 장기 기억**: 크롤링 결과를 Supabase에 누적 → 이상치 검사, 가격 추세, 소스 평판 모두 이 데이터 기반
- **AI 이중 분석**: Claude로 진출 적합성 판정 + Perplexity Sonar로 시장 인사이트 보강

## 핵심 기능

### 1. 타겟 의약품 검색
- Excel에 등록된 8개 의약품을 **하나씩 선택**하여 7개 사이트에서 타겟 검색
- 종류/품목/성분/함량/제형 5개 정보 기반 검색
- 검색 결과를 **공공조달 / 민간** 카테고리로 분류
- **소스별 최적화 완료**: SFDA PHP API, Nahdi Algolia API 직접 호출, Whites Akinon __next_f 파싱

### 2. 진출 적합성 AI 분석 + 보고서 생성
- 4단계 파이프라인: **크롤링 → Claude 분석 → 참고문헌(Perplexity) → 보고서 생성**
- Claude Sonnet으로 verdict(적합/조건부/부적합), confidence, rationale, key_factors 자동 판정
- DB에 쌓인 동일 성분 가격 데이터 기반 **가격 비교 + AI 예상 가격 범위** 산출
- Perplexity Sonar로 시장 인사이트 (시장 규모, 경쟁 현황, 규제 동향) 보강
- 1공정 시장조사 보고서 양식(DOCX) 자동 생성

### 3. 7개 사우디 소스 크롤러
| # | 소스 | 카테고리 | 상태 | 검색 방식 |
|---|------|----------|------|-----------|
| 1 | SFDA 의약품 API | 공공조달 | ✅ 동작 | PHP JSON API (GetDrugsSearch3.php) |
| 2 | SFDA 의약품 HTML | 공공조달 | ✅ 동작 (API fallback) | PHP JSON API |
| 3 | SFDA 제약사 | 공공조달 | ✅ 동작 | PHP JSON API (GetDrugCompaniesSearch.php) |
| 4 | NUPCO 텐더 | 공공조달 | ✅ 동작 | WordPress HTML 파싱 |
| 5 | Etimad API | 공공조달 | ⚠️ API 키 필요 | REST API (Ocp-Apim-Subscription-Key) |
| 6 | Nahdi 약국 | 민간 | ✅ 동작 | **Algolia Search API 직접 호출** |
| 7 | Whites 약국 | 민간 | ✅ 동작 | **Next.js __next_f 스트리밍 파싱** (Akinon) |

> **참고**: Al Dawaa / Tamer Group / Noon Saudi 3개 소스는 Cloudflare/WAF IP 차단으로 접근 불가하여 제외됨.

**소스별 크롤링 기술 상세**:
- **Nahdi**: Next.js SPA + Algolia InstantSearch. JS 번들에서 추출한 공개 Algolia 키로 Search API를 직접 호출 (App ID: `H9X4IH7M99`, Index: `prod_en_products`)
- **Whites**: Next.js + Akinon Cloud 커머스. `search_text` 파라미터 사용. `self.__next_f.push()` 스트리밍 chunks 내 Akinon 제품 JSON 파싱

### 4. 데이터 파이프라인
```
크롤링 → 정규화 → WHO INN 매칭 → 이상치 검사 → 소스 신뢰도 보정
                                                         ↓
                                          감사로그 + 메트릭 → DB 적재
```

| 기술 | 구현 | 목적 |
|------|------|------|
| Anti-bot 탐지 | `antibot.py` | Cloudflare/CAPTCHA/WAF 자동 감지 + 대응 |
| UA 회전 | `antibot.py` | 6개 실제 브라우저 UA 풀에서 랜덤 선택 |
| EMA 소스 신뢰도 | `supabase_state.py` | 소스별 과거 성공률 기반 confidence 보정 |
| 구조화 감사로그 | `supabase_state.py` | 모든 크롤링 이벤트 JSON 기록 |
| K-통계량 이상치 | `outlier_detector.py` | DB 기존 가격 기반 IQR 이상치 검사 |
| WHO INN 매칭 | `inn_normalizer.py` | preon 기반 성분명 정규화 |
| SPA 역공학 | `nahdi_web.py`, `whites_web.py` | Algolia API 직접 호출, Next.js 스트리밍 파싱 |

### 5. AI 자율 서칭

고정 7개 소스 외에 **Claude AI가 자율적으로 추가 정보원을 탐색**하고, 발견된 사이트에서 데이터를 자동 추출한다.

```
Phase A: 소스 발견                Phase B: 데이터 추출             Phase C: 통합
┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│ 약품 키워드       │         │ 유효 사이트 HTML   │         │ normalize        │
│   → LLM 쿼리 생성 │         │   → LLM XPath 생성│         │ → INN 매칭       │
│   → 검색엔진 URL  │  ───►   │   → top-down 탐색 │  ───►   │ → 이상치 검사    │
│   → HTML 전처리   │         │   → step-back 검증│         │ → DB 적재        │
│   → LLM 사이트 판별│         │   → 스크래퍼 합성  │         │                  │
└──────────────────┘         └──────────────────┘         └──────────────────┘
```

발견된 AI 소스는 대시보드 **데이터 분석** 탭에서 확인 가능.

## 타겟 의약품 (8개)

| # | 종류 | 품목 | 성분 | 함량 | 제형 |
|---|------|------|------|------|------|
| 1 | 개량신약 | Omethyl Cutielet | Omega-3-Acid Ethyl Esters 90 | 2g | Pouch |
| 2 | 개량신약 | Rosumeg Combigel | Rosuvastatin + Omega-3-EE90 | 5/1000, 10/1000 | Cap. |
| 3 | 개량신약 | Atmeg Combigel | Atorvastatin + Omega-3-EE90 | 10/1000 | Cap. |
| 4 | 개량신약 | Ciloduo | Cilostazol + Rosuvastatin | 200/10, 200/20mg | Tab. |
| 5 | 개량신약 | Gastiin CR | Mosapride Citrate | 15mg | Tab. |
| 6 | 일반제 | Sereterol Activair | Fluticasone + Salmeterol | 250/50, 500/50 | Inhaler |
| 7 | 일반제 | Gadvoa Inj. | Gadobutrol | 5mL, 7.5mL | PFS |
| 8 | 항암제 | Hydrine | Hydroxyurea | 500mg | Cap. |

## 프로젝트 구조

```
saudi-pharma-crawler/
├── main.py                    # 벌크 크롤링 엔트리포인트 (토글 기반)
├── ai_search.py               # AI 자율 서칭 엔트리포인트 (Phase A→B→C)
├── drug_registry.py           # 타겟 의약품 관리 (Excel→JSON)
├── targeted_search.py         # 타겟 검색 오케스트레이터 (1약품→7소스)
├── report_generator.py        # DOCX 보고서 생성
│
├── frontend/                  # 대시보드 프론트엔드
│   ├── server.py              #   FastAPI 서버 (SSE + 4단계 파이프라인)
│   ├── dashboard_sites.py     #   크롤링 사이트 7개 메타 정의
│   └── static/
│       └── index.html         #   3탭 대시보드 UI (분석 / 이상치 / 데이터 분석)
│
├── crawlers/                  # 7개 소스 크롤러
│   ├── sfda_api.py            #   SFDA 의약품 (PHP JSON API)
│   ├── sfda_drugs_list_html.py #   SFDA 의약품 HTML fallback
│   ├── sfda_companies.py      #   SFDA 제약사/대리점 마스터
│   ├── nupco_tenders.py       #   NUPCO 공공조달 텐더
│   ├── etimad_api.py          #   Etimad 공공조달 API
│   ├── nahdi_web.py           #   Nahdi 소매약국 (Algolia API 직접 호출)
│   └── whites_web.py          #   Whites 소매약국 (Next.js __next_f 파싱)
│
├── assets/
│   ├── sources.yaml           # 소스 설정 (토글/레이트리밋)
│   ├── drug_registry.json     # 타겟 의약품 목록 (8개)
│   ├── snippets/              # 공용 모듈
│   │   ├── llm_client.py      #   Claude API 래퍼
│   │   ├── perplexity_client.py #  Perplexity Sonar API
│   │   ├── html_preprocessor.py #  HTML→LLM 프롬프트 전처리
│   │   ├── source_discoverer.py #  AI 소스 발견
│   │   ├── auto_scraper.py    #   LLM XPath 생성·검증
│   │   ├── sfda_web.py        #   SFDA 공개 API 클라이언트
│   │   ├── normalizer.py      #   함량/제형/가격 정규화
│   │   ├── inn_normalizer.py  #   WHO INN 매칭
│   │   ├── outlier_detector.py #  K-통계량 이상치 검사
│   │   ├── antibot.py         #   Anti-bot 탐지 + UA 회전
│   │   ├── supabase_state.py  #   상태 관리 (서킷브레이커, 감사로그, 메트릭)
│   │   └── backoff_retry.py   #   지수 백오프 + Anti-bot 대응
│   └── sql/                   # DB 스키마
│
├── tests/                     # 테스트
├── reports/                   # 생성된 보고서 출력 폴더
├── .env                       # 환경변수 (API 키, .gitignore 등록됨)
└── DATABASE.md                # 데이터베이스 구조 문서
```

## 환경변수

| 환경변수 | 사용처 | 필수 여부 |
|---------|--------|----------|
| `SUPABASE_URL` | DB 연결 — 크롤링 결과 적재, 이력 비교 | DB 적재 시 필수 |
| `SUPABASE_SERVICE_KEY` | DB 인증 — 상태 관리, 서킷브레이커, 감사로그 | DB 적재 시 필수 |
| `CLAUDE_API_KEY` | AI 분석 + 자율 서칭 | AI 기능 사용 시 필수 |
| `PERPLEXITY_API_KEY` | AI 소스 발견 + 시장 인사이트 분석 | 선택 (없으면 DuckDuckGo fallback) |
| `ETIMAD_API_KEY` | Etimad 공공조달 API | 선택 (없으면 건너뜀) |

환경변수는 프로젝트 루트의 `.env` 파일에 설정.

**Supabase 키 없이도 타겟 검색 + 보고서 생성 동작** (로컬 JSON fallback)

## 실행

```bash
# 대시보드 서버 실행 (크롤링 + 분석 + 보고서 모두 여기서)
python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8000
# 브라우저에서 http://localhost:8000 접속

# 벌크 크롤링 (CLI)
python -m main

# AI 자율 서칭
CLAUDE_API_KEY=sk-ant-... python ai_search.py
```

### AI 추출을 `products`에 넣으려면

이미 `ai_search.py`는 성공 시 **`products` 테이블에 upsert**한다 (`source_name` = `ai_discovered`, `product_id` = `ai_discovered:…` 해시). 필요한 것만 정리하면 아래와 같다.

| 필요 항목 | 설명 |
|-----------|------|
| **Supabase에 `products` 스키마** | 프로젝트에서 `assets/sql/supabase_bootstrap.sql`(또는 `team-kit/sql/01_products.sql`)을 SQL 에디터로 적용해 두었다고 가정한다. |
| **`SUPABASE_URL` + `SUPABASE_SERVICE_KEY`** | `.env`에 설정. **서비스 롤** 키여야 `products`에 쓸 수 있다(anon 키만으로는 RLS/권한에 막힐 수 있음). DB 비밀번호는 앱에서 쓰지 않는다. |
| **`CLAUDE_API_KEY`** | Phase A/B LLM 호출에 필수. |
| **`PERPLEXITY_API_KEY`** (선택) | 있으면 소스 발견에 우선 사용, 없으면 DuckDuckGo 경로. |
| **실행** | 위 환경에서 `python ai_search.py`. 첫 실행 전에 DB에 `ai_discovered_sources` 등 보조 테이블도 같이 만들어 두면 재크롤·XPath 저장이 동작한다. |

예전에만 `ai_discovered_products`에 쌓인 행을 `products`로 옮기려면, Supabase에서 한 번 실행하는 **INSERT…SELECT 마이그레이션**이 별도로 필요하다(데이터 유무에 따라).

## 대시보드 (3탭)

`frontend/server.py`로 구동하는 통합 대시보드:

| 탭 | 내용 |
|----|------|
| **🔬 분석** | 품목 선택 → **실행**: 고정 7소스 크롤+분석 / **AI 자율 서칭**: 검색·URL발견·추출·`products` 적재 (`ai_search`와 동일) / Perplexity·보고서·실시간 로그 |
| **⚠️ 이상치 탐지** | 정상/이상치 도넛 차트 + 신뢰도 분포 + 상세 테이블 (100행) |
| **📊 데이터 분석** | 실시간 DB 데이터 기반 — 소스별 수집 현황, 가격 분포, 신뢰도 분포, 이상치 유형 분류, AI 자동 탐색 URL, 전체 레코드 테이블 |

### 주요 API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 대시보드 UI |
| GET | `/api/stream` | SSE 실시간 이벤트 스트림 |
| GET | `/api/sites` | 크롤링 사이트 상태 (7개) |
| GET | `/api/products` | 품목 + DB 레코드 병합 반환 |
| GET | `/api/macro` | KPI 카드 데이터 |
| POST | `/api/pipeline/{key}` | 단일 품목 4단계 파이프라인 |
| GET | `/api/pipeline/{key}/status` | 파이프라인 진행 상태 |
| GET | `/api/pipeline/{key}/result` | 결과 (분석+참고문헌+보고서+AI소스) |
| GET | `/api/ai-sources` | AI 발견 소스 전체 목록 |
| POST | `/api/perplexity/analyze` | Perplexity 시장 인사이트 |
| GET | `/api/db-stats` | DB 소스별 통계 |
| POST | `/api/report` | 보고서 생성 |
| GET | `/api/report/download` | 보고서 다운로드 |
| POST | `/api/ai-search/run` | AI 자율 서칭 (`all_drugs` 또는 `product_key`) — `ai_search.py`와 동일 파이프라인 |
| GET | `/api/ai-search/status` | AI 자율 서칭 실행 여부 |

## 관련 문서

- **[DATABASE.md](DATABASE.md)** — 데이터베이스 구조 상세 (테이블, 인덱스, RPC 함수)
