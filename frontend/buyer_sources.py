"""Curated Saudi buyer and distributor sources for P3 prospect discovery.

The AI search occasionally returns only one highly specific prospect.  P3 is
most useful when it still gives the sales team a practical outreach list, so
this module supplies a conservative, verified seed set of Saudi healthcare
buyers that can be ranked against each product.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


SAUDI_BUYER_SEEDS: list[dict[str, Any]] = [
    {
        "title": "Tamer Group - Healthcare Distribution",
        "url": "https://tamergroup.com/sectors/distribution-healthcare-fmcg",
        "category": "distributor",
        "description": "Saudi healthcare group supplying pharmaceutical, OTC, generic, medical equipment, and hospital products across public and private channels.",
        "focus": ["general", "hospital", "retail", "respiratory", "cardiovascular", "gastrointestinal"],
        "base_score": 0.93,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Tamer Healthcare",
        "url": "https://hc.tamergroup.com/about-us",
        "category": "distributor",
        "description": "Trading company under Tamer with broad Saudi market reach for medical and pharmaceutical products and services.",
        "focus": ["general", "hospital", "retail"],
        "base_score": 0.91,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Cigalah Healthcare",
        "url": "https://order.cigalah.com.sa/about-us",
        "category": "distributor",
        "description": "Saudi importer, warehousing, manufacturing, and distribution group with pharmaceutical healthcare distribution hubs.",
        "focus": ["general", "hospital", "retail", "cardiovascular", "respiratory"],
        "base_score": 0.92,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Cigalah Group Healthcare Division",
        "url": "https://naghi-group.com/cigalah-group/cigalah-healthcare-sector/",
        "category": "distributor",
        "description": "Healthcare division described as a leading pharmaceutical distribution company in Saudi Arabia with regulatory affairs capability.",
        "focus": ["general", "hospital", "retail"],
        "base_score": 0.91,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Salehiya Healthcare",
        "url": "https://salehiya.com/",
        "category": "distributor",
        "description": "Saudi healthcare organization with GDP warehousing and transportation certification for pharmaceutical products.",
        "focus": ["general", "hospital", "imaging", "oncology"],
        "base_score": 0.90,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Saudi Arabian Drug Store Company (SADSCO)",
        "url": "https://sadsco.com/",
        "category": "distributor",
        "description": "Saudi distributor supplying pharmaceutical, nutritional, cosmetics, medical device, FMCG, hospital tender, retail, and pharmacy channels.",
        "focus": ["general", "hospital", "retail", "cardiovascular", "respiratory", "gastrointestinal"],
        "base_score": 0.90,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Ideal Choice Trading",
        "url": "https://idealchoice.sa/",
        "category": "importer",
        "description": "SFDA-authorized importer and distributor that works with major Saudi pharmacy chains and government healthcare tenders.",
        "focus": ["general", "retail", "pharmacy_chain"],
        "base_score": 0.88,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "NUPCO",
        "url": "https://www.nupco.com/en/about-nupco/",
        "category": "government_procurement",
        "description": "Saudi centralized healthcare procurement, warehousing, and distribution company for pharmaceuticals, medical devices, and supplies.",
        "focus": ["government", "hospital", "oncology", "imaging", "respiratory", "cardiovascular"],
        "base_score": 0.89,
        "has_product_listing": False,
        "language": "en",
    },
    {
        "title": "Nahdi Medical Company",
        "url": "https://nahdi.sa/about-nahdi/our-history/",
        "category": "pharmacy_chain",
        "description": "Large Saudi pharmacy-led retailer with nationwide pharmacy and healthcare service reach.",
        "focus": ["retail", "pharmacy_chain", "general", "cardiovascular", "respiratory", "gastrointestinal"],
        "base_score": 0.86,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Al-Dawaa Pharmacies (DMSCO)",
        "url": "https://www.al-dawaa.com.sa/en/Al-Dawaa/departments/pharmacies/",
        "category": "pharmacy_chain",
        "description": "Saudi pharmacy chain with more than 900 branches and dedicated purchasing, logistics, and medical equipment departments.",
        "focus": ["retail", "pharmacy_chain", "general", "cardiovascular", "respiratory", "gastrointestinal"],
        "base_score": 0.86,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Pharma Al-Dawaa",
        "url": "https://pharmaaldawaa.com/",
        "category": "distributor",
        "description": "Saudi partner for domestic and global pharmaceutical companies with nationwide presence and specialist network.",
        "focus": ["general", "retail", "hospital"],
        "base_score": 0.84,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Whites Pharmacy",
        "url": "https://store.whites.net/en/page/about-whites",
        "category": "pharmacy_chain",
        "description": "Saudi retail pharmacy and lifestyle chain that can be screened for consumer and retail pharmaceutical placement.",
        "focus": ["retail", "pharmacy_chain", "general"],
        "base_score": 0.82,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "SPIMACO ADDWAEIH",
        "url": "https://www.spimaco.com/",
        "category": "other",
        "description": "Saudi pharmaceutical manufacturer with product development, manufacturing capacity, and partnership opportunity signals.",
        "focus": ["general", "cardiovascular", "respiratory", "gastrointestinal", "hospital"],
        "base_score": 0.82,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Tabuk Pharmaceuticals",
        "url": "https://www.tabukpharmaceuticals.com/",
        "category": "other",
        "description": "Large Saudi pharmaceutical company with MENA presence, broad therapeutic portfolio, and in-licensing partnership activity.",
        "focus": ["general", "respiratory", "cardiovascular", "gastrointestinal", "hospital", "oncology"],
        "base_score": 0.83,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Jamjoom Pharma",
        "url": "https://www.jamjoompharma.com/about-us/",
        "category": "other",
        "description": "Saudi pharmaceutical company with local manufacturing, consumer health, ophthalmology, cardiometabolic, and regional portfolio reach.",
        "focus": ["general", "cardiovascular", "retail", "hospital"],
        "base_score": 0.81,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Riyadh Pharma",
        "url": "https://www.riyadhpharma.com/about-us/",
        "category": "other",
        "description": "Saudi pharmaceutical manufacturer covering cardiovascular, gastrointestinal, respiratory, and other therapeutic areas.",
        "focus": ["general", "respiratory", "cardiovascular", "gastrointestinal"],
        "base_score": 0.80,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Avalon Pharma",
        "url": "https://www.avalonpharmaceutical.com/contact-us",
        "category": "other",
        "description": "Saudi pharmaceutical company with commercial contacts, Riyadh headquarters, branch offices, and distributor network.",
        "focus": ["general", "respiratory", "retail", "hospital"],
        "base_score": 0.80,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Sudair Pharma",
        "url": "https://sudairpharma.com/about-us/",
        "category": "other",
        "description": "Saudi company focused on localizing advanced oncology and hematology pharmaceutical manufacturing.",
        "focus": ["oncology", "hospital", "government"],
        "base_score": 0.84,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Gulf Medical Stores",
        "url": "https://gulfmedistores.com/",
        "category": "distributor",
        "description": "Licensed and regulated Saudi medical device distributor with hospital-facing sales and manufacturer representation.",
        "focus": ["hospital", "imaging", "medical_device"],
        "base_score": 0.78,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "AMICO Group",
        "url": "https://www.amicogroup.com/about-us/",
        "category": "distributor",
        "description": "Saudi-founded MENA medical technology distributor serving specialist healthcare fields, useful for device-linked hospital opportunities.",
        "focus": ["hospital", "imaging", "ophthalmology", "medical_device"],
        "base_score": 0.77,
        "has_product_listing": True,
        "language": "en",
    },
    {
        "title": "Dr. Sulaiman Al Habib Medical Group",
        "url": "https://hmg.com.sa/en/Pages/home.aspx",
        "category": "hospital_group",
        "description": "Large private hospital group in Saudi Arabia and the wider Middle East, relevant for hospital formulary and procurement screening.",
        "focus": ["hospital", "oncology", "imaging", "respiratory", "cardiovascular"],
        "base_score": 0.76,
        "has_product_listing": False,
        "language": "en",
    },
    {
        "title": "Mouwasat Medical Services",
        "url": "https://www.mouwasat.com/en/about-us",
        "category": "hospital_group",
        "description": "Saudi hospital network and medical services company with hospitals, medical centers, pharmaceutical warehouses, and pharmacies.",
        "focus": ["hospital", "oncology", "imaging", "respiratory", "cardiovascular"],
        "base_score": 0.75,
        "has_product_listing": False,
        "language": "en",
    },
]


MULTI_LABEL_PUBLIC_SUFFIXES = {
    "com.sa",
    "net.sa",
    "org.sa",
    "gov.sa",
    "edu.sa",
    "med.sa",
    "sch.sa",
}


THERAPEUTIC_TERMS: dict[str, tuple[str, ...]] = {
    "oncology": (
        "oncology",
        "cancer",
        "hematology",
        "tumor",
        "antineoplastic",
        "capecitabine",
        "docetaxel",
        "bortezomib",
        "lenalidomide",
    ),
    "respiratory": (
        "respiratory",
        "asthma",
        "copd",
        "inhal",
        "salmeterol",
        "fluticasone",
        "sereterol",
    ),
    "cardiovascular": (
        "cardio",
        "hypertension",
        "antiplatelet",
        "lipid",
        "statin",
        "cilostazol",
        "rosuvastatin",
        "telmisartan",
        "amlodipine",
    ),
    "gastrointestinal": (
        "gastro",
        "intestinal",
        "gi ",
        "acid",
        "motility",
        "tegoprazan",
        "itopride",
        "pyridostigmine",
        "gastiin",
    ),
    "imaging": (
        "contrast",
        "radiology",
        "imaging",
        "gadolinium",
        "gadobutrol",
        "gadvoa",
        "injection",
        "vial",
    ),
    "inner_beauty": (
        "agatri",
        "agastache",
        "baechohyang",
        "skin",
        "beauty",
        "collagen",
        "moisture",
        "uv",
        "supplement",
        "nutraceutical",
        "functional food",
        "dietary",
    ),
}


def registrable_domain_from_host(host: str) -> str:
    host = (host or "").strip().lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_LABEL_PUBLIC_SUFFIXES:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def registrable_domain_from_url(url: str) -> str:
    return registrable_domain_from_host(urlparse(url or "").netloc)


def _base_domain(url: str) -> str:
    return registrable_domain_from_url(url)


def _product_text(drug_info: dict[str, Any]) -> str:
    fields = [
        drug_info.get("trade_name"),
        drug_info.get("ingredients"),
        drug_info.get("dosage_form"),
        drug_info.get("strength"),
    ]
    return " ".join(str(v or "") for v in fields).lower()


def infer_product_tags(drug_info: dict[str, Any]) -> set[str]:
    text = _product_text(drug_info)
    tags = {
        tag
        for tag, terms in THERAPEUTIC_TERMS.items()
        if any(term in text for term in terms)
    }
    if re.search(r"\b(injection|injectable|infusion|vial|ampoule|syringe|iv)\b", text):
        tags.add("hospital")
    if re.search(r"\b(tablet|capsule|caplet|syrup|suspension|oral)\b", text):
        tags.add("retail")
    if "inhal" in text:
        tags.update({"respiratory", "retail"})
    if {"inner_beauty"} & tags:
        tags.update({"retail", "pharmacy_chain", "general"})
    if not tags:
        tags.add("general")
    return tags


def curated_buyer_candidates(
    drug_info: dict[str, Any],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Return ranked Saudi buyer seeds shaped like Perplexity source items."""
    tags = infer_product_tags(drug_info)
    out: list[dict[str, Any]] = []

    for seed in SAUDI_BUYER_SEEDS:
        focus = set(seed.get("focus") or [])
        matched = sorted(tags & focus)
        score = float(seed.get("base_score") or 0.75)

        if matched:
            score += 0.05 + min(0.04, 0.01 * len(matched))
        if "oncology" in tags and "oncology" in focus:
            score += 0.08
        if "imaging" in tags and "imaging" in focus:
            score += 0.06
        if "respiratory" in tags and "respiratory" in focus:
            score += 0.04
        if "cardiovascular" in tags and "cardiovascular" in focus:
            score += 0.04
        if "gastrointestinal" in tags and "gastrointestinal" in focus:
            score += 0.04
        if "hospital" in tags and seed.get("category") in {
            "distributor",
            "hospital_group",
            "government_procurement",
        }:
            score += 0.03
        if "retail" in tags and seed.get("category") == "pharmacy_chain":
            score += 0.03
        if seed.get("category") in {"distributor", "importer"}:
            score += 0.02

        url = str(seed["url"])
        domain = urlparse(url).netloc.lower()
        title = str(seed["title"])
        category = str(seed["category"])
        reason = (
            f"Matched product tags: {', '.join(matched)}."
            if matched
            else "General Saudi healthcare buyer/distributor seed."
        )

        out.append(
            {
                "url": url,
                "website": url,
                "domain": domain,
                "base_domain": _base_domain(url),
                "title": title,
                "company": title,
                "name": title,
                "country": "Saudi Arabia",
                "category": category,
                "type": category,
                "description": seed["description"],
                "relevance_score": round(min(score, 0.98), 3),
                "has_price_data": False,
                "has_product_listing": bool(seed.get("has_product_listing", False)),
                "language": seed.get("language", "en"),
                "source": "curated_saudi_buyer_seed",
                "reasons": [reason, "Use website contact or corporate business development route for outreach validation."],
                "references": [url],
                "portfolio": sorted(focus),
            }
        )

    out.sort(key=lambda item: (-float(item["relevance_score"]), str(item["title"])))
    return out[: max(1, limit)]
