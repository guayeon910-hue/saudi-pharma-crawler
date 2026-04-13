"""
Trafilatura 시뮬레이션 — 사우디 약국 크롤러 10개 소스 대상 테스트
목적: 각 소스별로 Trafilatura가 유의미한 본문을 추출할 수 있는지 검증
결과를 SUCCESS / PARTIAL / FAIL 로 분류
"""

import json
import time
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx
import trafilatura
from trafilatura.settings import use_config

# ─── 설정 ──────────────────────────────────────────
TIMEOUT = 15.0
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Trafilatura 설정 (공격적 추출 모드)
traf_config = use_config()
traf_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "100")      # 최소 출력 100자
traf_config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "50")     # 최소 추출 50자


# ─── 테스트 대상 URL ──────────────────────────────────
TARGETS = [
    # Tier 1: 규제/공식
    {
        "name": "sfda_drugs_list_html",
        "url": "https://www.sfda.gov.sa/en/drugs-list",
        "tier": 1,
        "expect": "drug names, registration numbers",
        "access": "html_static",
    },
    {
        "name": "sfda_companies",
        "url": "https://www.sfda.gov.sa/en/drug-companies",
        "tier": 1,
        "expect": "company names, agent info",
        "access": "html_static",
    },
    # Tier 2: 공공조달
    {
        "name": "nupco_tenders",
        "url": "https://www.nupco.com/en/tenders/",
        "tier": 2,
        "expect": "tender titles, dates, categories",
        "access": "html_static",
    },
    {
        "name": "etimad_portal",
        "url": "https://www.mof.gov.sa/en/eservices/Pages/Etimad.aspx",
        "tier": 2,
        "expect": "service descriptions, portal info",
        "access": "html_static",
    },
    # Tier 3: 소매 약국
    {
        "name": "nahdi_web",
        "url": "https://www.nahdionline.com/en-sa",
        "tier": 3,
        "expect": "product names, prices in SAR",
        "access": "html_static",
    },
    {
        "name": "nahdi_medicine_category",
        "url": "https://www.nahdionline.com/en-sa/c/medicine/",
        "tier": 3,
        "expect": "medicine product listings, prices",
        "access": "html_static",
    },
    {
        "name": "al_dawaa_web",
        "url": "https://www.al-dawaa.com/en/",
        "tier": 3,
        "expect": "product names, prices in SAR",
        "access": "html_static",
    },
    {
        "name": "whites_web",
        "url": "https://www.whites.sa/en-sa",
        "tier": 3,
        "expect": "product names, prices",
        "access": "html_static",
    },
    # Tier 4: 도매/유통
    {
        "name": "tamer_group",
        "url": "https://tamergroup.com/sectors/distribution-healthcare-fmcg",
        "tier": 4,
        "expect": "brand portfolio, distribution info",
        "access": "html_static",
    },
    # Tier 5: 대형 상거래
    {
        "name": "noon_saudi",
        "url": "https://www.noon.com/saudi-en/health/main-pharmacy-sa/",
        "tier": 5,
        "expect": "product names, prices",
        "access": "html_dynamic",
    },
]


@dataclass
class SimResult:
    name: str
    tier: int
    url: str
    access: str
    expect: str
    # fetch results
    http_status: Optional[int] = None
    fetch_error: Optional[str] = None
    html_length: int = 0
    # trafilatura results
    extracted_text: Optional[str] = None
    extracted_length: int = 0
    metadata: Optional[dict] = None
    # analysis
    verdict: str = "FAIL"           # SUCCESS / PARTIAL / FAIL
    fail_reason: Optional[str] = None
    has_price_data: bool = False
    has_product_names: bool = False
    has_arabic: bool = False
    has_structured_data: bool = False  # JSON-LD, __NEXT_DATA__ 등
    elapsed_sec: float = 0.0


