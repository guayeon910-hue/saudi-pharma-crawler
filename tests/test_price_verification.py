from drug_registry import TargetDrug
from frontend import server


def test_collect_price_data_counts_only_source_verified_prices(monkeypatch):
    monkeypatch.setattr(server, "_get_supabase", lambda: None)
    drug = TargetDrug(
        id="sample",
        drug_type="RX",
        trade_name="Sample",
        ingredient="Samplepril",
        strength="10 mg",
        dosage_form="Tablet",
    )
    search_data = {
        "source_results": [
            {
                "source_name": "verified_source",
                "source_url": "https://prices.example.sa/sample",
                "matches": [
                    {
                        "trade_name": "Sample A",
                        "price_sar": 20.0,
                        "match_quality": "ingredient",
                    }
                ],
            },
            {
                "source_name": "unverified_source",
                "source_url": "",
                "matches": [
                    {
                        "trade_name": "Sample B",
                        "price_sar": 99.0,
                        "match_quality": "ingredient",
                    }
                ],
            },
        ]
    }

    data = server._collect_price_data(drug, search_data)

    assert data["summary"]["raw_count"] == 2
    assert data["summary"]["verified_count"] == 1
    assert data["summary"]["unverified_count"] == 1
    assert data["summary"]["avg"] == 20.0
    assert data["same_ingredient"][0]["is_verified_price"] is True
    assert data["same_ingredient"][1]["is_verified_price"] is False


def test_dashboard_record_marks_price_without_source_url_unverified():
    shaped = server._shape_record_for_dashboard(
        {
            "trade_name": "Snapshot Price",
            "price_sar": 12.5,
            "source_name": "snapshot",
            "source_url": "",
        }
    )

    assert shaped["is_verified_price"] is False
    assert shaped["verification_status"] == "observed_missing_source_url"
