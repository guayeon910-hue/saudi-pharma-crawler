# Crawler Patterns — 방어·폴백·복원력 패턴

이 문서는 프로젝트 내부의 `core/crawler.py` 분석 보고서에서 검증된 패턴 + 학술 문헌(분산 크롤링 연구 동향 PDF)의 권장사항을 사우디 크롤러에 맞게 재구성한 것이다. **모든 소스 구현체는 이 문서의 패턴을 기본 전제로 삼는다.**

## 핵심 원칙

1. **실패를 정상으로 간주한다.** 크롤러의 90%는 실패 처리 코드다.
2. **서버 신호를 존중한다.** 429/Retry-After/503은 무시하고 재시도하면 오히려 장애를 증폭시킨다.
3. **상태는 외부에 저장한다.** Actions 러너는 휘발성이다.
4. **한 URL에서 무한히 매달리지 않는다.** 3회 실패 = 포이즌 필.
5. **가능하면 요청 자체를 줄인다.** ETag/If-None-Match로 304 받으면 최고.

---

## 패턴 1. 지수 백오프 + 지터 + 캡

### 언제

네트워크 오류, 타임아웃, 5xx, 429(Retry-After 없을 때)

### 규칙

- 기본 대기: 3초
- 지수 증가: 3 → 6 → 12 → 24 → 48
- **캡**: 최대 60초. 그 이상은 의미 없음
- **지터**: ±25% 랜덤 (retry storm 방지)
- **최대 재시도**: 5회. 이후 `failed_urls` 테이블로

### 왜 지터인가

동시 실패한 워커 N개가 동일 간격으로 재시도하면 서버에 또 다른 피크를 만든다. 랜덤 지터가 있으면 자연스럽게 분산된다. AWS/Google Cloud 모두 공식 문서에서 권장.

```
지터 없이: ████  ████  ████  ← 같은 시점 집중
지터 있음: ██ █ ██  █ ██  ██  ← 자연 분산
```

### tenacity 사용 예

```python
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=3, max=60, jitter=2)
)
async def fetch_with_retry(url: str):
    ...
```

---

## 패턴 2. Retry-After 헤더 존중

### 언제

HTTP 429 수신 시

### 규칙

1. 서버가 `Retry-After: <seconds>` 헤더를 주면 **지수 백오프보다 우선**
2. `Retry-After: <HTTP-date>` 형식이면 해당 시각까지 대기
3. 최대 캡은 300초 (5분). 그 이상이면 잡 자체를 실패로 처리하고 다음 cron에서 재시도

### 코드 스케치

```python
async def handle_429(resp):
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            wait = min(int(retry_after), 300)
        except ValueError:
            # HTTP-date 파싱
            wait = min(parse_http_date_delta(retry_after), 300)
        await asyncio.sleep(wait)
        return True  # 재시도 가능
    return False  # 백오프로 넘김
```

---

## 패턴 3. 403 + User-Agent 로테이션

### 언제

HTTP 403 Forbidden — 특정 User-Agent 차별적 차단

### 규칙

1. 백오프 없이 즉시 User-Agent 교체 → 재시도
2. UA 풀은 `fake-useragent` 또는 하드코딩 리스트
3. 같은 URL에서 3번 이상 403 → 해당 도메인 서킷 브레이커 오픈 (30분)

### UA 풀 예시

```python
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ...",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) ...",
]
```

**주의**: UA 스푸핑은 "차단 회피"가 아니라 "클라이언트 식별 다양화" 목적. 공격적 우회로 해석될 수 있는 행위(IP 풀 강제 순환, 세션 쿠키 조작)는 법적 리스크다. UA 교체는 HTTP 표준 범위 내 정상 행위.

---

## 패턴 4. WAF/CAPTCHA 감지

### 언제

응답이 정상 HTTP 200이지만 내용이 WAF 챌린지 페이지일 때

### 감지 규칙

