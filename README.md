# Saudi Pharma Crawler

사우디아라비아 의약품 시장조사 자동화 시스템.
유나이티드제약 수출 대상 의약품의 시장 데이터를 10개 사우디 공신력 사이트에서 수집하고, AI 분석 + 시장조사 보고서(DOCX)를 자동 생성한다.

> **핵심 목적**: 등록 여부 판정이 아닌, **시장 진입을 위한 경쟁 환경 정보 수집**. 동일 성분 제품의 가격·제조사·유통 현황을 긁어모아 수출 전략 수립을 지원한다.

## 시스템 설계 철학

```
                        GitHub Actions 러너 (IP 로테이션)
                        ┌─────────────────────────────┐
                        │                             │
[고정 10개 소스 크롤링] ─┤→ [Supabase DB 적재] → [이력 비교/패턴 학습]
                        │         ↓                    │
[AI 자율 서칭] ─────────┤  재크롤링 시 이전 데이터 참조  │
                        │  (가격 변동, 신규 등록 감지)   │
                        └─────────────────────────────┘
                                      ↓
                        로컬 (크롤링 없음, DB 조회 + 분석)
                        ┌─────────────────────────────┐
                        │ frontend/server.py → 대시보드 │
                        │ Claude 분석 + Perplexity 인사이트│
                        │ report_generator.py → DOCX   │
                        └─────────────────────────────┘
```

- **모든 크롤링은 GitHub Actions에서만 실행**: 로컬 IP 노출 방지 + 러너마다 IP 변경으로 대상 사이트 부하 분산 + CF/WAF 차단 회피
- **SciSpace 논문 적용**: Anti-bot 탐지, EMA 소스 신뢰도, 구조화 감사로그, 경량 메트릭 — 크롤링할수록 시스템이 학습
- **AUTOSCRAPER 논문 적용**: LLM 기반 XPath 자동 생성 — 새 사이트 발견 시 수동 크롤러 코딩 없이 데이터 추출
- **DB = 장기 기억**: 크롤링 결과를 Supabase에 누적 → 이상치 검사, 가격 추세, 소스 평판 모두 이 데이터 기반
- **AI 이중 분석**: Claude로 진출 적합성 판정 + Perplexity Sonar로 시장 인사이트 보강

## 핵심 기능

### 1. 타겟 의약품 검색
- Excel에 등록된 8개 의약품을 **하나씩 선택**하여 10개 사이트에서 타겟 검색
- 종류/품목/성분/함량/제형 5개 정보 기반 검색
- 검색 결과를 **공공조달 / 민간 / 논문** 카테고리로 분류
- 새 의약품 추가 가능 (UI에서 5개 필드 입력)
- **소스별 최적화 완료**: SFDA PHP API, Nahdi Algolia API 직접 호출, Whites Akinon __next_f 파싱

### 2. 진출 적합성 AI 분석 + 보고서 생성
- 4단계 파이프라인: **크롤링 → Claude 분석 → 논문 검색 → 보고서 생성**
- Claude Sonnet으로 verdict(적합/조건부/부적합), confidence, rationale, key_factors 자동 판정
- DB에 쌓인 동일 성분 가격 데이터 기반 **가격 비교 + AI 예상 가격 범위** 산출
- Perplexity Sonar로 시장 인사이트 (시장 규모, 경쟁 현황, 규제 동향) 보강
- 1공정 시장조사 보고서 양식(DOCX) 자동 생성

