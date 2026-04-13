# Sources — 사우디 소스 우선순위 및 접근 방법

## 소스 레이어 (신뢰도 순)

1. **공식 규제** (최상) — SFDA
2. **공공조달** — NUPCO, Etimad (MoF)
3. **대형 소매 약국** — Nahdi, Al-Dawaa, Whites
4. **도매/유통** — Tamer Group, Ultra Group
5. **대형 상거래** (보조) — Noon, Amazon.sa

동일 품목이 여러 레이어에서 나오면 교차검증한다. 예: SFDA 공식 가격과 Nahdi 소매가가 20% 이상 차이나면 프로모션/세금/패키지 차이로 플래그.

---

## 1. Saudi Food and Drug Authority (SFDA)

**가장 중요한 소스.** 등록번호·성분·함량·제형·제조사·마케팅사·에이전트·ATC·가격까지 구조화되어 제공된다.

### 접근 방법

우선순위 1: **Developer Portal API (Open Data, Bearer token)**
- 엔트리: `https://developer.sfda.gov.sa/`
- Registered Drug Service: `https://developer.sfda.gov.sa/apidoc/registered-drug-service/84`
- OAuth: `https://developer.sfda.gov.sa/apidoc/oauth/78`
- 검색 키: 바코드(GTIN) / 등록번호 / 키워드

우선순위 2: **웹 목록/상세 HTML** (API 실패 시 폴백)
- `https://www.sfda.gov.sa/en/drugs-list`
- `https://www.sfda.gov.sa/en/drug-companies` (회사 마스터)

### OAuth 흐름 (필수 구현)

1. Client Credentials로 토큰 발급 (서버-서버)
2. 토큰 만료 **24시간** → 매 잡 시작 시 재발급 권장
3. 모든 API 호출에 `Authorization: Bearer <token>` 헤더

```python
# 개념 스케치 — 실제 엔드포인트는 Developer Portal 콘솔에서 확인
# 주의: SFDA 포털은 구독 승인이 필요할 수 있음
async def get_sfda_token(client_id: str, client_secret: str) -> str:
    # POST /oauth/token with client_credentials grant
    # 응답에서 access_token 추출
    # Supabase rate_limit_state에 발급 시각 기록
    ...
```

### 필드 매핑 (SFDA → 공통 8필드)

| SFDA 필드 | 공통 스키마 |
|---|---|
| Register Number | `regulatory_id` |
| Trade Name | `trade_name` |
| Scientific Name | `scientific_name` |
| Strength + Unit | `strength` (문자열 결합) |
| Dosage Form | `dosage_form` |
| Price | `price_sar` |
| Manufacturer / Marketing Company | `manufacturer_or_marketing_company` |
| First/Second/Third Agent | `agent_or_supplier` |

추가 확장: `atc_code`, `package_size`, `public_price_vat_included`

### 수집 주기

- 공식 가격은 SFDA 절차상 "가격 변경 통지 후 180일 적용 기간"이 있어 **초단위 변화가 없다**. 일 1회 또는 주 2~3회로 충분.
- `confidence`: 0.90 ~ 0.95

---

## 2. NUPCO (공공조달 GPO)

국가 단위 공동구매 주체. 병원 납품 가격의 실질 기준이 된다.

### 접근

- 공개 텐더 목록: `https://www.nupco.com/en/tenders/`
- 상세/결과는 포털 로그인이 필요한 경우가 있음 — **로그인 우회 금지**

### 수집 가능 데이터

- 텐더 ID, 제목, 마감일, 카테고리, 결과 공개 여부
- 가격은 결과 문서가 공개된 건에 한해
- 낙찰 공급자 → `agent_or_supplier` 필드 강화 재료

### 주기

주 1회 (신규 텐더/결과 갱신 확인)

### 주의

- 텐더 문서가 아랍어 PDF인 경우가 많음 → `pdfplumber` + Claude Haiku 요약으로 처리
- "로그인 필요 내부 시스템"에는 절대 접근하지 않는다. 공개 범위만.

---

## 3. Etimad Platform (MoF)

사우디 정부 통합 디지털 조달 플랫폼. 계약 데이터 조회 API가 존재하지만 **구독 필요 가능성** 있음.

### 접근

- 공개 페이지: `https://www.mof.gov.sa/en/eservices/Pages/Etimad.aspx`
- Developer Portal: `https://apiportal.etimad.sa/en`
- API 상품 (예: Contracts Plus): `https://apiportal.etimad.sa/en/api_products`
- 약관: `https://apiportal.etimad.sa/en/terms-and-conditions`

### 인증

- NAFATH(사우디 국가 로그인) 기반 로그인 필요한 포털 영역 존재
- API는 별도 키/구독 절차 — **팀 차원의 법인 계정 필요**

### 구현 전 체크리스트

- [ ] API 구독이 필요한지 확인
- [ ] 약관에 "자동화 크롤링 허용" 문구가 있는지
- [ ] 무료 티어 존재 여부
- [ ] 실패 시 NUPCO로 폴백 가능한지

현실적으로 **D7 이후 미션**으로 뒤로 빼는 것을 권장. SFDA + NUPCO + 소매 1곳으로 뼈대부터 완성.

---

## 4. 대형 소매 약국

### 4-1. Nahdi Medical Company

- URL: `https://www.nahdionline.com/en-sa`
- 유형: 대형 체인 온라인 약국
- 접근: **공개 상품 페이지 HTML 스크래핑만**
- 수집 필드: 제품명, 가격 (SAR), 프로모션 표시, 재고 상태, 카테고리
- 주기: 일 1회 (프로모션 변동)
- `confidence`: 0.70 ~ 0.85
- 주의: 앱/내부 API는 약관·법무 검토 전에는 건드리지 않는다

