import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "snippets"))

from drug_registry import TargetDrug
from normalizer import normalize_dosage_form
from targeted_search import (
    SearchResult,
    _annotate_match,
    _determine_feasibility,
    _filter_relevant_matches,
)
from crawlers.rosheta_web import _parse_product_page


def test_retail_filter_keeps_only_same_active_ingredient():
    target = ["Hydroxyurea"]

    cureaml = {
        "name": "Cureaml 500 mg Hydroxyurea Capsules 30 Count",
        "ingredient": "HYDROXYUREA",
        "price": 52.55,
    }
    allopurinol = {
        "name": "No-Uric 100 mg Tablet 50pcs",
        "ingredient": "Allopurinol",
        "price": 16.3,
    }

    assert _annotate_match(cureaml, target)["match_quality"] == "ingredient"
    assert _annotate_match(allopurinol, target) is None


def test_omega_filter_drops_generic_fish_oil_when_ethyl_ester_needed():
    records = [
        {
            "trade_name": "OMEGANA 1000MG SOFT CAPSULE",
            "scientific_name": "OMEGA-3-ACID ETHYL ESTERS",
            "price_sar": 48.95,
        },
        {
            "trade_name": "JP Omega 3 Fish Oil 567 mg 30 Capsules",
            "scientific_name": "OMEGA-3 POLYUNSATURATED FATTY ACIDS",
            "price_sar": 69.0,
        },
    ]

    filtered = _filter_relevant_matches(records, ["Omega-3-Acid Ethyl Esters 90"])

    assert len(filtered) == 1
    assert filtered[0]["trade_name"] == "OMEGANA 1000MG SOFT CAPSULE"


def test_combo_requires_all_ingredients_for_possible_verdict():
    drug = TargetDrug(
        id="ciloduo",
        drug_type="개량신약",
        trade_name="Ciloduo",
        ingredient="Cilostazol 200mg + Rosuvastatin 10mg",
        strength="200/10mg",
        dosage_form="Tab.",
    )
    keywords = {
        "ingredient_names": ["Cilostazol", "Rosuvastatin"],
        "dosage_form_normalized": "tablet",
    }
    sfda = SearchResult(
        source_name="sfda_api",
        source_category="공공조달",
        source_url="https://www.sfda.gov.sa/en/drugs-list",
        matches=[
            {
                "trade_name": "FANCATA 100MG TABLET",
                "scientific_name": "CILOSTAZOL",
                "dosage_form": "Tablet",
                "regulatory_id": "1",
                "source_url": "https://www.sfda.gov.sa/en/drugs-list",
            },
            {
                "trade_name": "CRESTOR 10 MG FILM COATED TABLETS",
                "scientific_name": "ROSUVASTATIN",
                "dosage_form": "Film-coated tablet",
                "regulatory_id": "2",
                "source_url": "https://www.sfda.gov.sa/en/drugs-list",
            },
        ],
    )

    verdict, rationale, urls = _determine_feasibility(drug, keywords, [sfda])

    assert verdict == "조건부"
    assert "성분 조합" in rationale
    assert urls == ["https://www.sfda.gov.sa/en/drugs-list"]


def test_sfda_inhalation_forms_normalize_to_inhaler():
    assert normalize_dosage_form("Pressurised inhalation, suspension") == "inhaler"


def test_rosheta_product_page_parser_extracts_price_and_form():
    html = """
    <html><body>
      <h1>Mosapride tablets 5 mg</h1>
      <div>50.88 SAR | SAR scientific Name</div>
      <div>Dosage 5 mg</div>
      <div>Type Tablets</div>
    </body></html>
    """

    product = _parse_product_page(html, "https://www.rosheta.com/en/15405/mosapride")

    assert product["name"] == "Mosapride tablets 5 mg"
    assert product["price_sar"] == 50.88
    assert product["strength"] == "5 mg"
    assert product["dosage_form"] == "Tablets"
