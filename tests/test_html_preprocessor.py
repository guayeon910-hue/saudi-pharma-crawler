"""
test_html_preprocessor.py — html_preprocessor.py 단위 테스트

논문1 6단계 파이프라인 검증.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "assets", "snippets"))

from assets.snippets.html_preprocessor import (
    preprocess_html,
    preprocess_for_scraper,
    _parse_html,
    _remove_noise,
    _simplify_dom,
    _extract_blocks,
    _select_snippets,
    _to_prompt,
    TextBlock,
    PreprocessResult,
    STRUCTURE_TOKENS,
)


# ---------------------------------------------------------------------------
# 테스트용 HTML 샘플
# ---------------------------------------------------------------------------

PHARMA_HTML = """
<html>
<head>
    <title>Drug Search Results</title>
    <meta charset="utf-8">
    <link rel="stylesheet" href="/css/main.css">
    <style>.highlight { color: red; }</style>
    <script>console.log('tracking');</script>
</head>
<body>
    <nav>
        <a href="/">Home</a>
        <a href="/search">Search</a>
        <a href="/about">About</a>
    </nav>

    <header>
        <h1>Saudi Pharma Portal</h1>
        <div class="logo"><img src="/logo.png" alt="Logo"></div>
    </header>

    <main>
        <h1>Search Results for Paracetamol</h1>
        <p>Found 15 registered products containing Paracetamol in the SFDA database.
           These pharmaceutical products are approved for distribution in Saudi Arabia.</p>

        <div class="product-list">
            <div class="product-card" data-id="123">
                <h3>Panadol Extra 500mg Tablet</h3>
                <p>Manufacturer: GSK Saudi. Price: SAR 12.50.
                   Active ingredient: Paracetamol 500mg + Caffeine 65mg.
                   Registration number: SFDA-2024-001234.</p>
            </div>
            <div class="product-card" data-id="456">
                <h3>Adol 500mg Capsule</h3>
                <p>Manufacturer: Julphar. Price: SAR 8.75.
                   Active ingredient: Paracetamol 500mg.
                   Registration number: SFDA-2024-005678.</p>
            </div>
        </div>

        <table>
            <tr><th>Product</th><th>Strength</th><th>Price SAR</th></tr>
            <tr><td>Panadol Extra</td><td>500 mg</td><td>12.50</td></tr>
            <tr><td>Adol</td><td>500 mg</td><td>8.75</td></tr>
            <tr><td>Cetal</td><td>500 mg</td><td>6.00</td></tr>
        </table>

        <p>Price data last updated on 2025-03-15. All prices include 15% VAT.</p>
    </main>

    <!-- Hidden promo section -->
    <div style="display: none;">
        <p>Secret promo code: SAVE20</p>
    </div>

    <div aria-hidden="true">
        <p>Screen reader hidden content</p>
    </div>

    <footer>
        <p>Copyright 2025 Saudi Pharma Portal. All rights reserved.</p>
        <nav>
            <a href="/privacy">Privacy</a>
            <a href="/terms">Terms</a>
        </nav>
    </footer>
