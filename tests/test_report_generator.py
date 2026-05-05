"""Smoke test for DOCX report generation."""

import tempfile
import unittest
from pathlib import Path

from docx import Document

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

    def test_docx_uses_price_local_in_evidence_tables(self):
        tmp = Path(tempfile.mkdtemp())
        prev = rg.REPORTS_DIR
        rg.REPORTS_DIR = tmp
        try:
            drug = {
                "trade_name": "DbDrug",
                "ingredient": "Paracetamol",
                "strength": "500mg",
                "dosage_form": "Tablet",
                "drug_type": "RX",
                "id": "db_drug",
            }
            sd = {
                "total_matches": 1,
                "source_results": [
                    {
                        "source_name": "sfda_api",
                        "source_category": "공공조달",
                        "source_url": "https://sfda.example/drug",
                        "matches": [
                            {
                                "trade_name": "DbDrug",
                                "scientific_name": "Paracetamol",
                                "strength": "500 mg",
                                "price_local": 12.5,
                                "confidence": 0.91,
                            }
                        ],
                    }
                ],
                "export_feasibility": "적합",
                "feasibility_rationale": "동일 성분 가격 확인",
            }
            analysis = {
                "verdict": "적합",
                "confidence": 0.91,
                "rationale": "동일 성분 가격 확인",
                "key_factors": [],
                "hs_code": "HS 3004",
                "case_type": "Case A",
                "pillars": {},
                "strategy": {},
            }
            path = rg.generate_report(
                drug,
                sd,
                analysis=analysis,
                report_meta={"collection_finished_at": "2026-01-01"},
            )
            doc = Document(str(path))
            text = "\n".join(
                cell.text
                for table in doc.tables
                for row in table.rows
                for cell in row.cells
            )
            self.assertIn("DbDrug", text)
            self.assertIn("12.5", text)
            self.assertIn("$3.33", text)
        finally:
            rg.REPORTS_DIR = prev


if __name__ == "__main__":
    unittest.main()
