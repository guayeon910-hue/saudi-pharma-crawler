from crawlers.sdi_sfda import map_sdi_to_schema


def test_map_sdi_to_schema_marks_sdi_source_and_detail_url():
    item = {
        "drugId": 10358,
        "registerNumber": "1202269142",
        "tradeName": "GADOVIST 1MMOL-ML PREFILLED SYRINGE",
        "scientificName": "GADOBUTROL",
        "strength": "604.72",
        "strengthUnit": "mg/ml",
        "doesageForm": "Solution for injection in pre-filled syringe",
        "price": "386.10",
        "manufacturerName": "Bayer",
        "agent": "Bayer Saudi Arabia",
        "atcCode1": "V08CA09",
    }

    record = map_sdi_to_schema(item)

    assert record["product_id"] == "SDI_1202269142"
    assert record["source_name"] == "sdi_sfda"
    assert record["source_tier"] == 1
    assert record["trade_name"] == "GADOVIST 1MMOL-ML PREFILLED SYRINGE"
    assert record["scientific_name"] == "GADOBUTROL"
    assert record["price_sar"] == 386.10
    assert record["agent_or_supplier"] == "Bayer Saudi Arabia"
    assert record["raw_payload"]["data_source"] == "Saudi Drugs Information System (SDI)"
    assert record["raw_payload"]["sdi_result_url"].endswith("drugId=10358")
