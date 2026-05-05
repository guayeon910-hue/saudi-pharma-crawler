"""
test_auto_scraper.py — auto_scraper.py 단위 테스트

LLM mock 기반으로 논문2 AUTOSCRAPER 알고리즘 검증.
"""

import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from lxml import html as lxml_html

from assets.snippets.auto_scraper import (
    XPathAction,
    ActionSequence,
    SynthesizedScraper,
    PHARMA_FIELDS,
    _execute_xpath,
    generate_xpath_for_field,
    generate_action_sequence,
    synthesize_scraper,
    run_scraper,
)


# ---------------------------------------------------------------------------
# 테스트용 HTML
# ---------------------------------------------------------------------------

PRODUCT_HTML = """
<html>
<head><title>Saudi Pharmacy - Product Page</title></head>
<body>
<div class="product-listing">
    <div class="product-card">
        <h3 class="product-name">Panadol Extra 500mg Tablet</h3>
        <span class="price">SAR 12.50</span>
        <p class="manufacturer">GSK Saudi Arabia</p>
        <p class="ingredient">Paracetamol</p>
        <p class="strength">500mg</p>
    </div>
    <div class="product-card">
        <h3 class="product-name">Adol 500mg Capsule</h3>
        <span class="price">SAR 8.75</span>
        <p class="manufacturer">Julphar</p>
        <p class="ingredient">Paracetamol</p>
        <p class="strength">500mg</p>
    </div>
    <div class="product-card">
        <h3 class="product-name">Cetal Syrup 120mg/5ml</h3>
        <span class="price">SAR 15.00</span>
        <p class="manufacturer">SPIMACO</p>
        <p class="ingredient">Paracetamol</p>
        <p class="strength">120mg/5ml</p>
    </div>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# XPathAction 테스트
# ---------------------------------------------------------------------------

class TestXPathAction(unittest.TestCase):
    def test_basic_creation(self):
        a = XPathAction(field_name="price", xpath="//span[@class='price']/text()")
        self.assertEqual(a.field_name, "price")
        self.assertFalse(a.verified)
        self.assertEqual(a.attempts, 0)


# ---------------------------------------------------------------------------
# ActionSequence 테스트
# ---------------------------------------------------------------------------

class TestActionSequence(unittest.TestCase):
    def test_usable_with_2_verified(self):
        seq = ActionSequence(url="https://example.com", domain="example.com")
        seq.actions = [
            XPathAction("product_name", "//h3/text()", "Panadol", True),
            XPathAction("price", "//span/text()", "SAR 12", True),
        ]
        self.assertTrue(seq.is_usable)

    def test_not_usable_with_1_verified(self):
        seq = ActionSequence(url="https://example.com", domain="example.com")
        seq.actions = [
            XPathAction("product_name", "//h3/text()", "Panadol", True),
            XPathAction("price", "//span/text()", "", False),
        ]
        self.assertFalse(seq.is_usable)

    def test_to_dict(self):
        seq = ActionSequence(url="https://a.com", domain="a.com", page_title="Test")
        seq.actions = [XPathAction("price", "//x", "10", True)]
        seq.success_rate = 0.5
        d = seq.to_dict()
        self.assertEqual(d["domain"], "a.com")
        self.assertTrue(d["actions"][0]["verified"])


# ---------------------------------------------------------------------------
# XPath 실행 테스트
# ---------------------------------------------------------------------------

class TestXPathExecution(unittest.TestCase):
    def setUp(self):
        self.tree = lxml_html.fromstring(PRODUCT_HTML)

    def test_extract_product_names(self):
        values = _execute_xpath(self.tree, "//h3[@class='product-name']/text()")
        self.assertEqual(len(values), 3)
        self.assertIn("Panadol Extra 500mg Tablet", values)

    def test_extract_prices(self):
        values = _execute_xpath(self.tree, "//span[@class='price']/text()")
        self.assertEqual(len(values), 3)
        self.assertIn("SAR 12.50", values)

    def test_extract_manufacturers(self):
        values = _execute_xpath(self.tree, "//p[@class='manufacturer']/text()")
        self.assertEqual(len(values), 3)
        self.assertIn("GSK Saudi Arabia", values)

    def test_invalid_xpath(self):
        values = _execute_xpath(self.tree, "///invalid[")
        self.assertEqual(values, [])

    def test_no_match(self):
        values = _execute_xpath(self.tree, "//div[@class='nonexistent']/text()")
        self.assertEqual(values, [])


# ---------------------------------------------------------------------------
# XPath 생성 테스트 (LLM mock)
# ---------------------------------------------------------------------------

class TestXPathGeneration(unittest.TestCase):
    def test_top_down_success(self):
        """LLM이 첫 시도에 올바른 XPath 생성."""
        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = {
            "xpath": "//h3[@class='product-name']/text()",
            "expected_value": "Panadol Extra 500mg Tablet",
        }

        tree = lxml_html.fromstring(PRODUCT_HTML)
        field_def = PHARMA_FIELDS[0]  # product_name

        action = generate_xpath_for_field(mock_llm, PRODUCT_HTML, tree, field_def)

        self.assertTrue(action.verified)
        self.assertEqual(action.field_name, "product_name")
        self.assertIn("Panadol", action.sample_value)
        self.assertEqual(action.attempts, 1)

    def test_stepback_on_failure(self):
        """첫 시도 실패 → step-back 성공."""
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = [
            # 1st: 잘못된 XPath
            {"xpath": "//div[@class='wrong']/text()", "expected_value": ""},
            # 2nd (step-back): 올바른 XPath
            {"xpath": "//span[@class='price']/text()", "expected_value": "SAR 12.50"},
        ]

        tree = lxml_html.fromstring(PRODUCT_HTML)
        field_def = PHARMA_FIELDS[1]  # price

        action = generate_xpath_for_field(mock_llm, PRODUCT_HTML, tree, field_def)

        self.assertTrue(action.verified)
        self.assertEqual(action.attempts, 2)
        self.assertIn("SAR", action.sample_value)

    def test_all_attempts_fail(self):
        """모든 시도 실패."""
        mock_llm = MagicMock()
        mock_llm.ask_json.return_value = {
            "xpath": "//div[@class='nonexistent']/text()",
            "expected_value": "",
        }

        tree = lxml_html.fromstring(PRODUCT_HTML)
        field_def = PHARMA_FIELDS[0]

        action = generate_xpath_for_field(mock_llm, PRODUCT_HTML, tree, field_def)

        self.assertFalse(action.verified)
        self.assertGreater(action.attempts, 1)  # step-back 시도됨

    def test_llm_error_handling(self):
        """LLM 호출 실패."""
        mock_llm = MagicMock()
        mock_llm.ask_json.side_effect = Exception("API error")

        tree = lxml_html.fromstring(PRODUCT_HTML)
        field_def = PHARMA_FIELDS[0]

        action = generate_xpath_for_field(mock_llm, PRODUCT_HTML, tree, field_def)

        self.assertFalse(action.verified)


# ---------------------------------------------------------------------------
# Action Sequence 생성 테스트
# ---------------------------------------------------------------------------

class TestActionSequenceGeneration(unittest.TestCase):
    @patch("assets.snippets.html_preprocessor.preprocess_for_scraper")
    def test_generate_sequence(self, mock_preprocess):
        mock_preprocess.return_value = "<html><body>cleaned</body></html>"

        mock_llm = MagicMock()
        # 5개 필드 각각에 대해 XPath 반환
        mock_llm.ask_json.side_effect = [
            {"xpath": "//h3[@class='product-name']/text()", "expected_value": "Panadol"},
            {"xpath": "//span[@class='price']/text()", "expected_value": "SAR 12.50"},
            {"xpath": "//p[@class='manufacturer']/text()", "expected_value": "GSK"},
            {"xpath": "//p[@class='ingredient']/text()", "expected_value": "Paracetamol"},
            {"xpath": "//p[@class='strength']/text()", "expected_value": "500mg"},
        ]

        cache_path = Path(tempfile.mkdtemp()) / "xpath_patterns.json"
        with patch("assets.snippets.auto_scraper._XPATH_CACHE_PATH", cache_path):
            seq = generate_action_sequence(
                mock_llm,
                url="https://pharmacy.sa/drugs",
                html=PRODUCT_HTML,
            )

        self.assertEqual(seq.domain, "pharmacy.sa")
        self.assertEqual(len(seq.actions), 5)
        self.assertTrue(seq.is_usable)
        self.assertGreater(seq.success_rate, 0)


# ---------------------------------------------------------------------------
# Synthesis 테스트
# ---------------------------------------------------------------------------

class TestSynthesis(unittest.TestCase):
    def test_synthesize_from_multiple_pages(self):
        seq1 = ActionSequence(url="https://a.com/page1", domain="a.com")
        seq1.actions = [
            XPathAction("product_name", "//h3/text()", "Drug A", True),
            XPathAction("price", "//span[@class='price']/text()", "SAR 10", True),
        ]

        seq2 = ActionSequence(url="https://a.com/page2", domain="a.com")
        seq2.actions = [
            XPathAction("product_name", "//h3/text()", "Drug B", True),  # 같은 XPath
            XPathAction("price", "//span[@class='price']/text()", "SAR 20", True),  # 같은 XPath
            XPathAction("manufacturer", "//p[@class='mfr']/text()", "Pharma Co", True),
        ]

        scraper = synthesize_scraper([seq1, seq2])

        self.assertIsNotNone(scraper)
        self.assertEqual(scraper.domain, "a.com")
        self.assertIn("product_name", scraper.field_xpaths)
        self.assertIn("price", scraper.field_xpaths)
        self.assertIn("manufacturer", scraper.field_xpaths)
        self.assertEqual(scraper.source_pages, 2)
        self.assertGreater(scraper.confidence, 0)

    def test_synthesize_voting(self):
        """다수결 투표로 XPath 선택."""
        seq1 = ActionSequence(url="https://a.com/1", domain="a.com")
        seq1.actions = [XPathAction("price", "//span[@class='price']/text()", "10", True)]

        seq2 = ActionSequence(url="https://a.com/2", domain="a.com")
        seq2.actions = [XPathAction("price", "//div[@class='cost']/text()", "20", True)]

        seq3 = ActionSequence(url="https://a.com/3", domain="a.com")
        seq3.actions = [XPathAction("price", "//span[@class='price']/text()", "30", True)]

        scraper = synthesize_scraper([seq1, seq2, seq3])

        # span[@class='price'] 가 2표로 선택됨
        self.assertEqual(scraper.field_xpaths["price"], "//span[@class='price']/text()")

    def test_synthesize_empty(self):
        self.assertIsNone(synthesize_scraper([]))

    def test_synthesize_no_verified(self):
        seq = ActionSequence(url="https://a.com", domain="a.com")
        seq.actions = [XPathAction("price", "//x", "", False)]
        self.assertIsNone(synthesize_scraper([seq]))

    def test_to_dict(self):
        s = SynthesizedScraper(
            domain="a.com",
            field_xpaths={"price": "//x"},
            confidence=0.8,
            source_pages=2,
        )
        d = s.to_dict()
        self.assertEqual(d["domain"], "a.com")
        self.assertEqual(d["confidence"], 0.8)


# ---------------------------------------------------------------------------
# 스크래퍼 실행 테스트
# ---------------------------------------------------------------------------

class TestRunScraper(unittest.TestCase):
    def test_extract_data(self):
        scraper = SynthesizedScraper(
            domain="test.com",
            field_xpaths={
                "product_name": "//h3[@class='product-name']/text()",
                "price": "//span[@class='price']/text()",
                "manufacturer": "//p[@class='manufacturer']/text()",
            },
        )

        records = run_scraper(scraper, PRODUCT_HTML)

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["product_name"], "Panadol Extra 500mg Tablet")
        self.assertEqual(records[0]["price"], "SAR 12.50")
        self.assertEqual(records[0]["manufacturer"], "GSK Saudi Arabia")
        self.assertEqual(records[1]["product_name"], "Adol 500mg Capsule")

    def test_extract_empty_html(self):
        scraper = SynthesizedScraper(
            domain="test.com",
            field_xpaths={"product_name": "//h3/text()"},
        )
        records = run_scraper(scraper, "<html><body></body></html>")
        self.assertEqual(records, [])

    def test_source_domain_in_records(self):
        scraper = SynthesizedScraper(
            domain="pharmacy.sa",
            field_xpaths={"product_name": "//h3[@class='product-name']/text()"},
        )
        records = run_scraper(scraper, PRODUCT_HTML)
        for r in records:
            self.assertEqual(r["_source_domain"], "pharmacy.sa")


# ---------------------------------------------------------------------------
# PHARMA_FIELDS 검증 함수 테스트
# ---------------------------------------------------------------------------

class TestFieldValidation(unittest.TestCase):
    def test_product_name_validation(self):
        v = PHARMA_FIELDS[0]["validation"]
        self.assertTrue(v("Panadol Extra"))
        self.assertFalse(v(""))
        self.assertFalse(v("ab"))  # 3자 미만
        self.assertFalse(v("Saudi Pharma Co"))

    def test_price_validation(self):
        v = PHARMA_FIELDS[1]["validation"]
        self.assertTrue(v("SAR 12.50"))
        self.assertTrue(v("8.75"))
        self.assertFalse(v("Drug Product 500mg"))
        self.assertFalse(v("500mg"))
        self.assertFalse(v("free"))
        self.assertFalse(v(""))

    def test_manufacturer_validation(self):
        v = PHARMA_FIELDS[2]["validation"]
        self.assertTrue(v("GSK"))
        self.assertTrue(v("Julphar"))
        self.assertFalse(v("SAR 25.00"))
        self.assertFalse(v(""))
        self.assertFalse(v("A"))  # 2자 미만

    def test_active_ingredient_validation(self):
        v = PHARMA_FIELDS[3]["validation"]
        self.assertTrue(v("Paracetamol"))
        self.assertTrue(v("Omega-3-Acid Ethyl Esters 90"))
        self.assertFalse(v("Saudi Pharma Co"))
        self.assertFalse(v("Drug Product 500mg"))
        self.assertFalse(v("SAR 25.00"))

    def test_strength_validation(self):
        v = PHARMA_FIELDS[4]["validation"]
        self.assertTrue(v("500mg"))
        self.assertTrue(v("120mg/5ml"))
        self.assertTrue(v("250/50"))
        self.assertFalse(v("SAR 25.00"))
        self.assertFalse(v("Drug Product 500mg"))
        self.assertFalse(v("unknown"))


if __name__ == "__main__":
    unittest.main()