```python
WAF_SIGNATURES = [
    "cloudflare",
    "cf-turnstile",
    "recaptcha",
    "hcaptcha",
    "incapsula",
    "akamai bot manager",
    "checking your browser",
]

def is_waf_challenge(html: str, resp_length: int) -> bool:
    # 1. HTML 기반 WAF
    html_lower = html.lower()
    if any(sig in html_lower for sig in WAF_SIGNATURES):
        return True
    # 2. 비정상적으로 짧은 403 JSON (API Gateway 패턴)
    if resp_length < 512 and '"error"' in html_lower:
        return True
    # 3. 강제 리다이렉션 히스토리 확인은 호출부에서
    return False
```

### 감지 후 조치

1. **즉시 중단.** 재시도하지 않는다. IP 블랙리스트 가속화 방지.
2. 해당 도메인 서킷 브레이커 오픈 (1시간)
3. `crawl_jobs` 테이블에 `error='waf_detected'` 기록
4. Slack 알림 (PM 확인 필요)

---

## 패턴 5. 서킷 브레이커

### 상태 머신

```
CLOSED (정상)
  │
  │ 연속 실패 5회
  ↓
OPEN (차단, 30분~1시간)
  │
  │ 타임아웃 경과
  ↓
HALF_OPEN (탐색)
  │
  │ 단일 요청 성공? → CLOSED
  │ 실패? → OPEN
  ↓
```

### 구현 위치

`source_circuit_state` 테이블:

```sql
create table source_circuit_state (
    domain text primary key,
    state text not null default 'closed',  -- closed/open/half_open
    failure_count int default 0,
    opened_at timestamptz,
    next_probe_at timestamptz
);
```

### 규칙

- **WAF 감지**: 즉시 OPEN, 60분
- **5회 연속 5xx**: OPEN, 30분
- **5회 연속 403**: OPEN, 30분 (UA 로테이션 후에도 실패한 경우)
- **HALF_OPEN 전환**: 타임아웃 경과 후 다음 잡 시작 시

---

## 패턴 6. 포이즌 필 (Poison Pill)

### 왜

같은 URL이 계속 실패하면 재시도 큐가 무한히 부풀어오른다. 리소스 낭비 + 진짜 처리해야 할 신규 URL이 밀린다.

### 규칙

`failed_urls.fail_count >= 3` → `dead_urls`로 이동. 영구 폐기.

```sql
create table dead_urls (
    url text primary key,
    first_failed_at timestamptz not null,
    last_failed_at timestamptz not null,
    fail_count int not null,
    last_error text,
    killed_at timestamptz default now()
);
```

`dead_urls`에 있는 URL은 다시 큐에 들어가지 않는다. 수동으로 되살리려면 PM이 SQL로 직접 지운다.

---

## 패턴 7. 다중 폴백 (Triple Fallback)

### 대상

HTML 스크래핑 소스 (소매 약국 등)

### 순서

1. **1차**: 하드코딩 CSS 셀렉터 (알려진 구조)
2. **2차**: 동적 셀렉터 캐시 조회 → 있으면 사용, 없으면 다음
3. **3차**: 텍스트 밀도 분석 or JSON 객체 탐색 (`__INITIAL_STATE__`, `__NEXT_DATA__`)

### 텍스트 밀도 분석 → Trafilatura 교체 (2026-04-10)

기존 자체 텍스트 밀도 분석을 **Trafilatura 2.0** (F-Score 0.896)으로 교체.
동일 원리(텍스트 많고 링크 적은 부분 = 본문)를 정밀하게 구현한 라이브러리.

**시뮬레이션 결과 (10개 소스 대상)**:

| 분류 | 소스 | 결과 | 비고 |
|------|------|------|------|
| SUCCESS | sfda_drugs_list_html | 833자, 가격+아랍어 | 정적 HTML 최적 |
| SUCCESS | etimad_portal | 2,717자 | 본문 풍부 |
| SUCCESS | whites_web | 464자, 상품명 감지 | __NEXT_DATA__ 존재 |
| PARTIAL | sfda_companies | 217자 | JS 렌더링, 네비게이션만 |
| PARTIAL | nupco_tenders | 188자 | 동적 로딩, 헤더만 |
| FAIL | nahdi_web | 0자 (HTML 862KB) | SPA — JSON 파싱으로 우회 |
| FAIL | al_dawaa_web | HTTP 403 | WAF 차단 (Trafilatura 이전 문제) |
| FAIL | tamer_group | HTTP 403 | WAF 차단 |
| FAIL | noon_saudi | HTTP 403 | Cloudflare |

