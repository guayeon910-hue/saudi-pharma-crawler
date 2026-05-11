from drug_registry import DrugRegistry, TargetDrug
from targeted_search import _determine_feasibility


def test_agatri_registry_entry_is_available():
    registry = DrugRegistry()

    drug = registry.get_drug("agatri")

    assert drug is not None
    assert drug.trade_name == "Agatri"
    assert drug.drug_type == "Health functional food / inner beauty ingredient"
    assert "Agastache rugosa extract" in drug.ingredient


def test_agatri_keyword_generation_uses_functional_ingredient():
    registry = DrugRegistry()
    drug = registry.get_drug("agatri")

    keywords = registry.generate_search_keywords(drug)

    assert "Agastache rugosa extract powder" in keywords["ingredient_names"]
    assert keywords["trade_name"] == "Agatri"


def test_health_functional_product_without_drug_register_match_is_conditional():
    drug = TargetDrug(
        id="agatri",
        drug_type="Health functional food / inner beauty ingredient",
        trade_name="Agatri",
        ingredient="Agastache rugosa extract powder",
        strength="1 g/day",
        dosage_form="Ingredient powder; tablet, powder, liquid, jelly",
    )
    keywords = {
        "ingredient_names": ["Agastache rugosa extract powder"],
        "dosage_form_normalized": None,
    }

    verdict, rationale, urls = _determine_feasibility(drug, keywords, [])

    assert verdict == "조건부"
    assert "health functional food" in rationale
    assert urls == []