def detect_structured_data(html: str) -> bool:
    """HTML 내 구조화 데이터 존재 여부"""
    markers = [
        '"@type"',                    # JSON-LD
        "__NEXT_DATA__",              # Next.js
        "__INITIAL_STATE__",          # Redux SSR
        "__NUXT__",                   # Nuxt.js
        "application/ld+json",        # Schema.org
        "window.__PRELOADED_STATE__", # React SSR
    ]
    html_lower = html.lower() if html else ""
    return any(m.lower() in html_lower for m in markers)


def detect_prices(text: str) -> bool:
    """SAR 가격 패턴 존재 여부"""
    import re
    patterns = [
        r'SAR\s*\d+',
        r'\d+\.?\d*\s*SAR',
        r'SR\s*\d+',
        r'\d+\.?\d*\s*SR',
        r'ر\.س',                     # 아랍어 SAR
        r'price',
        r'Price',
    ]
    return any(re.search(p, text) for p in patterns)


def detect_product_names(text: str) -> bool:
    """의약품/제품명 패턴 존재 여부"""
    import re
    pharma_keywords = [
        r'(?i)tablet', r'(?i)capsule', r'(?i)syrup', r'(?i)cream',
        r'(?i)mg\b', r'(?i)ml\b', r'(?i)paracetamol', r'(?i)ibuprofen',
        r'(?i)medicine', r'(?i)pharmaceutical', r'(?i)drug',
        r'(?i)vitamin', r'(?i)supplement',
    ]
    matches = sum(1 for p in pharma_keywords if re.search(p, text))
    return matches >= 2


def detect_arabic(text: str) -> bool:
    """아랍어 문자 포함 여부"""
    return any('\u0600' <= c <= '\u06FF' for c in (text or ""))


