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
├── frontend/                  # 대시보드 프론트엔드 (Render·Docker에서 단일 소스)
│   ├── server.py              #   FastAPI (파이프라인, 뉴스, 2공정 스텁 API 등)
│   ├── dashboard_sites.py     #   크롤링 사이트 7개 메타 정의
│   └── static/                #   정적 UI — 서버가 `/static`으로 마운트
│       ├── index.html         #   5탭 SPA: 메인 / 1·2·3공정 / 보고서 (`#main` … `#rep` 해시)
│       ├── app.js
│       ├── style.css
│       └── images/logo.png
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

## 대시보드 (UPharma Export AI · 5탭)

`uvicorn frontend.server:app`으로 구동하며, UI는 [frontend/static/](frontend/static/)만 서빙된다 (`/` → `index.html`, `/static/*` → JS/CSS/이미지). URL 해시로 탭 직링크가 가능하다 (`/#main`, `/#p1`, `/#p2`, `/#p3`, `/#rep`).

| 탭 | 내용 |
|----|------|
| **메인 프리뷰** | 사우디 관세·환율 요약, 공정 To-Do, 시장 뉴스 |
| **1공정 · 시장조사** | 품목 선택 → Claude 진출 적합 분석, 논문, PDF 보고서 |
| **2공정 · 수출전략** | 1공정 보고서 선택·PDF 업로드, 공공/민간 시장, 가격 분석(스텁 API) |
| **3공정 · 바이어 발굴** | 이후 바이어 매칭 연동 예정 안내 및 보고서·2공정으로 이동 |
| **보고서** | 1공정 완료 시 자동 등록된 항목·PDF 다운로드(localStorage) |

### 주요 API 엔드포인트

자세한 라우트는 [frontend/server.py](frontend/server.py)를 본다. UI에서 쓰는 예시는 다음과 같다.

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 대시보드 HTML |
| GET | `/api/exchange` | SAR/KRW 등 환율 |
| GET | `/api/keys/status` | Claude·Perplexity 키 설정 여부 |
| GET | `/api/news` | 사우디 시장 뉴스 |
| POST | `/api/pipeline/{product_key}` | 1공정 단일 품목 파이프라인 |
| POST | `/api/pipeline/custom` | 신약(직접 입력) 파이프라인 |
| GET | `/api/report/download` | 최근 생성 PDF |
| POST | `/api/p2/price-analyze` | 2공정 가격 분석(현재 스텭 검증·스텁 응답) |
| GET | `/api/stream` 등 | 레거시/기타 크롤·대시보드 데이터용 엔드포인트 |

## 관련 문서

- **[DATABASE.md](DATABASE.md)** — 데이터베이스 구조 상세 (테이블, 인덱스, RPC 함수)