### 4-2. Al-Dawaa Pharmacies

- URL: `https://www.al-dawaa.com/en/`
- 유형: 대형 체인 약국
- 접근: 공개 상품 페이지
- 동일한 주기·confidence

### 4-3. Whites

- URL: `https://www.whites.sa/en-sa`
- 유형: 헬스/OTC 중심
- OTC·건강기능식품 비중이 높아 처방약 커버리지는 상대적으로 낮음

### 공통 구현 주의사항

1. **검색 → 상세 2단계**: 검색 결과에서 URL을 수집한 뒤, 상세 페이지에서 가격 추출. 검색 결과에서 가격이 직접 보이는 경우도 있음.
2. **SFDA 매칭 필수**: 소매 사이트는 `regulatory_id`를 거의 노출하지 않는다. 수집 직후 `normalizer` → `resolver`가 trade_name + strength + dosage_form으로 SFDA를 검색해 `regulatory_id`를 보강한다.
3. **가격 단위 확인**: SAR 표기지만 일부 패키지는 "per pack" vs "per tablet"로 다르므로 `package_size` 필드 필수.
4. **프로모션 표시 저장**: "30% off" 같은 할인율은 원문 문자열로 `promo_raw` 확장 컬럼에 저장. 세일가만 저장하면 평균 가격 통계가 왜곡된다.

---

## 5. 도매/유통

### 5-1. Tamer Group
- `https://tamergroup.com/`
- `https://tamergroup.com/sectors/distribution-healthcare-fmcg`
- 수집 목표: 유통 브랜드 포트폴리오 → 공급처 마스터 구축

### 5-2. Ultra Group
- `https://ultra.com.sa/service-detail?service=0`
- 동일 목적

### 주기

월 1회 (공급처 마스터는 자주 안 바뀜)

### 수집 전략

가격 정보가 아닌 **"어떤 회사가 어떤 브랜드를 유통하는가"** 관계 데이터 구축. `supplier_mapping` 확장 테이블에 (brand, distributor) 쌍으로 저장해서, 나중에 파트너 매칭 모듈이 참조한다.

---

## 6. 대형 상거래 (보조)

### Noon / Amazon.sa

- Noon: `https://www.noon.com/saudi-en/health/main-pharmacy-sa/`
- Amazon: `https://www.amazon.sa/-/en/medicine/s?k=medicine`

### 주의

- RX(처방약)는 정책상 노출 제한 가능 → OTC·건강기능식품 중심
- 차단 위험 중~상 → 요청 간격 매우 보수적으로
- 우선순위 낮음. SFDA + NUPCO + 소매 3곳이 먼저 안정된 뒤 붙인다.

---

## 소스 우선순위 요약 테이블

| 우선순위 | 소스 | 유형 | 접근 방법 | 주기 | confidence | 차단 위험 |
|---|---|---|---|---|---|---|
| 1 | SFDA API | 규제 공식 | OAuth API | 일 1회 | 0.90~0.95 | 낮음 |
| 2 | NUPCO | 조달 | HTML | 주 1회 | 0.80~0.90 | 중간 |
| 3 | Etimad API | 조달 | API (구독) | 주 1회 | 0.80~0.90 | 중간 |
| 4 | Nahdi | 소매 | HTML | 일 1회 | 0.75~0.85 | 중간 |
| 5 | Al-Dawaa | 소매 | HTML | 일 1회 | 0.75~0.85 | 중간 |
| 6 | Whites | 소매 | HTML | 일 1회 | 0.70~0.80 | 중간 |
| 7 | Tamer | 유통 | HTML | 월 1회 | 0.60~0.75 | 낮음 |
| 8 | Ultra | 유통 | HTML | 월 1회 | 0.60~0.75 | 낮음 |
| 9 | Noon | 상거래 | HTML | 일 1회 | 0.50~0.70 | 중~상 |
| 10 | Amazon.sa | 상거래 | HTML | 일 1회 | 0.50~0.70 | 중~상 |

## 구현 순서 (권장)

1. **D1~D2**: SFDA API (OAuth + 검색 + 상세) — 단독 돌아가는 뼈대
2. **D3**: Nahdi 또는 Al-Dawaa 중 robots.txt·약관 통과한 1곳 + SFDA 매칭
3. **D4**: NUPCO 공개 텐더 (PDF 처리 포함)
4. **D5**: 두 번째 소매 + Whites
5. **D6~D7**: Tamer/Ultra 도매 (공급처 마스터)
6. **D8+**: Etimad API 구독 확보 후 추가, Noon/Amazon 보조

---

## 8개 토글과 소스 매핑 (예시 자리표시)

토글명은 아직 미정이므로 목적 태그로만 매핑한다. 실제 토글명이 확정되면 `orchestrator/toggles.py`의 설정 딕셔너리만 업데이트.

| 토글 ID | 목적 태그 (예시) | 연결 소스 |
|---|---|---|
| toggle_1 | regulatory | SFDA API |
| toggle_2 | retail_primary | Nahdi + Al-Dawaa |
| toggle_3 | procurement | NUPCO + Etimad |
| toggle_4 | wholesale | Tamer + Ultra |
| toggle_5 | cross_validate | SFDA + Nahdi + NUPCO 머지 |
| toggle_6 | category_focus | 특정 ATC 코드 집중 |
| toggle_7 | strict_mode | robots 엄격 + 실패 null 허용 |
| toggle_8 | exploratory | AI 판단 레이어 전용 |

"토글 1개만 실행" 제약은 Orchestrator에서 강제. 동시 실행 시 `HTTP 409 Conflict` 반환.
