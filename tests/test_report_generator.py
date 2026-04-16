"""Smoke test for DOCX report generation."""

import tempfile
import unittest
from pathlib import Path

import report_generator as rg


class TestGenerateReportSmoke(unittest.TestCase):
    def test_minimal_docx(self):
        tmp = Path(tempfile.mkdtemp())
        prev = rg.REPORTS_DIR
        rg.REPORTS_DIR = tmp
        try:
            drug = {
                "trade_name": "TestDrug",
                "ingredient": "INN",
                "strength": "10mg",
                "dosage_form": "Tablet",
                "drug_type": "RX",
                "id": "smoke_test",
            }
            sd = {
                "total_matches": 0,
                "source_results": [],
                "export_feasibility": "조건부",
                "feasibility_rationale": "스모크 테스트",
            }
            analysis = {
                "verdict": "조건부",
                "confidence": 0.5,
                "rationale": "스모크",
                "key_factors": [],
                "hs_code": "HS 3004",
                "case_type": "Case B",
                "pillars": {},
                "strategy": {},
            }
            path = rg.generate_report(
                drug,
                sd,
                analysis=analysis,
                refs=[{"title": "Example", "url": "https://example.com/a"}],
                report_meta={"collection_finished_at": "2026-01-01"},
            )
            self.assertTrue(path.exists())
            self.assertEqual(path.suffix, ".docx")
        finally:
            rg.REPORTS_DIR = prev


if __name__ == "__main__":
    unittest.main()
