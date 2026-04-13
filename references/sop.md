# SOP — GitHub Actions 운영 매뉴얼

## GitHub Actions를 쓰는 이유 (재확인)

1. **유동 IP**: Azure 러너 대역에서 잡마다 다른 egress IP → 차단 회피 1차 방어
2. **무료**: public repo 무제한, private도 월 2000분
3. **스케줄링**: cron 네이티브 지원
4. **시크릿 관리**: Repository Secrets로 중앙집중
5. **실패 알림**: failure() 조건으로 Slack 훅 쉽게 연결
6. **실행 이력**: Actions 탭에서 시각적으로 확인

**한계**: 러너 휘발성 → 상태는 전부 Supabase로 외부화 (`architecture.md` 참조).

---

## 워크플로우 파일 구조

사우디 크롤러는 **4개 워크플로우**로 분리한다. cron 시간대를 분산해 API 키 기준 rate limit 충돌을 회피.

| 워크플로우 파일 | 대상 | 실행 주기 | 러너 |
|---|---|---|---|
| `sa_api_light.yml` | SFDA API | 매일 01:00 AST (UTC 22:00 전일) | `ubuntu-latest` |
| `sa_retail.yml` | Nahdi / Al-Dawaa / Whites | 매일 02:00 AST | `ubuntu-latest` |
| `sa_procurement.yml` | NUPCO / Etimad | 주 1회 월요일 03:00 AST | `ubuntu-latest` |
| `sa_wholesale.yml` | Tamer / Ultra | 월 1회 매월 1일 04:00 AST | `ubuntu-latest` |

**AST = Arabia Standard Time = UTC+3**

cron은 UTC 기준이므로:
- 01:00 AST = 22:00 UTC (전일)
- 02:00 AST = 23:00 UTC (전일)
- 03:00 AST = 00:00 UTC
- 04:00 AST = 01:00 UTC

---

## sa_api_light.yml 예시