### 3. 10개 사우디 소스 크롤러
| # | 소스 | Tier | 카테고리 | 상태 | 검색 방식 |
|---|------|------|----------|------|-----------|
| 1 | SFDA 의약품 API | 1 | 공공조달 | ✅ 동작 | PHP JSON API (GetDrugsSearch3.php) |
| 2 | SFDA 의약품 HTML | 1 | 공공조달 | ✅ 동작 (API fallback) | PHP JSON API |
| 3 | SFDA 제약사 | 1 | 공공조달 | ✅ 동작 | PHP JSON API (GetDrugCompaniesSearch.php) |
| 4 | NUPCO 텐더 | 2 | 공공조달 | ✅ 동작 | WordPress HTML 파싱 |
| 5 | Etimad API | 2 | 공공조달 | ⚠️ API 키 필요 | REST API (Ocp-Apim-Subscription-Key) |
| 6 | Nahdi 약국 | 3 | 민간 | ✅ 동작 | **Algolia Search API 직접 호출** |
| 7 | Al Dawaa 약국 | 3 | 민간 | ✅ Actions 전용 | Magento HTML 파싱 (로컬 CF 차단) |
| 8 | Whites 약국 | 3 | 민간 | ✅ 동작 | **Next.js __next_f 스트리밍 파싱** (Akinon) |
| 9 | Tamer Group | 4 | 민간 | ✅ Actions 전용 | 단일 페이지 텍스트 매칭 (로컬 403) |
| 10 | Noon Saudi | 5 | 민간 | ✅ Actions 전용 | Next.js __NEXT_DATA__ (로컬 CF 차단) |

> **실행 환경별 차이**: 사우디 사이트들은 자국민 외 API를 발행하지 않으므로, 모든 소스를 웹 크롤링으로 수집한다.
> Al Dawaa / Tamer Group / Noon Saudi 3개 소스는 **Cloudflare/Akamai IP 차단** 정책이 있어 로컬(한국 IP)에서는 403이 반환되지만, **GitHub Actions 러너(미국 Azure IP)에서는 정상 통과**한다.
> 로컬 실행(`targeted_search.py`, `api_server.py`)에서는 이 3개 소스가 자동으로 skip 처리되며, 나머지 7개 소스로 검색·보고서 생성이 동작한다.
> 전체 10개 소스 벌크 크롤링은 반드시 **GitHub Actions 워크플로**(`python -m main`)로 실행해야 한다.

**소스별 크롤링 기술 상세**:
- **Nahdi**: Next.js SPA + Algolia InstantSearch. SSR 페이지는 빈 쿼리의 프로모션만 포함하므로, JS 번들에서 추출한 공개 Algolia 키로 Search API를 직접 호출하여 정확한 검색 결과 취득 (App ID: `H9X4IH7M99`, Index: `prod_en_products`)
- **Whites**: Next.js + Akinon Cloud 커머스. 검색 파라미터가 `search_text` (일반적인 `q`가 아님). 제품 데이터는 `self.__next_f.push()` 스트리밍 chunks 내 Akinon 제품 JSON으로 포함됨 (pk, name, sku, price, retail_price, absolute_url, attributes.Brand)
- **Al Dawaa**: Magento 기반 e-commerce. Cloudflare 뒤에 있어 로컬에서 차단, Actions 러너에서 통과. JSON-LD + HTML `product-item` 패턴 파싱
- **Tamer Group**: 도매유통 단일 페이지. img alt / h3·h4 태그에서 브랜드·파트너 추출. WAF 차단으로 로컬 불가, Actions에서 통과
- **Noon Saudi**: Next.js SPA `__NEXT_DATA__` JSON에서 제품 데이터 추출. Akamai Bot Manager 차단으로 로컬 불가, Actions에서 통과

### 4. 데이터 파이프라인 (SciSpace 논문 적용 완료)
```
크롤링 → 정규화 → WHO INN 매칭 → 이상치 검사 → 소스 신뢰도(EMA) 보정
                                                         ↓
                                          감사로그 + 메트릭 → DB 적재
                                                         ↓
                                          재크롤링 시 이전 데이터와 비교
```

적용된 논문 기술:
| 기술 | 구현 | 목적 |
|------|------|------|
| Anti-bot 탐지 | `antibot.py` | Cloudflare/CAPTCHA/WAF 자동 감지 + 대응 |
| UA 회전 | `antibot.py` | 6개 실제 브라우저 UA 풀에서 랜덤 선택 |
| EMA 소스 신뢰도 | `supabase_state.py` | 소스별 과거 성공률 기반 confidence 보정 |
| 구조화 감사로그 | `supabase_state.py` | 모든 크롤링 이벤트 JSON 기록 |
| 경량 메트릭 | `supabase_state.py` | 카운터 + p95 히스토그램 (Prometheus 없이) |
| K-통계량 이상치 | `outlier_detector.py` | DB 기존 가격 기반 IQR 이상치 검사 |
| WHO INN 매칭 | `inn_normalizer.py` | preon 기반 성분명 정규화 |
| SPA 역공학 | `nahdi_web.py`, `whites_web.py` | Algolia API 직접 호출, Next.js 스트리밍 파싱 |

