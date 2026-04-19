# Saudi Pharma Crawler — UPharma Export AI

사우디아라비아 의약품 수출 전략 수립을 위한 통합 AI 대시보드.  
7개 사우디 공신력 사이트 크롤링 → Claude AI 분석 → 공공/민간 시장 FOB 역산 → 보고서 자동 생성까지 3공정 파이프라인을 단일 웹 UI로 제공한다.

---

## 프로덕션 (Render)

**현재 이 앱은 [Render](https://render.com) 웹 서비스에 배포되어 운영 중이다.** GitHub 저장소와 연결된 경우 `master`에 푸시하면 Dockerfile 기준으로 빌드·재배포된다.

| 항목 | 내용 |
|------|------|
| 접속 URL | **[https://saudi-pharma-crawler.onrender.com/](https://saudi-pharma-crawler.onrender.com/)** (Render 대시보드에서 동일하게 확인 가능) |
| 헬스 체크 | `GET /healthz` → `{"status":"ok"}` |
| 환경 변수 | Render → 해당 서비스 → **Environment** — 아래 「환경변수」표와 동일한 키를 설정 (`CLAUDE_API_KEY` 등) |
| 주의 | 무료 플랜은 일정 시간 미사용 후 **콜드 스타트**로 첫 요청이 지연될 수 있다 |

로컬에서만 돌릴 때는 아래 「로컬 실행」을 따른다.

---

## 로컬 실행

```bash
cd C:\Users\user\Desktop\saudi-pharma-crawler
python -m uvicorn frontend.server:app --host 0.0.0.0 --port 8000
```

브라우저에서 `http://localhost:8000` 접속.

> `.env` 파일에 API 키가 설정되어 있어야 AI 분석 기능이 동작한다 (아래 환경변수 참조).

---

## 환경변수 (`.env`)

| 변수 | 필수 | 설명 |
|------|------|------|
| `CLAUDE_API_KEY` | **필수** | 1공정 AI 분석 + 2공정 제품 분류 |
| `SUPABASE_URL` | 권장 | DB 연결 — SAR 가격 데이터 조회 (없으면 로컬 fallback) |
| `SUPABASE_SERVICE_KEY` | 권장 | DB 인증 (서비스 롤 키) |
| `PERPLEXITY_API_KEY` | 선택 | 시장 인사이트 보강 (없으면 건너뜀) |
| `ETIMAD_API_KEY` | 선택 | Etimad 공공조달 API (없으면 건너뜀) |

---

## 3공정 파이프라인

### 1공정 · 시장조사

- 품목 선택(8개) 또는 직접 입력 → 7개 사우디 소스 크롤링
- Claude Haiku로 verdict(적합/조건부/부적합), confidence, 경쟁 환경 분석
- Supabase DB 동일 성분 가격 비교 + AI 예상 SAR 가격 범위 산출
- Perplexity Sonar 시장 인사이트 보강 (선택)
- 1공정 시장조사 보고서 DOCX 자동 생성

### 2공정 · 수출전략

**공공 시장**: 1공정 보고서의 참고 SAR 가격 분포 → FOB 벤치마크 역산 (`PUBLIC_SCENARIO_DEFAULTS`, 조달 입찰 통행 가정)  
**민간 시장**: 동일 역산 코어·다른 시나리오 기본값 (`frontend/fob_private.py`)

역산 로직:
```
Retail SAR
  → Retail/(1+약국마진)/(1+도매마진) = CIF
  → CIF × generic/biosimilar cap
  → 함량 비율 보정 (target/competitor strength)
  → 복합제 프리미엄
  → 운임·보험료 차감
  → 에이전트 커미션 차감
= FOB (공격적/평균/보수적 3 시나리오)
```

Supabase에 해당 성분 SAR 가격이 없으면 1공정 Claude 추정가를 단일 기준점으로 자동 fallback.

### 3공정 · 바이어 발굴

바이어 매칭 연동 준비 중.

---

## 타겟 의약품 (8개)

| 품목 | 성분 | 함량 | 제형 |
|------|------|------|------|
| Omethyl Cutielet | Omega-3-Acid Ethyl Esters 90 | 2g | Pouch |
| Rosumeg Combigel | Rosuvastatin + Omega-3-EE90 | 5/1000, 10/1000mg | Cap. |
| Atmeg Combigel | Atorvastatin + Omega-3-EE90 | 10/1000mg | Cap. |
| Ciloduo | Cilostazol + Rosuvastatin | 200/10, 200/20mg | Tab. |
| Gastiin CR | Mosapride Citrate | 15mg | Tab. |
| Sereterol Activair | Fluticasone + Salmeterol | 250/50, 500/50 | Inhaler |
| Gadvoa Inj. | Gadobutrol | 5mL, 7.5mL | PFS |
| Hydrine | Hydroxyurea | 500mg | Cap. |

---

## 크롤링 소스 (7개)

| # | 소스 | 구분 | 상태 | 방식 |
|---|------|------|------|------|
| 1 | SFDA 의약품 API | 공공 | ✅ | PHP JSON API |
| 2 | SFDA 의약품 HTML | 공공 | ✅ | PHP JSON API fallback |
| 3 | SFDA 제약사 | 공공 | ✅ | PHP JSON API |
| 4 | NUPCO 텐더 | 공공 | ✅ | WordPress HTML 파싱 |
| 5 | Etimad API | 공공 | ⚠️ API 키 필요 | REST API |
| 6 | Nahdi 약국 | 민간 | ✅ | Algolia Search API 직접 호출 |
| 7 | Whites 약국 | 민간 | ✅ | Next.js `__next_f` 스트리밍 파싱 |

> Al Dawaa / Tamer Group / Noon Saudi — Cloudflare WAF 차단으로 제외.

---

## 프로젝트 구조

```
saudi-pharma-crawler/
├── frontend/
│   ├── server.py              # FastAPI 서버 (파이프라인 + 뉴스 + 2공정 API)
│   ├── fob_private.py         # 2공정 민간 시장 FOB 역산 파이프라인
│   └── static/
│       ├── index.html         # 3탭 SPA (메인 / 1·2·3공정 / 보고서)
│       ├── app.js
│       └── style.css
│
├── crawlers/                  # 7개 소스 크롤러
├── assets/
│   ├── drug_registry.json     # 타겟 의약품 목록
│   └── snippets/              # 공용 모듈 (LLM, 정규화, anti-bot 등)
│
├── tests/                     # 단위 테스트 (fob_private, p2 API)
├── main.py                    # 벌크 크롤링 CLI
├── ai_search.py               # AI 자율 서칭
├── report_generator.py        # DOCX 보고서 생성
├── Dockerfile
└── .env                       # API 키 (gitignore)
```

---

## 주요 API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 대시보드 HTML |
| GET | `/healthz` | 헬스 체크 |
| GET | `/api/exchange` | SAR/KRW·USD 환율 |
| GET | `/api/keys/status` | API 키 설정 여부 |
| GET | `/api/news` | 사우디 시장 뉴스 |
| POST | `/api/pipeline/{product_key}` | 1공정 단일 품목 파이프라인 |
| POST | `/api/pipeline/custom` | 신약(직접 입력) 파이프라인 |
| GET | `/api/pipeline/{key}/status` | 파이프라인 진행 상태 SSE |
| GET | `/api/report/download` | DOCX 보고서 다운로드 |
| POST | `/api/p2/price-analyze` | 2공정 민간 시장 FOB 역산 |

---

## Render 배포 (신규·재설정 시)

이미 배포된 서비스를 바꾸지 않을 때는 건드릴 필요 없다. 새로 Render에 올리거나 설정을 맞출 때만 참고한다.

- **Root Directory**: 레포 루트 (`Dockerfile`과 `frontend/`가 한 번에 보이는 경로). 서브폴더만 루트로 지정하면 안 된다.
- **Start Command**: 비워 두고 **Dockerfile의 `CMD`**만 사용한다.
- **Health Check Path**: `/healthz`
- **Environment**: 로컬 `.env`와 같은 변수명으로 Render 대시보드에 등록한다 (Git에 시크릿 커밋 금지).

---

## 관련 문서

- **[DATABASE.md](DATABASE.md)** — Supabase 테이블 구조 및 RPC 함수