</body>
</html>
"""

MINIMAL_HTML = "<html><body><p>Short</p></body></html>"

EMPTY_HTML = ""


class TestStage1Parsing(unittest.TestCase):
    def test_parse_valid(self):
        soup = _parse_html(PHARMA_HTML)
        self.assertIsNotNone(soup)
        self.assertTrue(soup.find("body"))

    def test_parse_minimal(self):
        soup = _parse_html(MINIMAL_HTML)
        self.assertEqual(soup.find("p").text, "Short")


class TestStage2NoiseRemoval(unittest.TestCase):
    def test_script_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("script"))

    def test_style_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("style"))

    def test_nav_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("nav"))

    def test_footer_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("footer"))

    def test_hidden_display_none_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        remaining_text = soup.get_text()
        self.assertNotIn("Secret promo", remaining_text)

    def test_aria_hidden_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        remaining_text = soup.get_text()
        self.assertNotIn("Screen reader hidden", remaining_text)

    def test_img_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("img"))

    def test_meta_link_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        self.assertIsNone(soup.find("meta"))
        self.assertIsNone(soup.find("link"))

    def test_comments_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        text = str(soup)
        self.assertNotIn("<!--", text)


class TestStage3DOMSimplify(unittest.TestCase):
    def test_class_id_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        for tag in soup.find_all(True):
            self.assertNotIn("class", tag.attrs)
            self.assertNotIn("id", tag.attrs)

    def test_data_attrs_removed(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        for tag in soup.find_all(True):
            for attr in tag.attrs:
                self.assertFalse(attr.startswith("data-"), f"data attr found: {attr}")

    def test_inline_tags_unwrapped(self):
        html = "<html><body><p>This is <strong>bold</strong> and <em>italic</em> text.</p></body></html>"
        soup = _parse_html(html)
        soup = _simplify_dom(soup)
        self.assertIsNone(soup.find("strong"))
        self.assertIsNone(soup.find("em"))
        # 텍스트는 보존
        self.assertIn("bold", soup.get_text())


class TestStage4BlockExtraction(unittest.TestCase):
    def test_blocks_extracted(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        blocks = _extract_blocks(soup)
        self.assertGreater(len(blocks), 0)

    def test_block_has_text(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        blocks = _extract_blocks(soup)
        for b in blocks:
            self.assertGreater(len(b.text), 0)

    def test_density_calculated(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        blocks = _extract_blocks(soup)
        for b in blocks:
            self.assertGreaterEqual(b.density, 0.0)
            self.assertLessEqual(b.density, 1.0)

    def test_word_count(self):
        soup = _parse_html(PHARMA_HTML)
        soup = _remove_noise(soup)
        soup = _simplify_dom(soup)
        blocks = _extract_blocks(soup)
        content_blocks = [b for b in blocks if b.word_count >= 5]
        self.assertGreater(len(content_blocks), 0)


class TestStage5SnippetSelection(unittest.TestCase):
    def test_selection_filters_short(self):
        blocks = [
            TextBlock(tag="p", text="Too short", total_length=20),
            TextBlock(tag="p", text="This is a longer paragraph with enough words to pass the filter easily", total_length=100),
        ]
        selected = _select_snippets(blocks)
        # 첫 번째 블록은 단어 수 부족으로 제거
        self.assertEqual(len(selected), 1)
        self.assertIn("longer paragraph", selected[0].text)

    def test_top_n_limit(self):
        blocks = [
            TextBlock(tag="p", text=f"Block number {i} has enough words in it to pass the filter", total_length=80)
            for i in range(50)
        ]
        selected = _select_snippets(blocks, top_n=10)
        self.assertLessEqual(len(selected), 10)


class TestStage6PromptConstruction(unittest.TestCase):
    def test_structure_tokens(self):
        blocks = [
            TextBlock(tag="h1", text="Product Search Results page title here nicely", total_length=60),
            TextBlock(tag="p", text="Paracetamol 500mg Tablet is available for SAR 12.50 at all pharmacies", total_length=100),
            TextBlock(tag="td", text="Panadol Extra is a very popular brand in Saudi Arabia market", total_length=80),
        ]
        prompt = _to_prompt(blocks)
        self.assertIn("[TITLE]", prompt)
        self.assertIn("[/TITLE]", prompt)
        self.assertIn("[PARAGRAPH]", prompt)
        self.assertIn("[/PARAGRAPH]", prompt)
        self.assertIn("[CELL]", prompt)
        self.assertIn("[/CELL]", prompt)

    def test_max_chars_limit(self):
        blocks = [
            TextBlock(tag="p", text="x " * 500, total_length=1200)
            for _ in range(20)
        ]
        prompt = _to_prompt(blocks, max_chars=500)
        self.assertLessEqual(len(prompt), 600)  # 약간의 마진


class TestEndToEnd(unittest.TestCase):
    def test_pharma_html_full_pipeline(self):
        result = preprocess_html(PHARMA_HTML)
        self.assertIsInstance(result, PreprocessResult)
        self.assertGreater(result.selected_blocks, 0)
        self.assertGreater(len(result.prompt_text), 0)
        self.assertTrue(result.has_price_pattern)
        self.assertTrue(result.has_product_pattern)
        self.assertTrue(result.has_table)

    def test_prompt_no_noise(self):
        result = preprocess_html(PHARMA_HTML)
        text = result.prompt_text
        # 노이즈 콘텐츠가 없어야 함
        self.assertNotIn("console.log", text)
        self.assertNotIn("Secret promo", text)
        self.assertNotIn("Screen reader hidden", text)
        self.assertNotIn("Copyright", text)

    def test_prompt_has_content(self):
        result = preprocess_html(PHARMA_HTML)
        text = result.prompt_text
        # 핵심 콘텐츠는 있어야 함
        self.assertIn("Paracetamol", text)

    def test_size_reduction(self):
        result = preprocess_html(PHARMA_HTML)
        self.assertLess(result.processed_text_size, result.original_html_size)

    def test_empty_html(self):
        result = preprocess_html(EMPTY_HTML)
        self.assertEqual(result.selected_blocks, 0)
        self.assertEqual(result.prompt_text, "")

    def test_minimal_html(self):
        result = preprocess_html(MINIMAL_HTML)
        self.assertEqual(result.selected_blocks, 0)  # "Short"은 5단어 미만

    def test_preprocess_for_scraper(self):
        clean = preprocess_for_scraper(PHARMA_HTML)
        self.assertIsInstance(clean, str)
        self.assertNotIn("<script>", clean)
        self.assertNotIn("<style>", clean)
        self.assertNotIn("<nav>", clean)

    def test_preprocess_for_scraper_truncation(self):
        clean = preprocess_for_scraper(PHARMA_HTML, max_chars=200)
        self.assertLessEqual(len(clean), 250)  # 200 + "<!-- truncated -->"


class TestArabicContent(unittest.TestCase):
    def test_arabic_pharma_html(self):
        html = """
        <html><body>
            <h1>نتائج البحث عن أدوية</h1>
            <p>تم العثور على 10 منتجات دوائية مسجلة في قاعدة بيانات الهيئة العامة للغذاء والدواء.
               سعر الباراسيتامول 500 ملغ هو 12.50 ر.س. هذه الأدوية معتمدة للتوزيع.</p>
            <table>
                <tr><th>المنتج</th><th>الجرعة</th><th>السعر</th></tr>
                <tr><td>بنادول اكسترا</td><td>500 mg</td><td>SAR 12.50</td></tr>
            </table>
        </body></html>
        """
        result = preprocess_html(html)
        self.assertGreater(result.selected_blocks, 0)
        self.assertTrue(result.has_price_pattern)
        self.assertTrue(result.has_table)


if __name__ == "__main__":
    unittest.main()
