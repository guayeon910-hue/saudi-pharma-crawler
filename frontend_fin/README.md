# UPharma Export AI — Frontend

한국유나이티드제약 싱가포르 수출 타당성 분석 대시보드의 프론트엔드입니다.

---

## 필수 사전 조건

> **아래 3가지가 없으면 앱이 동작하지 않습니다.**

### 1. 백엔드 서버 실행
이 프로젝트는 빌드 도구 없이 순수 HTML/CSS/JS로 구성되어 있습니다.
단, **모든 기능은 백엔드 API에 의존**하므로 반드시 백엔드 서버가 먼저 실행되어야 합니다.

### 2. 프론트엔드 파일 정적 서빙 경로
백엔드 서버에서 아래 경로로 파일을 서빙해야 합니다.

| 실제 파일 경로 | 서빙 URL 경로 |
|---|---|
| `index.html` | `/` |
| `style.css` | `/static/style.css` |
| `app.js` | `/static/app.js` |
| `images/logo.png` | `/static/images/logo.png` |

> `index.html` 내부에서 `/static/style.css?v=2`, `/static/app.js?v=2` 로 하드코딩되어 있습니다.
> 경로가 다르면 **UI가 깨지고 앱이 실행되지 않습니다.**

### 3. API 키 준비
백엔드에서 다음 두 가지 API 키가 반드시 설정되어 있어야 합니다.

- **Claude API Key** (Anthropic) — 의약품 분석 AI
- **Perplexity API Key** — 논문/레퍼런스 검색

---

## 백엔드 필수 API 목록

> **아래 엔드포인트가 모두 구현되어 있어야 합니다.** 하나라도 없으면 해당 기능이 동작하지 않습니다.

| Method | Endpoint | 설명 |
|---|---|---|
| `GET` | `/api/exchange` | 환율 정보 (KRW/SGD, USD/KRW) |
| `GET` | `/api/keys/status` | Claude & Perplexity API 키 상태 확인 |
| `POST` | `/api/pipeline/{product_key}` | 사전 등록 품목 분석 시작 |
| `GET` | `/api/pipeline/{product_key}/status` | 파이프라인 진행 상태 조회 |
| `GET` | `/api/pipeline/{product_key}/result` | 분석 결과 조회 |
| `POST` | `/api/pipeline/custom` | 커스텀 의약품 분석 시작 |
| `GET` | `/api/report/download` | PDF 보고서 다운로드 |
| `GET` | `/api/news` | 싱가포르 제약 시장 뉴스 |

### API 응답 형식 (프론트엔드 기준)

```json
// GET /api/exchange
{ "sgd_krw": 1050.5, "usd_krw": 1380.0, "sgd_usd": 0.76 }

// GET /api/keys/status
[{ "name": "claude", "active": true, "label": "Claude API" }, ...]

// GET /api/pipeline/{key}/status
{ "status": "running", "step": 2 }

// GET /api/pipeline/{key}/result
{
  "result": {
    "verdict": "green",         // "red" | "yellow" | "green"
    "market_medical": "...",
    "regulatory": "...",
    "trade": "...",
    "pbs_price": "...",
    "pathway": "...",
    "risk": "..."
  },
  "refs": [{ "title": "...", "url": "..." }],
  "pdf": "파일명.pdf"
}
```

---

## 프로젝트 구조

```
frontend/
├── index.html      # 진입점 — 탭 구조 및 레이아웃
├── app.js          # 전체 비즈니스 로직
├── style.css       # 디자인 시스템 및 스타일
└── images/
    └── logo.png    # 한국유나이티드제약 로고
```

빌드 과정 없음. 파일 수정 후 새로고침하면 즉시 반영됩니다.

---

## 주요 기능

| 탭 | 기능 | 상태 |
|---|---|---|
| 메인 프리뷰 | 환율 카드, 공정 체크리스트, 시장 뉴스 | 구현됨 |
| 1공정 · 시장조사 | 의약품 분석 실행 및 결과 확인, PDF 다운로드 | 구현됨 |
| 2공정 · 수출전략 | 플레이스홀더 | 미구현 |
| 3공정 · 바이어발굴 | 플레이스홀더 | 미구현 |
| 보고서 | 완료된 분석 결과 목록 및 재다운로드 | 구현됨 |

### 사전 등록된 분석 품목

- Sereterol Activair (Fluticasone/Salmeterol)
- Omethyl (Omega-3)
- Hydrine Capsule (Hydroxyurea)
- Gadvoa Injection (Gadobutrol)
- Rosumeg Combigel / Atmeg Combigel (Rosuvastatin/Omega-3)
- Ciloduo (Cilostazol/Rosuvastatin)
- Gastiin CR (Mosapride)

---

## 데이터 저장

백엔드 DB 없이 **브라우저 localStorage** 를 사용합니다.

| 키 | 내용 | 제한 |
|---|---|---|
| `sg_upharma_todos_v1` | 공정 체크리스트 상태 | — |
| `sg_upharma_reports_v1` | 생성된 보고서 메타데이터 | 최대 30건 |

> 브라우저 데이터를 삭제하면 보고서 목록이 초기화됩니다. 실제 PDF 파일은 백엔드에 보관됩니다.

---

## 기술 스택

- **언어:** HTML5, CSS3, Vanilla JavaScript (ES6+)
- **빌드 도구:** 없음
- **외부 라이브러리:** 없음
- **폰트:** Pretendard, Noto Sans KR (시스템 폴백 적용)
- **브라우저 지원:** 최신 브라우저 (Chrome, Edge, Safari, Firefox)

---

## 문의

기능 추가 및 버그 리포트는 담당자에게 직접 연락하거나 이슈로 등록해 주세요.
