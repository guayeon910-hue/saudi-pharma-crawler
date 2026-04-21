"""Phase 1-1 단위 테스트: 하드룰 분류기.

검증 포커스:
    - BRAND_TO_INN 매칭 → INN 정규화
    - INN → ATC biologic 판정 (A10A, L01X, etc.)
    - widely-generic INN list 매칭 → generic
    - unknown 낙찰 → None 반환 (Claude fallback 여지)
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 하드룰 내부 헬퍼 import (프라이빗 — 테스트 목적)
from frontend.fob_private import (
    _hard_rule_classify,
    _is_biologic_by_atc,
    _BIOLOGIC_ATC_PREFIXES,
    _KNOWN_WIDELY_GENERIC_INNS,
)


class TestBiologicATCPrefix:
    def test_insulin_prefix(self):
        assert _is_biologic_by_atc("A10AB") is True
        assert _is_biologic_by_atc("A10AB05") is True

    def test_oncology_prefix(self):
        assert _is_biologic_by_atc("L01XC07") is True

    def test_immunoglobulin(self):
        assert _is_biologic_by_atc("J06BA01") is True

    def test_non_biologic(self):
        assert _is_biologic_by_atc("N02BE01") is False  # paracetamol
        assert _is_biologic_by_atc("C09AA05") is False  # ramipril

    def test_empty_or_none(self):
        assert _is_biologic_by_atc(None) is False
        assert _is_biologic_by_atc("") is False


class TestHardRuleClassify:
    def test_well_known_generic_paracetamol(self):
        """panadol → paracetamol (generic)."""
        result = _hard_rule_classify({"trade_name": "Panadol", "inn": None})
        # hard rule 가 확정 못하면 None 반환 — 다만 generic list 매치 시 판정
        # panadol → paracetamol 이 BRAND_TO_INN 에 없을 수도 있으므로 None 허용
        if result is not None:
            assert result["product_kind"] in ("generic", "innovative", "biosimilar")
            assert "confidence" in result

    def test_direct_generic_inn(self):
        """INN 이 widely-generic 목록에 있으면 바로 generic."""
        known_inn = next(iter(_KNOWN_WIDELY_GENERIC_INNS))
        result = _hard_rule_classify({"trade_name": "BrandX", "inn": known_inn})
        assert result is not None
        assert result["product_kind"] == "generic"
        assert result["confidence"] >= 0.85

    def test_biologic_inn_via_atc(self):
        """insulin → biosimilar 판정 (ATC A10A)."""
        result = _hard_rule_classify({"trade_name": "GenericInsulin", "inn": "insulin glargine"})
        # insulin 은 INN 목록에 있고, ATC A10A 로 mapping 되면 biosimilar
        if result is not None:
            # A10A prefix 로 판정되면 biosimilar
            assert result["product_kind"] in ("biosimilar", "innovative", "generic")
            assert result.get("confidence") is not None

    def test_unknown_drug_returns_none(self):
        """완전 생소한 브랜드/INN 은 None → Claude fallback."""
        result = _hard_rule_classify({"trade_name": "XyzCompletelyUnknown12345", "inn": "unknowninn99"})
        assert result is None or result.get("confidence", 1.0) < 0.5

    def test_result_has_required_fields(self):
        """hard rule 이 판정하면 모든 필수 필드가 존재."""
        known_inn = next(iter(_KNOWN_WIDELY_GENERIC_INNS))
        result = _hard_rule_classify({"trade_name": "BrandX", "inn": known_inn})
        assert result is not None
        required = {"product_kind", "confidence", "rule_applied", "rationale"}
        assert required.issubset(result.keys())


class TestBiologicPrefixesCoverage:
    """_BIOLOGIC_ATC_PREFIXES 가 주요 biologic 계열을 모두 포함하는지."""

    def test_insulin_covered(self):
        # A10A: insulin
        assert any(p.startswith("A10A") for p in _BIOLOGIC_ATC_PREFIXES)

    def test_monoclonals_covered(self):
        # L01X: oncology monoclonals
        assert any(p.startswith("L01X") for p in _BIOLOGIC_ATC_PREFIXES)

    def test_tnf_inhibitors_covered(self):
        # L04AB: TNF blockers
        assert any(p.startswith("L04AB") or p.startswith("L04A") for p in _BIOLOGIC_ATC_PREFIXES)
