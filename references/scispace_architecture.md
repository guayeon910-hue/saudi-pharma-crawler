# 📊 사우디 약가 데이터 파이프라인 (Data Ingestion Architecture)

시스템은 분산 환경에서도 안정적으로 대량의 비정형 웹 데이터를 수집(Ingestion)하고, 이를 다층 구조의 정규화(Normalization) 프로세스를 거쳐 고신뢰성의 정형 데이터베이스로 구축하도록 설계되었습니다.

## Phase 1: 분산 환경 오케스트레이션 및 상태 제어 (Distributed Target Crawling)
GitHub Actions와 같은 임시(Ephemeral) 런타임 환경에서도 상태 유실 없이 대규모 트래픽을 관장하기 위해, 데이터베이스(Supabase) 중심의 상태(State) 머신을 구현했습니다.

* **Circuit Breaker (서킷 브레이커 패턴)**: 대상 웹 서버가 방화벽(WAF) 차단(403)이나 에러(5xx), 타임아웃을 연쇄 발생시킬 시 해당 소스에 대한 접근을 일시적으로 차단(Open)하고 대기 타이머를 발동시켜 크롤러 시스템의 리소스 누수를 방지합니다.
* **Token Bucket (속도 제어 알고리즘)**: 크롤러 워커들이 공유할 수 있는 토큰 버킷 기반 레이트 리밋(Rate Limiting)을 구현하여, 대상 서버의 가용성을 침해하지 않는 선에서 윤리적이고 안전한 빈도의 연속 Request를 보장합니다.
* **Generator-based Pagination**: 페이지네이션 과정에서 메모리 오버헤드가 발생하지 않도록 제너레이터 패턴(`iter_all`, `iter_search`)으로 구현하였으며, 폐쇄적인 API 대신 공개 웹 엔드포인트를 우회 맵핑(`map_web_to_schema`)하여 처리 효율을 높였습니다.

## Phase 2: 휴리스틱 파싱 및 제1차 정규화 (Heuristic Normalization)
웹에서 수집된 비정형 메타데이터(문자열)를 스키마(Schema) 규칙에 맞게 일원화 및 검증합니다. 

* **다중 복합제 분해 체인**: `500/125mg` 와 같이 파편화 기재된 함량(Strength) 데이터를 토큰화 알고리즘을 통해 탐색하고 뒤에서부터 단위(mg, ml 등)를 앞의 실숫값으로 전파하여 독립된 규격 문자열로 자동 치환합니다.
* **제형(Dosage Form) 클러스터링**: 긴 텍스트로 기입된 제형(예: "Film-Coated Tablet", "SOFT GELATIN CAPSULE")을 부분 문자열(Substring) 분석을 통해 15개의 최상위 표준 제형 티어(Tablet, Capsule, Injection 등)로 정형(Mapping)합니다. 
* **PII Redaction (개인 식별 정보 마스킹)**: 제약사 및 에이전트의 메타데이터에 실수로 노출된 이메일 계정, 국제 전화번호 폼, 사우디아라비아 외국인 등록번호(Iqama) 등을 정규표현식으로 포착(Detect)하여 `[REDACTED]` 치환함으로써 정보보안 요건을 내재화했습니다.

## Phase 3: NLP / Preon 기반 성분명(WHO INN) 표준화
수집된 각각의 제약 브랜드를 국제일반명(WHO INN) 체계와 동기화하기 위해, 다단계 매칭 알고리즘을 도입했습니다. 

* **아랍어 음역 보정(Transliteration Tuning)**: 아랍어 화자가 영문 알파벳으로 약물 성분을 기입할 때 자주 발생하는 음운학적 변환 오류(`amoksisilin` → `amoxicillin`, `bara` → `para` 등)를 추적하여 변형 후보군을 자동 조합해냅니다.
* **n-gram 및 부분 치환 알고리즘 (Preon Library)**: 텍스트에 염(Salt) 성분이나 수화물(Trihydrate 등) 표기가 혼재되어 있을 경우 이를 잘라낸 뒤, 문자열 유사도 분석을 기반으로 하는 **Preon(Precision Oncology Normalizer)** 알고리즘을 거쳐 0.1ms 이내의 속도로 식약청 표준 ATC 코드와 약물 INN 명칭으로 결속(Join)시킵니다.
* **데이터 채움률 벌점(Completeness Penalty)**: 데이터의 완결성이 부족한 행(Record)이나 INN 매칭 적중률에 따라 데이터의 신뢰도값(Confidence Score)을 통계적으로 가감합니다.

## Phase 4: K-통계량 바탕의 가격 이상치 탐지 (Anomaly & Outlier Detection)
입력된 약가 중, 휴먼 에러 등으로 인해 시장가와 현저히 궤를 달리하는 데이터 포인트를 식별합니다. 

* **Relative Range (상대 범위) 기반 모델링**: 최신 학술문헌 *"Empirical Evaluation of the Relative Range for Detecting Outliers"* (Entropy 저널, 2025)에 등장하는 $K$ 통계량 모델 ($K = \frac{\text{Range}}{\text{IQR}}$)을 의약품 가격 탐지 도메인에 적용했습니다.
* **가변적 임계값 (Adaptive Threshold)**: 그룹의 모수(Sample size)에 따라 모델을 전환합니다.
    * $N < 5$ : 극소 표본의 경우 K 통계량이 불안정해지므로 '중앙값(Median) 비례 배수 검정'으로 대체합니다.
    * $N \ge 20$ : K 통계량 알고리즘과 논문에서 도식된 정규 분포 K 임계값 매트릭스를 사용하여 이상치 플래그(`outlier_flagged`)를 마킹, 데이터 신뢰성에 기여합니다.
