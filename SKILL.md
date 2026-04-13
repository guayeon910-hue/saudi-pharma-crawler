---
name: saudi-pharma-crawler
description: 사우디아라비아 의약품 가격·경쟁사·공급처 자동 크롤링 시스템을 설계·구현·운영하기 위한 통합 스킬. 한국유나이티드제약 × 무역AX 캡스톤 프로젝트의 중동 권역 백엔드 파이프라인에 특화되어 있다. 사용자가 사우디 크롤러, SFDA API, NUPCO/Etimad 조달 데이터, Nahdi/Al-Dawaa 소매 가격 수집, 8필드 공통 스키마, GitHub Actions 기반 분산 크롤링, FOB 역산용 데이터 수집, 의약품 시장조사 자동화를 언급하면 반드시 이 스킬을 사용할 것. "사우디 크롤러 만들어줘", "SFDA 붙여줘", "의약품 가격 긁어줘" 같은 간접 표현에도 트리거된다.
---

# Saudi Pharma Crawler Skill

사우디아라비아 대상 의약품 가격·경쟁사·공급처 자동 수집 파이프라인을 만들 때 참조하는 스킬이다. 프로젝트 본 과제는 한국유나이티드제약 AI 기반 해외 시장 분석 시스템의 "중동 권역 크롤링 → DB 적재 → 보고서 3종(시장분석/FOB 제안서/파트너 리스트) 자동 생성" 파이프라인이며, 이 스킬은 그중 **수집~정제 단계**만 다룬다. FOB 역산 로직과 AHP 기반 파트너 매칭은 본 스킬 범위가 아니다.

## 핵심 아키텍처 한 줄 요약

```
GitHub Actions (유동 IP·사용량 조절)
  → 고정 URL 크롤링 (SFDA API 등 공식) + AI 판단 크롤링 (뉴스·시장조사)
  → 정제 (8필드 공통 스키마)
  → Supabase 적재 → 보고서 3종 생성
```

GitHub Actions 러너가 Azure 대역에서 IP를 회전시키는 특성을 **버그가 아닌 기능으로** 활용한다. ScraperAPI 같은 유료 Residential Proxy를 대체하는 1차 방어층이다.

## 언제 어떤 reference를 읽을지

작업 성격에 따라 필요한 파일만 골라 읽는다. 전부 다 읽지 말 것.

| 사용자가 요청하는 것 | 먼저 읽을 파일 |
|---|---|
| "크롤러 처음 설계부터" / "아키텍처 짜줘" | `references/architecture.md` → `references/sources.md` → `references/schema.md` |
| "SFDA 연동해줘" / "공공조달 붙여줘" / "Nahdi 가격" | `references/sources.md` → `references/schema.md` |
| "DB 테이블 만들어줘" / "컬럼 정의" / "정규화 로직" | `references/schema.md` |
| "차단됐어" / "429 뜸" / "본문 파싱 실패" / "재시도 로직" | `references/crawler_patterns.md` |
| "GitHub Actions YAML 짜줘" / "cron 언제 돌려" / "실패 알림" | `references/sop.md` |
| "robots.txt 괜찮아?" / "저작권" / "PDPL" | `references/legal.md` |
| "전반적 운영 리스크 점검" | `references/sop.md` → `references/legal.md` |

## 바로 쓸 수 있는 시드 파일 (assets/)

설계 문서만으로 코딩을 시작하지 말고, 먼저 assets에 있는 시드 파일을 복사해 뼈대를 만든다. 전부 실행 가능한 상태로 검증되어 있다.

| 용도 | 파일 | 설명 |
|---|---|---|
| 소스 설정 | `assets/sources.yaml` | 10개 소스 + 8개 토글 매핑. 새 소스 추가는 이 파일만 수정 |
| DB 스키마 | `assets/sql/schema.sql` | `saudi_products` 메인 테이블 DDL. Supabase SQL 에디터에서 바로 실행 |
| 상태 테이블 | `assets/sql/state_tables.sql` | 토큰버킷·서킷브레이커·실패큐·셀렉터캐시·실행이력 6개 테이블 |
| Actions YAML | `assets/workflows/sa_api_light.yml` | Tier 1 API용 워크플로우. cron·Secrets·Slack 알림 포함 |
| Actions YAML | `assets/workflows/sa_retail_mid.yml` | 소매 3곳 matrix 순차 실행 (IP 회전 극대화) |
| SFDA 클라이언트 | `assets/snippets/sfda_oauth.py` | OAuth Bearer 자동 갱신 + API 응답 → 8필드 매핑 함수 |
| 재시도 로직 | `assets/snippets/backoff_retry.py` | `@with_backoff` 데코레이터. 429 Retry-After 존중 |
| 정규화 | `assets/snippets/normalizer.py` | 함량/제형/가격/성분 표준화. 자가 테스트 포함 |
| 매칭 | `assets/snippets/identity_resolver.py` | 5가지 유사도 스코어링 → auto_confirm/candidate/no_match 판정 |
| 상태 영속화 | `assets/snippets/supabase_state.py` | `TokenBucket`, `CircuitBreaker`, `FailedQueue`, `CrawlRun` |
| Trafilatura 폴백 | `assets/snippets/trafilatura_fallback.py` | Triple Fallback 3차 + SPA JSON 파싱(2.5차). 시뮬레이션 검증 완료 |
| INN 정규화 | `assets/snippets/inn_normalizer.py` | preon 기반 WHO INN 매칭 + 브랜드 테이블 + 아랍어 음역 보정. 시뮬레이션 검증 완료 |
| 의존성 | `assets/requirements.txt` | 버전 핀 고정. Actions 러너와 로컬 일치 보장 |