```yaml
name: SA SFDA API Crawler

on:
  schedule:
    - cron: '0 22 * * *'   # 매일 01:00 AST
  workflow_dispatch: {}    # 수동 실행 허용

concurrency:
  group: sa-api-light
  cancel-in-progress: false  # 이전 잡 끝날 때까지 대기 (취소 금지)

jobs:
  crawl:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install deps
        run: |
          pip install -r crawlers/saudi/requirements.txt

      - name: Run SFDA crawler
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
          SFDA_CLIENT_ID: ${{ secrets.SFDA_CLIENT_ID }}
          SFDA_CLIENT_SECRET: ${{ secrets.SFDA_CLIENT_SECRET }}
          CLAUDE_API_KEY: ${{ secrets.CLAUDE_API_KEY }}
          TOGGLE_ID: toggle_1   # regulatory
        run: |
          python -m crawlers.saudi.orchestrator.runner --toggle $TOGGLE_ID

      - name: Notify on failure
        if: failure()
        uses: slackapi/slack-github-action@v1.27.0
        with:
          payload: |
            {
              "text": "🚨 SA SFDA 크롤러 실패",
              "blocks": [
                {
                  "type": "section",
                  "text": {
                    "type": "mrkdwn",
                    "text": "*사우디 SFDA 크롤러 실패*\n• Run: ${{ github.run_id }}\n• Actor: ${{ github.actor }}\n• <${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}|로그 보기>"
                  }
                }
              ]
            }
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

### 중요 설정

- **`cancel-in-progress: false`**: 이전 잡이 돌고 있으면 새 잡은 대기. 취소하면 포이즌 필이 잘못 발동할 수 있음.
- **`timeout-minutes: 20`**: SFDA API 잡은 20분을 넘으면 안 됨. Playwright 잡은 30~40분.
- **`cache: 'pip'`**: 콜드 스타트 시간 단축. 의존성 변경 없으면 30초 → 5초.

---

## GitHub Secrets 등록

Repository → Settings → Secrets and variables → Actions → New repository secret

| Secret Name | 내용 | 관리 |
|---|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL | 백엔드 리드 |
| `SUPABASE_KEY` | Service Role Key (Server-side only) | 백엔드 리드 |
| `SFDA_CLIENT_ID` | SFDA Developer Portal OAuth Client ID | 백엔드 리드 |
| `SFDA_CLIENT_SECRET` | SFDA OAuth Client Secret | 백엔드 리드 |
| `ETIMAD_API_KEY` | Etimad API 키 (구독 확보 후) | 백엔드 리드 |
| `CLAUDE_API_KEY` | Claude Haiku API 키 | 백엔드 리드 |
| `SCRAPER_API_KEY` | ScraperAPI (GH Actions IP 부족 시 폴백) | 백엔드 리드 |
| `CAPTCHA_API_KEY` | 2captcha (HSA 등 CAPTCHA 돌파 시) | 백엔드 리드 |
| `SLACK_WEBHOOK_URL` | 실패 알림 Slack Webhook | PM |

### 주의사항

1. **Secrets는 로그에 자동 마스킹된다.** 그래도 `echo $SECRET` 같은 실수는 금지.
2. **Service Role Key는 절대 클라이언트 코드에 노출 금지.** 브라우저/앱에서는 anon key만.
3. **개인 `.env` 금지.** 팀원 각자 로컬에 동일 값을 복사해두지 말 것. 로컬 테스트는 Supabase 별도 dev 프로젝트 사용.
4. **예산 모니터링**: `SCRAPER_API_KEY`, `CAPTCHA_API_KEY`는 80% 소진 시 별도 알림 설정.

---

## cron 스케줄 분산 원칙

모든 잡을 00:00 UTC에 몰면 API 키 기준 rate limit이 터진다. 아래 원칙을 지킨다.

1. **최소 1시간 간격**: 워크플로우 간 시작 시각은 1시간 이상 벌린다.
2. **소스별 격리**: 같은 API를 쓰는 잡은 서로 겹치지 않게.
3. **중동 야간 시간대 활용**: 사우디 현지 새벽 01~04시 (UTC 22~01) = 현지 트래픽 최저 → 서버 부하 최소.
4. **GitHub의 5분 최소 간격**: cron은 최소 5분 간격까지 지원. 그 이하는 안 됨.

---

## 워크플로우 실행 중 해야 할 일 vs 하지 말아야 할 일

### 해야 할 일

- **Supabase `crawl_jobs` 테이블에 실행 시작/종료/상태 기록**
- **`toggle_id` 파라미터로 실행 스코프 명시**
- **잡 시작 시 서킷 브레이커 상태 확인**
- **잡 종료 시 수집 통계 출력** (수집 건수, 실패 건수, 평균 latency)
- **예외는 stderr로 출력** → Actions 로그에 기록 → Slack 알림

### 하지 말아야 할 일

- **러너 로컬 디스크에 상태 저장** — 휘발됨
- **워크플로우 간 파일 공유** — artifacts로 임시 공유는 가능하지만 영속 상태는 Supabase로
- **동일 cron 시각 중복** — 한 워크플로우 안에서만 matrix 병렬 허용
- **timeout-minutes 미설정** — 무한 루프 시 Actions 월 분량을 소진
- **`continue-on-error: true`를 핵심 step에** — 진짜 실패를 숨김

---

## Supabase 상태 테이블 운용

### crawl_jobs

```sql
create table crawl_jobs (
    id uuid primary key default gen_random_uuid(),
    workflow_name text not null,
    toggle_id text,
    github_run_id text,
    started_at timestamptz not null,
    finished_at timestamptz,
    status text not null default 'running',  -- running/success/partial/failed
    records_inserted int default 0,
    records_failed int default 0,
    error_message text,
    error_stack text
);

create index on crawl_jobs (started_at desc);
create index on crawl_jobs (workflow_name, started_at desc);
```

### 잡 시작 시

```python
async def job_start(workflow_name: str, toggle_id: str) -> str:
    resp = await supabase.table('crawl_jobs').insert({
        'workflow_name': workflow_name,
        'toggle_id': toggle_id,
        'github_run_id': os.environ.get('GITHUB_RUN_ID'),
        'started_at': datetime.now(timezone.utc).isoformat(),
        'status': 'running'
    }).execute()
    return resp.data[0]['id']