**핵심 발견**: Trafilatura 자체 실패 0건. HTTP 200이 오면 100% 작동.

#### 적용 순서 (Quad Fallback)

1. **1차**: 하드코딩 CSS 셀렉터
2. **2차**: 동적 셀렉터 캐시
3. **2.5차**: 구조화 JSON 탐색 (`__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`)
4. **3차**: Trafilatura 본문 추출 (`assets/snippets/trafilatura_fallback.py`)

#### 적용 대상 vs 부적합

- **O**: 규제 페이지 본문, 뉴스/시장조사, 서비스 설명 텍스트
- **O**: SPA 소스의 `__NEXT_DATA__` JSON 파싱 (2.5차)
- **X**: 가격표/상품카드 직접 파싱 (텍스트 밀도 원리 한계)
- **X**: 403 차단 소스 (Trafilatura 이전에 UA 로테이션/서킷브레이커가 먼저)

#### confidence 감점 규칙

Trafilatura fallback 사용 시 confidence를 감점한다:
- 구조화 JSON 성공: **-0.03** (신뢰도 높음)
- Trafilatura 정적 HTML 성공: **-0.05**
- 짧은 추출 (200자 미만): **-0.10**
- 추출 실패: **-0.15**

**주의**: 가격표·상품 카드에는 여전히 부적합. 가격표는 `<table>` 또는 repeating `<div class="product-card">` 구조라 텍스트 밀도가 낮다. CSS 셀렉터가 정답.

### 동적 셀렉터 캐시

한 번 텍스트 밀도 분석으로 본문 블록을 찾으면, 그 블록의 `id` 또는 `class`를 1시간 TTL로 `source_selectors` 테이블에 저장. 다음 요청은 이 셀렉터를 바로 시도하고 실패 시에만 재분석.

```sql
create table source_selectors (
    source_domain text,
    selector_purpose text,  -- 'article_body' / 'product_card' / 'price_text'
    selector_value text,     -- 'div.article-content' 등
    ttl_until timestamptz,
    primary key (source_domain, selector_purpose)
);
```

---

## 패턴 8. HTTP 조건부 요청 (ETag / If-None-Match)

### 왜

"변경 없음"인 페이지를 매번 풀로 다운로드하면 서버·클라이언트 양쪽이 손해. ETag로 304 받으면 바디 전송 생략 → 대역폭·서버 CPU·크롤러 처리비용 동시 감소.

### 구현

1. 첫 수집 시 응답 헤더의 `ETag` 저장 (`source_cache` 테이블)
2. 재수집 시 `If-None-Match: <etag>` 헤더 부착
3. 304 수신 → 기존 데이터 유지, `crawled_at`만 갱신
4. 200 수신 → 새 데이터 + 새 ETag 저장

```sql
create table source_cache (
    url text primary key,
    etag text,
    last_modified text,
    last_fetched_at timestamptz,
    last_status int
);
```

### 주의

ETag가 없는 서버가 많다. 있으면 활용, 없으면 그냥 풀 요청. 강제하지 말 것.

---

## 패턴 9. 레이트 리미터 (토큰 버킷)

### 왜

동일 도메인에 초당 N회 이상 때리면 차단된다. 워커가 여러 개라면 더 주의.

### 단일 프로세스 버전

```python
class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.monotonic()

    async def acquire(self):
        while True:
            now = time.monotonic()
            self.tokens = min(
                self.capacity,
                self.tokens + (now - self.last) * self.rate
            )
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(1 / self.rate)
```

### 분산(Actions 다중 잡) 버전

GitHub Actions 잡 여러 개가 병렬로 돌 때는 로컬 토큰 버킷이 의미 없다. Supabase `rate_limit_state` 테이블을 공유 상태로 사용:

```sql
create table rate_limit_state (
    key_name text primary key,       -- 'sfda_api', 'nahdi_domain' 등
    tokens numeric not null,
    capacity numeric not null,
    rate_per_sec numeric not null,
    last_refill_at timestamptz not null
);
```