**스니펫 사용 원칙**:
- 그대로 복붙하지 말고 **프로젝트 네임스페이스에 맞춰 import 경로만 조정**한다
- `normalizer.py`와 `identity_resolver.py`는 `if __name__ == "__main__"` 블록에 자가 테스트가 있다. 수정 후 반드시 다시 돌려서 통과 확인
- `supabase_state.py`는 `assets/sql/state_tables.sql`을 먼저 실행해야 동작한다
- `sfda_oauth.py`는 `backoff_retry.py`와 조합해서 쓰는 것을 전제로 작성됨 (401만 자체 처리, 나머지는 데코레이터에 위임)

## 절대 규칙 (프로젝트 헌법)

코드 생성 전 반드시 이 규칙들을 모두 만족하는지 확인한다. 하나라도 위배하면 설계를 다시 한다.

1. **공통 6컬럼 불변**: `id`, `product_id`, `market_segment`, `fob_estimated_usd`, `confidence`, `crawled_at` 의 이름·타입·의미를 변경하지 않는다. 사우디 전용 추가 컬럼은 자유이지만 이 6개는 다른 권역 크롤러와 공유되는 공통 스키마다.
2. **API 우선 원칙**: 공식 API(SFDA Developer Portal 등)가 존재하면 HTML 스크래핑보다 무조건 우선한다. HTML은 가격·프로모션처럼 API가 없는 영역에만 쓴다.
3. **API 키 서버 저장**: Claude/SFDA/ScraperAPI 등 어떤 키도 로컬 `.env`에 개인이 보관하지 않는다. GitHub Secrets로만 관리한다.
4. **LLM은 저가 모델만**: Claude Haiku, GPT-4o-mini만 허용. Sonnet/Opus/GPT-4 Turbo는 금지. 테스트 시 제품 1~2개만 돌리고 "20개국 × 10제품 루프" 같은 실수 금지.
5. **인증 우회·보안 회피 금지**: 사우디 Anti-Cyber Crime Law 저촉 가능성이 있는 행위(로그인 우회, CAPTCHA 무단 돌파, Rate Limit 강제 우회)는 하지 않는다. 2captcha는 공식 서비스 경로로만 사용한다.
6. **토글 1개만 실행**: UI 8개 토글은 동시 실행 불가. Orchestrator 레이어에서 강제한다.
7. **수집 데이터 재배포 금지**: 캡스톤 내부 용도로만 저장·사용한다. 원문 HTML 대량 복제 금지. 필요 최소 필드 + 출처 URL + 타임스탬프 중심.

## 완료 조건 (이 스킬 산출물이 "됐다"고 말할 수 있는 기준)

- [ ] 사우디 크롤러가 Supabase `saudi_products` 테이블에 공통 6컬럼 + 사우디 확장 컬럼 행을 실제로 삽입한다
- [ ] 삽입된 데이터가 IQR 이상치 검증을 통과하거나 `outlier_flagged` 플래그를 갖는다
- [ ] GitHub Actions 워크플로우가 최소 1회 이상 성공 실행 기록을 남긴다
- [ ] SFDA API 호출이 OAuth Bearer 토큰 흐름으로 동작한다
- [ ] 소매 사이트 최소 1곳(Nahdi/Al-Dawaa/Whites 중)에서 가격 필드가 수집된다
- [ ] `confidence` 필드가 소스 신뢰도에 따라 차등 부여된다 (공식 0.90+, 소매 0.70~0.85, 정적 fallback 0.30)

## 바이브코딩 진행 순서

처음부터 전체를 짜려 하지 말 것. 아래 순서로 얇은 층을 쌓는다. **각 단계는 assets에 있는 시드 파일을 먼저 복사·적용한 뒤 수정하는 것이 원칙**.