```

### 잡 종료 시

```python
async def job_finish(job_id: str, status: str, stats: dict, error: str = None):
    await supabase.table('crawl_jobs').update({
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'status': status,
        'records_inserted': stats.get('inserted', 0),
        'records_failed': stats.get('failed', 0),
        'error_message': error,
    }).eq('id', job_id).execute()
```

---

## 실패 대응 런북

### 증상 1: SFDA 잡이 인증 실패로 죽음

1. Actions 로그에서 `401 Unauthorized` 확인
2. Supabase `rate_limit_state`에서 `sfda_api` row의 `last_refill_at` 확인
3. SFDA Developer Portal 로그인 → 클라이언트 크리덴셜 상태 확인
4. 토큰이 revoked면 재발급 → GitHub Secrets 업데이트
5. 수동 `workflow_dispatch`로 재실행

### 증상 2: 소매 사이트 전체가 403

1. WAF 감지 여부 확인 (`crawl_jobs.error_message`)
2. 서킷 브레이커가 OPEN 상태인지 확인
3. 해당 도메인 `robots.txt` 변경 여부 확인
4. **즉시 재시도 금지.** 최소 1시간 대기 후 다른 시간대에서 재시도
5. 3일 연속 실패 → PM에게 보고 → 정적 fallback 전환

### 증상 3: Actions 잡 타임아웃

1. `timeout-minutes` 초과 원인 파악 (Playwright 웜업? 무한 루프? 외부 API 지연?)
2. 개별 소스 레이트 리미터 확인
3. Supabase 쿼리 지연 여부 확인
4. 필요 시 워크플로우 분리 (한 잡에 너무 많이 묶지 말 것)

### 증상 4: 3일 연속 실패

**즉시 PM 보고.** Slack 알림이 3회 이상 쌓이면 수동 개입 시점.

---

## PM 보고 기준

| 사건 | 보고 타이밍 |
|---|---|
| 공통 6컬럼 변경 필요 | 즉시 (전원 영향) |
| 사우디 사이트가 D3까지 안 긁힐 경우 | CSV 어댑터 우회 후 보고 |
| LLM API 사용량 급증 | 즉시 |
| Actions 워크플로우 3일 연속 실패 | 즉시 |
| ScraperAPI / 2captcha 예산 80% 소진 | 즉시 |
| 법적 리스크 감지 (약관 변경, 저작권 경고 수신) | 즉시 |

---

## 배포 환경 선택 가이드

| 소스 성격 | 권장 실행 환경 | 이유 |
|---|---|---|
| SFDA / NUPCO / Tamer / Ultra | GitHub Actions (Ubuntu) | 경량 HTTP, 20분 내 완료 |
| Nahdi / Al-Dawaa / Whites 정적 페이지 | GitHub Actions (Ubuntu) | Selectolax면 충분, 브라우저 불필요 |
| 동적 렌더링 필수 사이트 | **Render (필요 시만 스핀업)** | Playwright 헤비 작업, GH Actions는 시간 제약 |
| Etimad API | GitHub Actions | API 구독 후 경량 호출 |

**Render 비용 절감**: Playwright가 필요한 경우에만 Render Free Plan (월 750시간)의 일부를 사용. GitHub Actions가 REST API로 Render 서비스를 wake up → 실행 → sleep 시키는 방식. 월 실제 사용 시간 < 60시간이면 무료.

---

## 개발 중 로컬 테스트

Actions 없이 로컬에서 테스트할 때:

```bash
# 1. 로컬 .env 파일 (커밋 금지, .gitignore에 포함)
cp .env.example .env
# SFDA_CLIENT_ID 등 채우기 (개인 dev 크리덴셜)

# 2. Supabase dev 프로젝트 분리
# Production이 아닌 dev 인스턴스 URL로

# 3. 단일 토글 실행
python -m crawlers.saudi.orchestrator.runner --toggle toggle_1 --limit 5
# --limit 5 로 5건만 수집해서 동작 확인

# 4. DB에서 결과 확인
# Supabase SQL Editor에서 select * from saudi_products order by crawled_at desc limit 10;
```

**절대 프로덕션 크리덴셜로 로컬에서 돌리지 말 것.** 로컬에서 실수로 무한 루프 돌면 API 쿼터 다 태운다.