API 호출 직전 트랜잭션으로 토큰 차감. 경합 시 Supabase의 Row Level Lock이 자동 처리. 고빈도(10+ QPS) 호출에는 오버헤드가 크지만, 사우디 크롤러는 대부분 1 QPS 이하라 충분.

### 도메인별 기본값 (권장)

| 도메인 | QPS | 동시성 |
|---|---|---|
| SFDA API | 2 | 3 |
| NUPCO | 0.5 | 1 |
| Etimad | 0.5 | 1 |
| Nahdi/Al-Dawaa/Whites | 0.3 | 1 |
| Noon/Amazon | 0.2 | 1 |

**시작은 보수적으로.** 차단 없이 며칠 돌아가면 조금씩 올린다.

---

## 패턴 10. robots.txt 준수

### 구현

`robots.txt`를 잡 시작 시 1회 fetch → 파싱 → Supabase `robots_cache` 테이블에 24시간 TTL 캐싱.

```python
from urllib.robotparser import RobotFileParser

async def is_allowed(url: str, user_agent: str = "*") -> bool:
    domain = urlparse(url).netloc
    robots = await get_cached_robots(domain)
    if not robots:
        robots_url = f"https://{domain}/robots.txt"
        content = await fetch_text(robots_url)
        robots = RobotFileParser()
        robots.parse(content.splitlines())
        await cache_robots(domain, content)
    return robots.can_fetch(user_agent, url)
```

### 원칙

- `Disallow: /` 만나면 해당 경로 자체를 건드리지 않는다
- `Crawl-delay` 지시자가 있으면 토큰 버킷 rate를 그에 맞춰 낮춘다
- **robots.txt는 "허가"가 아니라 "요청"이다.** 준수한다고 법적 면책이 아니다. `legal.md` 참조.

---

## 패턴 11. 데이터 직렬화 방어 (`_sanitize_article`)

### 왜

수집 과정에서 `bytes`, `datetime`, `Decimal` 같은 객체가 섞이면 Supabase JSON 삽입 시 크래시한다.

### 규칙

INSERT 직전 모든 값을 다음 중 하나로 강제 변환:
- `str` (UTF-8)
- `int` / `float`
- `bool`
- `None`
- `list` / `dict` (재귀 변환)

```python
def sanitize(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', errors='replace')
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: sanitize(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [sanitize(x) for x in v]
    return str(v)  # 최후
```

---

## 패턴 12. 서버 다운스트림 장애 이중 확인

### 왜

사이트 백엔드 DB 에러로 5xx가 뜨면 "우리가 차단당했다"고 오인해서 쓸데없이 세션 갱신 로직이 돌아간다. 실제로는 사이트 자체가 죽은 상태.

### 규칙

5xx 수신 시, 같은 도메인에 **1KB 경량 프로브**를 한 번 날려본다:

```python
async def probe_domain(domain: str) -> bool:
    """도메인 자체가 살아있는지 확인. True면 우리 요청만 실패."""
    try:
        resp = await client.get(
            f"https://{domain}/",
            headers={"Range": "bytes=0-1023"},
            timeout=5
        )
        return resp.status_code < 500
    except Exception:
        return False
```

- 프로브 실패 (도메인 전체 장애) → 해당 도메인 다음 잡까지 skip, 세션 갱신 안 함
- 프로브 성공 (우리 요청만 문제) → 세션/인증 갱신 로직 진입

---

## 패턴 적용 체크리스트

새 소스 커넥터를 만들 때 아래를 전부 체크:

- [ ] `tenacity` 지수 백오프 + 지터 + 캡 60초 적용
- [ ] 429 시 `Retry-After` 헤더 우선 처리
- [ ] 403 시 UA 로테이션 후 재시도
- [ ] WAF 감지 함수 통과
- [ ] 서킷 브레이커 상태 확인 (잡 시작 시)
- [ ] 포이즌 필 (fail_count >= 3)
- [ ] robots.txt 확인
- [ ] 도메인별 레이트 리미터
- [ ] ETag 조건부 요청 (가능한 경우)
- [ ] INSERT 전 `sanitize()` 호출
- [ ] 모든 예외는 `crawl_jobs` 테이블에 기록