def run_simulation(target: dict) -> SimResult:
    """단일 소스에 대한 시뮬레이션 실행"""
    result = SimResult(
        name=target["name"],
        tier=target["tier"],
        url=target["url"],
        access=target["access"],
        expect=target["expect"],
    )

    start = time.time()

    # ── Step 1: HTTP fetch ──
    try:
        with httpx.Client(
            headers={"User-Agent": UA},
            follow_redirects=True,
            timeout=TIMEOUT,
        ) as client:
            resp = client.get(target["url"])
            result.http_status = resp.status_code
            html = resp.text
            result.html_length = len(html)
    except httpx.TimeoutException:
        result.fetch_error = "TIMEOUT"
        result.fail_reason = "HTTP request timed out"
        result.elapsed_sec = time.time() - start
        return result
    except Exception as e:
        result.fetch_error = str(e)[:200]
        result.fail_reason = f"HTTP fetch failed: {type(e).__name__}"
        result.elapsed_sec = time.time() - start
        return result

    # ── Step 2: 기본 HTML 분석 ──
    if result.http_status != 200:
        result.fail_reason = f"HTTP {result.http_status}"
        result.elapsed_sec = time.time() - start
        return result

    result.has_structured_data = detect_structured_data(html)

    # ── Step 3: Trafilatura 추출 ──
    try:
        extracted = trafilatura.extract(
            html,
            config=traf_config,
            include_links=True,
            include_tables=True,
            include_comments=False,
            favor_recall=True,          # 재현율 우선 (놓치는 것보다 많이 잡는 게 나음)
            output_format="txt",
        )
    except Exception as e:
        result.fail_reason = f"Trafilatura error: {type(e).__name__}: {str(e)[:100]}"
        result.elapsed_sec = time.time() - start
        return result

    if extracted:
        result.extracted_text = extracted
        result.extracted_length = len(extracted)
        result.has_price_data = detect_prices(extracted)
        result.has_product_names = detect_product_names(extracted)
        result.has_arabic = detect_arabic(extracted)

    # ── Step 4: 메타데이터 추출 ──
    try:
        meta = trafilatura.extract(
            html,
            config=traf_config,
            output_format="json",
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
        if meta:
            import json as _json
            result.metadata = _json.loads(meta) if isinstance(meta, str) else meta
    except Exception:
        pass  # 메타데이터 실패는 무시

    # ── Step 5: 판정 ──
    if not extracted or result.extracted_length < 50:
        result.verdict = "FAIL"
        result.fail_reason = result.fail_reason or "No meaningful text extracted"
    elif result.extracted_length < 200:
        result.verdict = "PARTIAL"
        result.fail_reason = "Extracted text too short for reliable analysis"
    elif result.has_price_data or result.has_product_names:
        result.verdict = "SUCCESS"
    elif result.extracted_length >= 500:
        result.verdict = "SUCCESS"
    else:
        result.verdict = "PARTIAL"
        result.fail_reason = "Text extracted but no pharma-specific data found"

    result.elapsed_sec = time.time() - start
    return result


def print_result(r: SimResult, idx: int):
    """단일 결과 콘솔 출력"""
    icon = {"SUCCESS": "✅", "PARTIAL": "⚠️", "FAIL": "❌"}[r.verdict]
    print(f"\n{'='*70}")
    print(f"[{idx+1:02d}] {icon} {r.verdict} — {r.name} (Tier {r.tier})")
    print(f"    URL: {r.url}")
    print(f"    HTTP: {r.http_status or r.fetch_error} | HTML: {r.html_length:,} chars | Time: {r.elapsed_sec:.1f}s")
    print(f"    Extracted: {r.extracted_length:,} chars")
    print(f"    Structured Data: {r.has_structured_data} | Prices: {r.has_price_data} | Products: {r.has_product_names} | Arabic: {r.has_arabic}")
    if r.fail_reason:
        print(f"    Fail Reason: {r.fail_reason}")
    if r.extracted_text:
        preview = r.extracted_text[:300].replace('\n', ' ')
        print(f"    Preview: {preview}...")


def main():
    print("=" * 70)
    print("  TRAFILATURA SIMULATION — Saudi Pharma Crawler Sources")
    print(f"  trafilatura v{trafilatura.__version__}")
    print(f"  Targets: {len(TARGETS)} URLs")
    print("=" * 70)

    results: list[SimResult] = []

    for i, target in enumerate(TARGETS):
        print(f"\n>>> [{i+1}/{len(TARGETS)}] Fetching {target['name']}...", flush=True)
        r = run_simulation(target)
        results.append(r)
        print_result(r, i)
        # 예의 바른 간격
        if i < len(TARGETS) - 1:
            time.sleep(2)

    # ── 요약 ──
    successes = [r for r in results if r.verdict == "SUCCESS"]
    partials  = [r for r in results if r.verdict == "PARTIAL"]
    fails     = [r for r in results if r.verdict == "FAIL"]

    print("\n" + "=" * 70)
    print("  SIMULATION SUMMARY")
    print("=" * 70)
    print(f"  ✅ SUCCESS : {len(successes)}/{len(results)}")
    print(f"  ⚠️  PARTIAL : {len(partials)}/{len(results)}")
    print(f"  ❌ FAIL    : {len(fails)}/{len(results)}")

    if successes:
        print(f"\n  ── SUCCESS Cases ──")
        for r in successes:
            print(f"    • {r.name} (Tier {r.tier}) — {r.extracted_length:,} chars, prices={r.has_price_data}, products={r.has_product_names}")

    if partials:
        print(f"\n  ── PARTIAL Cases ──")
        for r in partials:
            print(f"    • {r.name} (Tier {r.tier}) — {r.extracted_length:,} chars | {r.fail_reason}")

    if fails:
        print(f"\n  ── FAIL Cases ──")
        for r in fails:
            print(f"    • {r.name} (Tier {r.tier}) — {r.fail_reason}")

    # JSON 출력 (상세 분석용)
    json_results = []
    for r in results:
        d = asdict(r)
        # extracted_text는 너무 길어서 잘라냄
        if d.get("extracted_text"):
            d["extracted_text_preview"] = d["extracted_text"][:500]
            del d["extracted_text"]
        json_results.append(d)

    output_path = "tests/trafilatura_simulation_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(json_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Full results saved to: {output_path}")


if __name__ == "__main__":
    main()
