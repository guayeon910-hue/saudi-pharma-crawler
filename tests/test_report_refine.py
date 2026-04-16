"""Unit tests for report_refine."""

import unittest

from report_refine import (
    aggregate_evidence_by_source,
    count_procurement_vs_retail,
    refine_cell_text,
)


class TestRefineCellText(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(refine_cell_text("  hello  "), "hello")

    def test_json_notes_plain(self):
        raw = '{"item_collected_at_fallback":true,"notes_plain":"성인 유병률 약 35%"}'
        out = refine_cell_text(raw)
        self.assertIn("35%", out)
        self.assertNotIn("item_collected_at_fallback", out)

    def test_inline_notes_plain(self):
        raw = 'prefix {"notes_plain": "한글 설명"} suffix'
        out = refine_cell_text(raw)
        self.assertIn("한글", out)


class TestAggregate(unittest.TestCase):
    def test_counts_and_confidence(self):
        src = [
            {
                "source_name": "sfda_web",
                "source_category": "공공조달",
                "matches": [
                    {"confidence": 0.9},
                    {"confidence": 0.8},
                ],
            },
            {
                "source_name": "nahdi",
                "source_category": "민간",
                "matches": [{}],
            },
        ]
        rows = aggregate_evidence_by_source(src)
        self.assertEqual(len(rows), 2)
        by = {r["source"]: r for r in rows}
        self.assertEqual(by["sfda_web"]["count"], 2)
        self.assertEqual(by["sfda_web"]["avg_confidence"], 0.85)

    def test_procurement_retail(self):
        pub, priv = count_procurement_vs_retail(
            [
                {"source_category": "공공조달", "matches": [{}, {}]},
                {"source_category": "민간", "matches": [{}]},
            ]
        )
        self.assertEqual(pub, 2)
        self.assertEqual(priv, 1)


if __name__ == "__main__":
    unittest.main()