1. **DB 스키마 먼저**: `assets/sql/schema.sql` + `assets/sql/state_tables.sql`을 Supabase SQL 에디터에서 실행. 이 단계에서 `saudi_products` + 5개 상태 테이블이 전부 생성된다. 상세 규칙은 `references/schema.md` 참조.
2. **SFDA API 단독 동작**: `assets/snippets/sfda_oauth.py` 그대로 import → OAuth 토큰 발급 → 등록 의약품 1건 조회 → `map_sfda_to_schema()`로 변환 → `saudi_products` 삽입. 이 한 줄기가 끝나면 "돌아가는 뼈대" 완성. 상세는 `references/sources.md`의 SFDA 섹션.
3. **정규화·매칭 레이어**: `assets/snippets/normalizer.py`의 `normalize_record()`를 삽입 직전에 통과시킨다. 이어서 `assets/snippets/inn_normalizer.py`의 `normalize_record()`로 WHO INN 정규화를 수행한다 (inn_name, inn_id, inn_match_type 필드 추가). 소매 데이터를 SFDA와 매칭할 때는 `assets/snippets/identity_resolver.py`의 `find_best_match()` 사용.
4. **소매 1곳 추가**: `assets/sources.yaml`에서 `nahdi_web` OR `al_dawaa_web` 중 robots.txt·약관 통과한 쪽 하나만. 가격 필드 채우고 SFDA 매칭으로 `regulatory_id` 보강. 차단 방어는 `references/crawler_patterns.md`.
5. **GitHub Actions에 태우기**: `assets/workflows/sa_api_light.yml`을 `.github/workflows/`로 복사하고 Secrets 등록. 이 시점에 유동 IP 효과 확인. 운영 규칙은 `references/sop.md`.
6. **상태 영속화 전환**: 3~5단계까지 만든 크롤러가 로컬 변수로 상태를 들고 있다면, `assets/snippets/supabase_state.py`의 `TokenBucket`·`CircuitBreaker`·`FailedQueue`로 교체. Actions 러너 휘발성 대응.
7. **나머지 소스 확장**: NUPCO, Etimad, Whites 순. 각 소스마다 `assets/sources.yaml`에 엔트리 추가 → `references/crawler_patterns.md`의 방어 패턴 적용.
8. **AI 판단 크롤링**: 고정 URL 외 시장조사 성격의 수집은 Claude Haiku에 "이 페이지에서 가격·경쟁사·공급처 정보만 추출" 프롬프트로 별도 레이어. 여기서 `core/crawler.py`의 텍스트 밀도 엔진을 재활용 가능.

## 안티패턴 (절대 하지 말 것)

- ❌ 뉴스 크롤러 엔진(`crawler.py`의 텍스트 밀도 분석)을 가격표 추출에 그대로 재사용 — 뉴스는 본문 덩어리 하나, 가격표는 셀 N개라 본질이 다름. AI 판단 레이어에만 쓸 것.
- ❌ GitHub Actions 러너 로컬 디스크에 상태 저장 (`.failed_queue.json`, 메모리 캐시) — 러너 종료와 함께 증발. 상태는 Supabase 테이블로.
- ❌ 워크플로우 4개를 동일 cron 시각에 병렬 실행 — IP는 달라도 API 키 기준 rate limit 공유. 시간대 분산 필수.
- ❌ 소매 사이트에 로그인 세션 유지 — 약관 위반 + 법적 리스크. 공개 페이지만.
- ❌ `confidence` 1.0 부여 — 어떤 소스도 100% 신뢰하지 않는다. 공식 API도 최대 0.95.
- ❌ 토글 이름을 코드에 하드코딩 — 8개 토글은 설정 파일/DB로 관리. 토글명이 아직 미정이라 `toggle_1`~`toggle_8` 키로만 참조.

## 트러블슈팅 빠른 인덱스

| 증상 | 원인 후보 | 가야 할 파일 |
|---|---|---|
| SFDA API 401 | Bearer 토큰 만료(24h) | `references/sources.md` OAuth 섹션 + `assets/snippets/sfda_oauth.py` |
| 소매 사이트 403/429 | IP 차단 or Rate Limit | `references/crawler_patterns.md` 백오프 + `assets/snippets/backoff_retry.py` |
| Actions 잡 타임아웃 | Playwright 웜업 + 동시 실행 | `references/sop.md` 러너 섹션 |
| 가격 필드 null 많음 | 소스 DOM 변경 or 파서 셀렉터 오래됨 | `references/crawler_patterns.md` 동적 셀렉터 캐싱 |
| `regulatory_id` 매칭 실패 | 성분명 언어(아/영) or 함량 단위 불일치 | `assets/snippets/normalizer.py` + `assets/snippets/identity_resolver.py` |
| 함량 포맷 이상 (`"5 mg"` 인데 실제 500) | Decimal normalize 버그 | `assets/snippets/normalizer.py`의 `_clean_number` |
| 토큰 버킷이 요청 폭주 방지 안 함 | Actions 잡 간 상태 공유 안 됨 | `assets/snippets/supabase_state.py` + `assets/sql/state_tables.sql` |
| robots.txt 차단 표시 | REP 파싱 or 도메인 전체 차단 | `references/legal.md` robots 섹션 |
| `confidence` 1.0 삽입 실패 | check 제약 위반 (confidence < 1.00) | `assets/sql/schema.sql` DDL |

---

**참고**: 이 스킬은 사우디 단독이다. 다른 권역(싱가포르 1공정 등)은 별도 스킬로 분리된다. 사우디 작업이 끝나면 이 스킬의 경험을 다음 권역 스킬 제작에 reference로 활용한다.