### 5. AI 자율 서칭 (✅ 구현 완료)

고정 10개 소스 외에 **Claude AI가 자율적으로 추가 정보원을 탐색**하고, 발견된 사이트에서 데이터를 자동 추출한다.

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

적용된 논문:
| 논문 | 구현 파일 | 핵심 기술 |
|------|-----------|-----------|
| HTML Snippet 추출/압축 알고리즘 | `html_preprocessor.py` | 노이즈 제거 → 텍스트 밀도 선택 → 구조화 토큰 변환 |
| AUTOSCRAPER (Progressive Understanding) | `auto_scraper.py` | LLM XPath 생성 + top-down/step-back + 다중 페이지 합성 |

Phase A 소스 발견 경로:
```
[Perplexity Sonar API]  ← 1회 호출로 URL + 메타데이터 반환 (우선)
       │ 실패/미설정 시
       ▼
[DuckDuckGo HTML 스크래핑 → Claude Haiku 판별]  ← 기존 다단계 fallback
```

LLM 호출 지점:
| 호출 | 모델 | 용도 |
|------|------|------|
| 소스 발견 (우선) | Perplexity Sonar | 1회 호출로 URL + 카테고리 + 관련도 |
| 검색 쿼리 생성 (fallback) | Haiku | 약품 정보 → 검색 쿼리 5개 |
| 사이트 판별 (fallback) | Haiku | 정제 스니펫 → 의약품 관련 여부 + 신뢰도 |
| XPath 생성 | Haiku | 정제 HTML → XPath 표현식 |
| 추출값 검증 | Haiku | 추출 데이터 → pass/fail |

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
├── main.py                    # ✅ GitHub Actions 엔트리포인트 (토글 기반 벌크 크롤링)
├── ai_search.py               # ✅ AI 자율 서칭 엔트리포인트 (Phase A→B→C)
├── drug_registry.py           # ✅ 타겟 의약품 관리 (Excel→JSON, 추가/조회)
├── targeted_search.py         # ✅ 타겟 검색 오케스트레이터 (1약품→10소스)
├── report_generator.py        # ✅ DOCX 보고서 생성 (표지/요약/경쟁분석/가격/결론)
├── dashboard_data.json        # ✅ SFDA 파이프라인 분석 데이터 (이상치/신뢰도/차트)
│
├── frontend/                  # ✅ 대시보드 프론트엔드
│   ├── server.py              #   FastAPI 서버 (SSE + 4단계 파이프라인 + 20개 API)
│   ├── dashboard_sites.py     #   크롤링 사이트 10개 메타 정의
│   └── static/
│       └── index.html         #   통합 대시보드 UI (분석 + Chart.js 차트 + 이상치)
│
├── crawlers/                  # ✅ 10개 소스 크롤러 (전체 구현 완료)
│   ├── sfda_api.py            #   SFDA 의약품 (PHP JSON API)
│   ├── sfda_drugs_list_html.py #   SFDA 의약품 HTML fallback
│   ├── sfda_companies.py      #   SFDA 제약사/대리점 마스터
│   ├── nupco_tenders.py       #   NUPCO 공공조달 텐더
│   ├── etimad_api.py          #   Etimad 공공조달 API
│   ├── nahdi_web.py           #   ✅ Nahdi 소매약국 (Algolia API 직접 호출)
│   ├── al_dawaa_web.py        #   ✅ Al Dawaa 소매약국 (Actions 전용, 로컬 CF 차단)
│   ├── whites_web.py          #   ✅ Whites 소매약국 (Next.js __next_f 파싱)
│   ├── tamer_group.py         #   ✅ Tamer Group 도매유통 (Actions 전용, 로컬 403)
│   └── noon_saudi.py          #   ✅ Noon Saudi 대형상거래 (Actions 전용, 로컬 CF 차단)
│
├── assets/
│   ├── sources.yaml           # ✅ 소스 설정 (토글/워크플로/레이트리밋)
│   ├── drug_registry.json     # ✅ 타겟 의약품 목록 (8개, Excel에서 로드)
│   ├── snippets/              # ✅ 공용 모듈
│   │   ├── llm_client.py      #   ✅ Claude API 래퍼 (Haiku/Sonnet, 재시도, 토큰추적)
│   │   ├── perplexity_client.py #  ✅ Perplexity Sonar API (소스 발견 + 시장 인사이트)
│   │   ├── html_preprocessor.py #  ✅ 논문1: HTML→LLM 프롬프트 전처리 6단계
│   │   ├── source_discoverer.py #  ✅ AI 소스 발견 (쿼리생성→URL수집→LLM판별)
│   │   ├── auto_scraper.py    #   ✅ 논문2: AUTOSCRAPER (XPath생성+검증+합성)
│   │   ├── sfda_web.py        #   SFDA 공개 API 클라이언트
│   │   ├── normalizer.py      #   함량/제형/가격 정규화 (약어 매핑 포함)
│   │   ├── inn_normalizer.py  #   WHO INN 매칭 (preon 기반)
│   │   ├── outlier_detector.py #  K-통계량 이상치 검사
│   │   ├── trafilatura_fallback.py # HTML 텍스트 추출 fallback
│   │   ├── identity_resolver.py #  소매→SFDA 매칭 (유사도 스코어링)
│   │   ├── antibot.py         #   Anti-bot 탐지 + UA 회전
│   │   ├── supabase_state.py  #   상태 관리 (서킷브레이커, 감사로그, 메트릭, EMA 신뢰도)
│   │   └── backoff_retry.py   #   지수 백오프 + Anti-bot 대응
│   ├── sql/                   # DB 스키마
│   │   ├── schema.sql         #   사우디 메인 테이블 (saudi_products)
│   │   ├── state_tables.sql   #   사우디 상태 테이블 6개 (서킷브레이커, 토큰버킷 등)
│   │   └── migrations/        #   스키마 마이그레이션
│   └── workflows/             # GitHub Actions (sa_api_light, sa_retail_mid, sa_procurement)
│
├── tests/                     # ✅ 테스트 (총 299 tests)
├── reports/                   # 생성된 보고서 출력 폴더
├── references/                # 참고 문서 (architecture, sources, legal 등)
├── team-kit/                  # 12개국 확장 템플릿 (크롤러 예제, SQL)
├── .env                       # 🔒 환경변수 (API 키, .gitignore 등록됨)
├── .gitignore                 # .env, __pycache__, reports/*.docx 등 제외
└── DATABASE.md                # ✅ 데이터베이스 구조 문서
```

## 환경변수 (API 키)

| 환경변수 | 사용처 | 필수 여부 |
|---------|--------|----------|
| `SUPABASE_URL` | DB 연결 — 크롤링 결과 적재, 이력 비교 | DB 적재 시 필수 |
| `SUPABASE_SERVICE_KEY` | DB 인증 — 상태 관리, 서킷브레이커, 감사로그 | DB 적재 시 필수 |
| `CLAUDE_API_KEY` | AI 자율 서칭 — LLM 소스 판별, XPath 생성 | AI 서칭 시 필수 |
| `PERPLEXITY_API_KEY` | AI 소스 발견 + 시장 인사이트 분석 (Perplexity Sonar) | 선택 (없으면 DuckDuckGo fallback) |
| `PERPLEXITY_MODEL` | Perplexity 모델 지정 | 선택 (기본: sonar) |
| `SFDA_CLIENT_ID` | SFDA Developer Portal OAuth2 | 선택 (공개 API로 대체 가능) |
| `SFDA_CLIENT_SECRET` | SFDA Developer Portal OAuth2 | 선택 (사우디 국적자만 발급) |
| `ETIMAD_API_KEY` | Etimad 공공조달 API | 선택 (없으면 건너뜀) |

환경변수는 프로젝트 루트의 `.env` 파일에 설정 (`.gitignore` 등록됨).

**Supabase 키 없이도 타겟 검색 + 보고서 생성 동작** (로컬 JSON fallback)

## 실행 전략 — 왜 GitHub Actions인가

사우디 사이트들은 자국민 외 API를 발행하지 않아 **전부 웹 크롤링**으로 수집한다.
GitHub Actions 러너를 선택한 이유:

1. **IP 보호**: 로컬 IP로 반복 크롤링하면 차단당함. Actions 러너는 실행마다 IP가 바뀜
2. **대상 사이트 보호**: 토큰버킷(`TokenBucket`)으로 요청 속도를 제한하되, 러너가 매번 달라지므로 한 IP에서 과도한 요청이 나가지 않음
3. **CF/WAF 통과**: Al Dawaa, Tamer, Noon 등 Cloudflare/Akamai 차단 사이트도 Actions 러너 IP에서는 통과

```
┌──────────────────────────────────────────────────────────────┐
│                     실행 경로 4가지                            │
├──────────────┬───────────────────────────────────────────────┤
│ 벌크 크롤링   │ GitHub Actions → main.py                      │
│ (10소스 전체)  │  · sources.yaml 토글 기반                     │
│               │  · CircuitBreaker + TokenBucket + 감사로그     │
│               │  · Supabase DB 적재                           │
│               │  · 스케줄: sa_api_light / sa_retail_mid /      │
│               │    sa_procurement 워크플로                     │
├──────────────┼───────────────────────────────────────────────┤
│ 타겟 검색     │ GitHub Actions → targeted_search.py           │
│ (1약품→10소스) │  · workflow_dispatch로 drug_id 파라미터 전달   │
│               │  · 결과를 Supabase에 저장 → UI에서 조회        │
│               │  · 보고서 생성은 Actions 완료 후 로컬에서 실행  │
├──────────────┼───────────────────────────────────────────────┤
│ AI 자율 서칭  │ GitHub Actions → ai_search.py                 │
│ (✅ 구현 완료) │  · LLM이 새 소스 URL 발견 → HTML 전처리       │
│               │  · XPath 자동 생성 → 데이터 추출 → DB 적재     │
│               │  · CLAUDE_API_KEY 필수                        │
├──────────────┼───────────────────────────────────────────────┤
│ 로컬 대시보드  │ 로컬 → frontend/server.py                     │
│ (크롤링 없음)  │  · DB 조회 + Claude 분석 + Perplexity 인사이트 │
│               │  · 가격 비교/예상치 + DOCX 보고서 생성          │
│               │  · SSE 실시간 로그 + Chart.js 분석 대시보드      │
└──────────────┴───────────────────────────────────────────────┘
```

> **원칙: 사이트를 때리는 모든 HTTP 요청은 GitHub Actions에서만 실행한다.**
> 로컬에서는 DB 조회, 보고서 생성, UI 서빙만 한다.

## 실행

```bash
# 벌크 크롤링 (GitHub Actions 워크플로)
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... TOGGLE_ID=toggle_1 WORKFLOW=sa_api_light python -m main

