"""
dashboard_sites.py — 크롤링 사이트 메타 정의 + 초기 상태 유틸

GitHub Actions로 실행되는 10개 사우디 소스의 UI 표시 정보를 관리한다.
"""

from __future__ import annotations

SITES: list[dict] = [
    {
        "key": "sfda_api",
        "name": "SFDA API",
        "domain": "sfda.gov.sa",
        "hint": "의약품 등록 DB (PHP API)",
        "category": "공공조달",
    },
    {
        "key": "nupco",
        "name": "NUPCO",
        "domain": "nupco.com",
        "hint": "국영 조달 입찰",
        "category": "공공조달",
    },
    {
        "key": "etimad_api",
        "name": "Etimad",
        "domain": "etimad.sa",
        "hint": "전자조달 플랫폼",
        "category": "공공조달",
    },
    {
        "key": "sfda_companies",
        "name": "SFDA Companies",
        "domain": "sfda.gov.sa",
        "hint": "등록 제약사 목록",
        "category": "공공조달",
    },
    {
        "key": "sfda_drugs",
        "name": "SFDA Drug List",
        "domain": "sfda.gov.sa",
        "hint": "허가 의약품 리스트",
        "category": "공공조달",
    },
    {
        "key": "nahdi_web",
        "name": "Nahdi",
        "domain": "nahdionline.com",
        "hint": "소매 약국 체인",
        "category": "민간",
    },
    {
        "key": "whites_web",
        "name": "Whites",
        "domain": "whites.sa",
        "hint": "소매 약국",
        "category": "민간",
    },
    {
        "key": "al_dawaa_web",
        "name": "Al-Dawaa",
        "domain": "al-dawaa.com",
        "hint": "소매 약국 체인",
        "category": "민간",
    },
    {
        "key": "tamer_group",
        "name": "Tamer Group",
        "domain": "tamergroup.com",
        "hint": "유통/도매",
        "category": "민간",
    },
    {
        "key": "noon_saudi",
        "name": "Noon",
        "domain": "noon.com",
        "hint": "이커머스 플랫폼",
        "category": "민간",
    },
]


def get_initial_states() -> dict[str, dict]:
    """모든 사이트를 pending 상태로 초기화한 dict 반환."""
    return {
        s["key"]: {"status": "pending", "message": "", "ts": ""}
        for s in SITES
    }
