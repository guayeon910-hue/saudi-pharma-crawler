from frontend.buyer_sources import curated_buyer_candidates, infer_product_tags
from frontend.server import _merge_p3_prospect_lists


def test_curated_buyer_candidates_provide_practical_outreach_pool():
    drug_info = {
        "trade_name": "Sereterol",
        "ingredients": "Salmeterol / Fluticasone",
        "dosage_form": "Inhalation powder",
        "strength": "50/250 mcg",
    }

    buyers = curated_buyer_candidates(drug_info)

    assert len(buyers) >= 20
    assert all(item["url"].startswith("http") for item in buyers)
    assert all(item.get("company") for item in buyers)
    assert all(item.get("website") for item in buyers)
    assert any(item["category"] == "distributor" for item in buyers)
    assert any(item["category"] == "pharmacy_chain" for item in buyers)


def test_curated_buyer_candidates_rank_product_fit():
    oncology_info = {
        "trade_name": "Oncology Injection",
        "ingredients": "Docetaxel",
        "dosage_form": "Injection vial",
        "strength": "20 mg",
    }

    tags = infer_product_tags(oncology_info)
    buyers = curated_buyer_candidates(oncology_info)
    top_titles = {item["title"] for item in buyers[:10]}

    assert {"oncology", "hospital"} <= tags
    assert "Sudair Pharma" in top_titles
    assert "NUPCO" in top_titles


def test_p3_merge_supplements_single_ai_buyer_with_curated_pool():
    ai_items = [
        {
            "url": "https://example-distributor.sa",
            "title": "Example Distributor",
            "category": "distributor",
            "relevance_score": 0.9,
        }
    ]
    curated = curated_buyer_candidates(
        {
            "trade_name": "Gadvoa",
            "ingredients": "Gadobutrol",
            "dosage_form": "Injection",
            "strength": "1 mmol/mL",
        }
    )

    merged = _merge_p3_prospect_lists([], ai_items + curated)

    assert len(merged) > 10
    assert merged[0]["company"] == "Example Distributor"
    assert merged[0]["website"] == "https://example-distributor.sa"
    assert len({item["domain"] for item in merged}) == len(merged)