# 타겟 검색 (GitHub Actions workflow_dispatch)
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python targeted_search.py rosumeg-combigel

# AI 자율 서칭 (GitHub Actions)
CLAUDE_API_KEY=sk-ant-... python ai_search.py                    # 8개 약품 전체
CLAUDE_API_KEY=sk-ant-... DRUG_ID=hydrine python ai_search.py    # 특정 약품만
CLAUDE_API_KEY=sk-ant-... DRY_RUN=true python ai_search.py       # DB 저장 없이 테스트

# 로컬: 대시보드 + 분석 + 보고서 (크롤링 없음, DB 조회 + AI 분석)
python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8000
# 브라우저에서 http://localhost:8000 접속
```

## 테스트

```bash
# 전체 테스트
python -m pytest tests/ -v

# 개별 테스트
python -m pytest tests/test_crawlers_import.py       # 크롤러 임포트 (55)
python -m pytest tests/test_scispace_stage4.py       # 파이프라인 통합 (21)
python -m pytest tests/test_llm_client.py            # Claude API 래퍼 (15)
python -m pytest tests/test_html_preprocessor.py     # HTML 전처리 (31)
python -m pytest tests/test_source_discoverer.py     # AI 소스 발견 (14)
python -m pytest tests/test_auto_scraper.py          # AUTOSCRAPER (26)
python -m pytest tests/test_ai_search.py             # AI 자율 서칭 통합 (9)
```

## 대시보드 기능

`frontend/server.py`로 구동하는 통합 대시보드:

| 기능 | 설명 |
|------|------|
| **진출 적합 분석** | 8개 품목 중 선택 → 4단계 파이프라인 (크롤링→Claude 분석→논문검색→보고서) |
| **가격 비교/예상치** | DB 동일 성분 가격 + 유사 제형 경쟁제품 + Claude AI 예상 가격 범위 |
| **AI 발견 소스** | AI 자율서칭에서 발견한 URL 목록 (도메인, relevance, 가격 유무) |
| **시장 인사이트** | Perplexity Sonar로 시장 분석 (시장 규모, 경쟁 현황, 규제 동향) |
| **이상치 검증** | SFDA 50개 레코드 기반 정상/이상치 도넛 + 신뢰도 분포 + 상세 테이블 |
| **Data Analytics** | Price Distribution, Confidence, Outlier Breakdown, Reputation EMA 차트 |
| **사이트 상태** | 10개 크롤링 사이트 실시간 상태 (SSE) |
| **실시간 로그** | 파이프라인 진행 상황 SSE 스트리밍 |
| **보고서 다운로드** | DOCX 시장조사 보고서 자동 생성 + 다운로드 |

### API 엔드포인트 (20개)

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 대시보드 UI |
| GET | `/api/stream` | SSE 실시간 이벤트 스트림 |
| POST | `/api/run` | 전체 크롤 실행 |
| GET | `/api/status` | 실행 상태 |
| GET | `/api/sites` | 크롤링 사이트 상태 |
| GET | `/api/products` | 품목 + SFDA 레코드 병합 반환 |
| POST | `/api/analyze` | 전체 분석 실행 |
| GET | `/api/analyze/status` | 분석 상태 |
| GET | `/api/analyze/result` | 분석 결과 |
| POST | `/api/pipeline/{key}` | 단일 품목 4단계 파이프라인 |
| GET | `/api/pipeline/{key}/status` | 파이프라인 진행 상태 |
| GET | `/api/pipeline/{key}/result` | 파이프라인 결과 (분석+논문+PDF+AI소스) |
| GET | `/api/ai-sources/{key}` | AI 발견 소스 URL 목록 |
| POST | `/api/perplexity/analyze` | Perplexity 시장 인사이트 분석 |
| GET | `/api/perplexity/result` | Perplexity 분석 결과 |
| POST | `/api/report` | 전체 보고서 생성 |
| GET | `/api/report/status` | 보고서 생성 상태 |
| GET | `/api/report/download` | 보고서 파일 다운로드 |
| GET | `/api/macro` | 거시지표 카드 데이터 |
| GET | `/api/dashboard-data` | Chart.js용 원본 분석 데이터 |

## 알려진 이슈 / 남은 작업

| # | 항목 | 상태 | 설명 |
|---|------|------|------|
| 1 | Supabase 스키마 적용 | ✅ 완료 | 사우디 전용 + 12개국 통일 스키마 모두 적용됨 |
| 2 | Perplexity API 통합 | ✅ 완료 | 소스 발견 + 시장 인사이트 분석 |
| 3 | 대시보드 통합 | ✅ 완료 | 분석+차트+이상치+AI소스+Perplexity 통합 대시보드 |
| 4 | DB 우선 조회 | ✅ 완료 | Supabase 우선 → 없으면 레지스트리 fallback |
| 5 | AI 소스 UI 표시 | ✅ 완료 | AI 발견 소스 카드 (도메인, relevance, 가격 유무) |
| 6 | AI 서칭 Actions 워크플로 | ❌ 미작성 | `sa_ai_search.yml` 작성 필요 |
| 7 | 발견 소스 재크롤링 | ❌ 미구현 | 유효 소스를 다음 실행에서 자동 재방문하는 루프 |

## 관련 문서

- **[DATABASE.md](DATABASE.md)** — 데이터베이스 구조 상세 (테이블, 인덱스, RPC 함수)
- **[references/](references/)** — 아키텍처, 소스, 법적 참고, SciSpace 논문 등
