from fastapi.testclient import TestClient

import frontend.server as server
from frontend.buyer_sources import (
    curated_buyer_candidates,
    infer_product_tags,
    registrable_domain_from_url,
)
from frontend.server import (
    _annotate_p3_candidate_scores,
    _is_safe_public_http_url,
    _merge_p3_prospect_lists,
    _verify_p3_prospect_items,
)


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


def test_saudi_second_level_domains_dedupe_by_company_domain():
    assert registrable_domain_from_url("https://one.example.com.sa/a") == "example.com.sa"
    assert registrable_domain_from_url("https://two.al-dawaa.com.sa/b") == "al-dawaa.com.sa"

    merged = _merge_p3_prospect_lists(
        [],
        [
            {"url": "https://a.cigalah.com.sa/about", "title": "Cigalah"},
            {"url": "https://www.al-dawaa.com.sa/en/", "title": "Al-Dawaa"},
        ],
    )

    assert len(merged) == 2
    assert {item["base_domain"] for item in merged} == {"cigalah.com.sa", "al-dawaa.com.sa"}


def test_p3_verifier_drops_obvious_unverified_ai_candidates():
    verified = _verify_p3_prospect_items(
        [{"url": "https://example.com/fake", "title": "Imaginary Buyer"}],
        max_live_checks=0,
    )

    assert verified == []


def test_p3_verifier_keeps_curated_seed_with_verification_metadata():
    curated = curated_buyer_candidates(
        {
            "trade_name": "Agatri",
            "ingredients": "Agastache rugosa extract skin beauty supplement",
            "dosage_form": "Powder",
            "strength": "1 g/day",
        },
        limit=1,
    )

    verified = _verify_p3_prospect_items(curated, max_live_checks=0)

    assert len(verified) == 1
    assert verified[0]["verified"] is False
    assert verified[0]["needs_manual_verification"] is True
    assert verified[0]["verification_status"]
    assert verified[0]["base_domain"]


def test_p3_url_guard_blocks_private_and_local_targets():
    assert _is_safe_public_http_url("http://127.0.0.1:8000/admin") is False
    assert _is_safe_public_http_url("http://localhost:8000/admin") is False
    assert _is_safe_public_http_url("http://169.254.169.254/latest/meta-data") is False
    assert _is_safe_public_http_url("file:///etc/passwd") is False
    assert _is_safe_public_http_url("https://example.com:8443/path") is False
    assert _is_safe_public_http_url("https://example.com/path") is True


def test_agatri_buyer_candidates_surface_consumer_health_fit():
    buyers = curated_buyer_candidates(
        {
            "trade_name": "Agatri",
            "ingredients": "Agastache rugosa extract skin beauty supplement",
            "dosage_form": "Powder",
        },
        limit=10,
    )
    scored = _annotate_p3_candidate_scores(buyers, {
        "trade_name": "Agatri",
        "ingredients": "Agastache rugosa extract skin beauty supplement",
        "dosage_form": "Powder",
    })

    assert any(item.get("enriched", {}).get("has_consumer_health_fit") for item in scored[:8])
    assert any("supplement" in set(item.get("portfolio") or []) for item in scored[:8])


def test_p3_prospects_no_pplx_returns_agatri_curated_pool(monkeypatch):
    def fake_verify(items, max_live_checks=24):
        return [
            {
                **item,
                "verified": True,
                "verification_status": "verified_live_website",
                "candidate_origin": item.get("source") or "test",
            }
            for item in items[:5]
        ]

    monkeypatch.setattr(server, "_get_pplx", lambda: None)
    monkeypatch.setattr(server, "_get_supabase", lambda: None)
    monkeypatch.setattr(server, "_verify_p3_prospect_items", fake_verify)

    with TestClient(server.app) as client:
        response = client.post("/api/p3/prospects", json={"product_key": "agatri"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["count"] > 0
    assert payload["curated_count"] > 0
    assert payload["items"][0]["scores"]


def test_p3_prospects_prioritizes_ai_result_before_curated(monkeypatch):
    class FakePplx:
        def search_pharma_sources(self, drug_info, excluded_domains):
            return [
                {
                    "url": "https://ai-buyer.example.org",
                    "title": "AI Buyer",
                    "category": "distributor",
                    "relevance_score": 0.995,
                }
            ]

    def fake_verify(items, max_live_checks=24):
        return [
            {
                **item,
                "verified": True,
                "verification_status": "verified_live_website",
                "candidate_origin": item.get("source") or "ai_or_database",
            }
            for item in items[:6]
        ]

    monkeypatch.setattr(server, "_get_pplx", lambda: FakePplx())
    monkeypatch.setattr(server, "_get_supabase", lambda: None)
    monkeypatch.setattr(server, "_verify_p3_prospect_items", fake_verify)

    with TestClient(server.app) as client:
        response = client.post("/api/p3/prospects", json={"product_key": "agatri"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["title"] == "AI Buyer"
