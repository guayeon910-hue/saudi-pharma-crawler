"""
report_generator.py -- 1공정 시장조사 보고서 DOCX 자동 생성

타겟 검색 결과(AggregatedResult)를 받아서
유나이티드제약 시장조사 보고서 양식의 DOCX 파일을 생성한다.

보고서 구조:
    1. 표지 (제품명, 대상 시장, 일자)
    2. 요약 (수출 가능 여부 판정 + 근거)
    3. 소스별 검색 결과 (공공조달 / 민간)
    4. 주요 경쟁 제품 목록 (가격, 제조사, 성분)
    5. 결론 및 권고

사용:
    from report_generator import generate_report
    path = generate_report(drug, search_data_dict)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

logger = logging.getLogger("report_generator")

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _add_heading(doc: Document, text: str, level: int = 1) -> None:
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = RGBColor(0x1A, 0x56, 0xDB)


def _add_table_row(table, cells_data: list[str], bold: bool = False) -> None:
    row = table.add_row()
    for i, text in enumerate(cells_data):
        cell = row.cells[i]
        cell.text = str(text)
        if bold:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True


def _set_table_style(table) -> None:
    """테이블 스타일 설정."""
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # Header row shading
    if table.rows:
        for cell in table.rows[0].cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(9)


def _feasibility_korean(value: str) -> tuple[str, str]:
    """수출 가능 여부를 한글 + 색상 힌트로 변환."""
    mapping = {
        "가능": ("수출 가능", "green"),
        "조건부": ("조건부 가능", "orange"),
        "불가": ("수출 불가", "red"),
        "possible": ("수출 가능", "green"),
        "conditional": ("조건부 가능", "orange"),
        "impossible": ("수출 불가", "red"),
    }
    return mapping.get(value, (value, "gray"))


def generate_report(drug: Any, search_data: dict) -> Path:
    """DOCX 시장조사 보고서 생성.

    Args:
        drug: TargetDrug 인스턴스 또는 dict
        search_data: AggregatedResult.to_dict() 결과

    Returns:
        생성된 DOCX 파일 경로
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # drug가 dict일 수도 있음 (API에서 넘어올 때)
    if isinstance(drug, dict):
        trade_name = drug.get("trade_name", "Unknown")
        ingredient = drug.get("ingredient", "")
        strength = drug.get("strength", "")
        dosage_form = drug.get("dosage_form", "")
        drug_type = drug.get("drug_type", "")
        drug_id = drug.get("id", "unknown")
    else:
        trade_name = drug.trade_name
        ingredient = drug.ingredient
        strength = drug.strength
        dosage_form = drug.dosage_form
        drug_type = drug.drug_type
        drug_id = drug.id

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
    date_str = now.strftime("%Y%m%d")

    doc = Document()

    # ── 기본 스타일 설정 ──
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Malgun Gothic"
    font.size = Pt(10)

    # ═══════════════════════════════════════════════
    # 1. 표지
    # ═══════════════════════════════════════════════
    doc.add_paragraph("")
    doc.add_paragraph("")
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("시장조사 보고서")
    run.font.size = Pt(28)
    run.bold = True
    run.font.color.rgb = RGBColor(0x1A, 0x56, 0xDB)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("Saudi Arabia Market Research Report")
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    doc.add_paragraph("")

    # 표지 정보 테이블
    cover_table = doc.add_table(rows=0, cols=2)
    cover_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cover_data = [
        ("대상 제품", trade_name),
        ("종류", drug_type),
        ("성분", ingredient),
        ("함량", strength),
        ("제형", dosage_form),
        ("대상 시장", "Saudi Arabia"),
        ("조사 일자", timestamp),
    ]
    for label, value in cover_data:
        row = cover_table.add_row()
        row.cells[0].text = label
        row.cells[1].text = str(value)
        for p in row.cells[0].paragraphs:
            for r in p.runs:
                r.bold = True

    doc.add_page_break()

    # ═══════════════════════════════════════════════
    # 2. 요약
    # ═══════════════════════════════════════════════
    _add_heading(doc, "1. Executive Summary", level=1)

    feasibility = search_data.get("export_feasibility", "불가")
    feas_text, feas_color = _feasibility_korean(feasibility)

    p = doc.add_paragraph()
    p.add_run("수출 가능 여부: ").bold = True
    run = p.add_run(feas_text)
    run.bold = True
    run.font.size = Pt(14)
    if feas_color == "green":
        run.font.color.rgb = RGBColor(0x05, 0x96, 0x69)
    elif feas_color == "orange":
        run.font.color.rgb = RGBColor(0xD9, 0x77, 0x06)
    else:
        run.font.color.rgb = RGBColor(0xDC, 0x26, 0x26)

    doc.add_paragraph("")
    rationale = search_data.get("feasibility_rationale", "")
    p = doc.add_paragraph()
    p.add_run("판정 근거: ").bold = True
    p.add_run(rationale)

    total_matches = search_data.get("total_matches", 0)
    duration = search_data.get("search_duration_sec", 0)
    p = doc.add_paragraph()
    p.add_run(f"총 수집 데이터: {total_matches}건 | 소요 시간: {duration}초")

    evidence_urls = search_data.get("feasibility_evidence_urls", [])
    if evidence_urls:
        p = doc.add_paragraph()
        p.add_run("근거 URL: ").bold = True
        for url in evidence_urls:
            p.add_run(f"\n  - {url}")

    doc.add_paragraph("")

    # ═══════════════════════════════════════════════
    # 3. 소스별 검색 결과
    # ═══════════════════════════════════════════════
    _add_heading(doc, "2. Source-by-Source Results", level=1)

    source_results = search_data.get("source_results", [])

    # 결과 요약 테이블
    summary_table = doc.add_table(rows=1, cols=5)
    summary_table.style = "Table Grid"
    headers = ["Source", "Category", "Matches", "Time(s)", "Status"]
    for i, h in enumerate(headers):
        summary_table.rows[0].cells[i].text = h

    for sr in source_results:
        name = sr.get("source_name", "")
        cat = sr.get("source_category", "")
        matches = len(sr.get("matches", []))
        t = sr.get("search_time_sec", 0)
        status = sr.get("error", "OK") if sr.get("error") else "OK"
        if len(status) > 40:
            status = status[:40] + "..."
        _add_table_row(summary_table, [name, cat, str(matches), f"{t:.1f}" if t else "-", status])

    _set_table_style(summary_table)
    doc.add_paragraph("")

    # ═══════════════════════════════════════════════
    # 4. 주요 경쟁 제품 분석
    # ═══════════════════════════════════════════════
    _add_heading(doc, "3. Competitive Product Analysis", level=1)

    # 공공조달 (SFDA)
    sfda_matches = []
    for sr in source_results:
        if sr.get("source_category") == "공공조달" and sr.get("matches"):
            sfda_matches.extend(sr["matches"])

    if sfda_matches:
        _add_heading(doc, "3.1 SFDA Registered Products (Regulatory)", level=2)

        reg_table = doc.add_table(rows=1, cols=5)
        reg_table.style = "Table Grid"
        for i, h in enumerate(["Trade Name", "Scientific Name", "Strength", "Form", "Price (SAR)"]):
            reg_table.rows[0].cells[i].text = h

        for m in sfda_matches[:30]:  # max 30
            tn = m.get("trade_name", "N/A")
            sn = m.get("scientific_name", "")
            st = m.get("strength", "")
            df = m.get("dosage_form", "")
            pr = m.get("price_sar", "")
            _add_table_row(reg_table, [str(tn), str(sn), str(st), str(df), str(pr)])

        _set_table_style(reg_table)
        doc.add_paragraph(f"  * SFDA 등록 제품 총 {len(sfda_matches)}건 확인")
    else:
        doc.add_paragraph("SFDA 등록 데이터에서 관련 제품을 찾지 못했습니다.")

    doc.add_paragraph("")

    # 민간 (소매약국)
    retail_matches = []
    for sr in source_results:
        if sr.get("source_category") == "민간" and sr.get("matches"):
            retail_matches.extend(
                [{"_source": sr["source_name"], **m} for m in sr["matches"]]
            )

    if retail_matches:
        _add_heading(doc, "3.2 Retail Pharmacy Products (Market Price)", level=2)

        ret_table = doc.add_table(rows=1, cols=5)
        ret_table.style = "Table Grid"
        for i, h in enumerate(["Source", "Product Name", "Brand", "Price (SAR)", "URL"]):
            ret_table.rows[0].cells[i].text = h

        for m in retail_matches[:30]:
            src = m.get("_source", "")
            name = m.get("name", m.get("trade_name", "N/A"))
            brand = m.get("brand", m.get("manufacturer_or_marketing_company", ""))
            price = m.get("price", m.get("price_sar", m.get("retail_price", "")))
            url = m.get("absolute_url", m.get("url", m.get("source_url", "")))
            if url and len(str(url)) > 50:
                url = str(url)[:50] + "..."
            _add_table_row(ret_table, [str(src), str(name), str(brand), str(price), str(url)])

        _set_table_style(ret_table)
        doc.add_paragraph(f"  * 소매 약국 제품 총 {len(retail_matches)}건 확인")
    else:
        doc.add_paragraph("소매 약국에서 관련 제품을 찾지 못했습니다.")

    doc.add_paragraph("")

    # ═══════════════════════════════════════════════
    # 5. 가격 분석 요약
    # ═══════════════════════════════════════════════
    _add_heading(doc, "4. Price Analysis Summary", level=1)

    all_prices = []
    for sr in source_results:
        for m in sr.get("matches", []):
            price = m.get("price_sar") or m.get("price") or m.get("retail_price")
            if price is not None:
                try:
                    all_prices.append(float(price))
                except (ValueError, TypeError):
                    pass

    if all_prices:
        min_p = min(all_prices)
        max_p = max(all_prices)
        avg_p = sum(all_prices) / len(all_prices)
        doc.add_paragraph(f"Price Range: {min_p:.2f} ~ {max_p:.2f} SAR")
        doc.add_paragraph(f"Average Price: {avg_p:.2f} SAR")
        doc.add_paragraph(f"Data Points: {len(all_prices)}")
    else:
        doc.add_paragraph("가격 데이터가 수집되지 않았습니다.")

    doc.add_paragraph("")

    # ═══════════════════════════════════════════════
    # 6. 결론 및 권고
    # ═══════════════════════════════════════════════
    _add_heading(doc, "5. Conclusion & Recommendation", level=1)

    p = doc.add_paragraph()
    p.add_run(f"대상 제품: ").bold = True
    p.add_run(f"{trade_name} ({ingredient})")

    p = doc.add_paragraph()
    p.add_run(f"수출 판정: ").bold = True
    p.add_run(feas_text)

    doc.add_paragraph("")
    doc.add_paragraph(rationale)

    if feasibility in ("가능", "possible"):
        doc.add_paragraph(
            "권고: 동일 성분이 SFDA에 등록되어 있으므로, "
            "사우디 시장 진입을 위한 등록 절차를 진행할 것을 권고합니다. "
            "소매 약국에서의 경쟁 제품 가격대를 참고하여 가격 전략을 수립하시기 바랍니다."
        )
    elif feasibility in ("조건부", "conditional"):
        doc.add_paragraph(
            "권고: 유사 성분이 SFDA에 등록되어 있으나 제형 또는 함량 차이가 있습니다. "
            "추가 조사 및 SFDA 사전 상담을 통해 등록 가능성을 확인한 후 진행할 것을 권고합니다."
        )
    else:
        doc.add_paragraph(
            "권고: SFDA 등록 데이터에서 해당 성분을 확인하지 못했습니다. "
            "시장 진입 전 SFDA와의 사전 협의 및 추가 시장조사가 필요합니다."
        )

    # ── Footer ──
    doc.add_paragraph("")
    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run(f"Generated by Saudi Pharma Crawler | {timestamp}")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)

    # ── 저장 ──
    filename = f"market_report_{drug_id}_{date_str}.docx"
    output_path = REPORTS_DIR / filename
    doc.save(str(output_path))
    logger.info(f"Report saved: {output_path}")

    return output_path


# ── CLI ──
if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    sys.path.insert(0, str(Path(__file__).resolve().parent / "assets" / "snippets"))
    from drug_registry import DrugRegistry
    from targeted_search import search_one_drug

    reg = DrugRegistry()
    drugs = reg.list_drugs()

    if not drugs:
        print("No drugs registered.")
        sys.exit(1)

    drug_id = sys.argv[1] if len(sys.argv) > 1 else drugs[0].id
    drug = reg.get_drug(drug_id)
    if not drug:
        print(f"Drug '{drug_id}' not found.")
        sys.exit(1)

    print(f"Searching: {drug.trade_name}...")
    result = search_one_drug(drug)
    search_data = result.to_dict()

    print(f"Generating report...")
    path = generate_report(drug, search_data)
    print(f"Report saved: {path}")
