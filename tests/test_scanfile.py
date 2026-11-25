from datetime import datetime
from types import SimpleNamespace

import pytest

from brscan.scanfile import build_name, pdf_metadata, sanitize_filename, sanitize_folder, unfiled_name


def filing(**kw):
    base = dict(document_type="grocery receipt", folder="Receipts", vendor="Safeway",
                date="2026-11-20", amount="71.29", filename="Safeway receipt",
                title="Safeway grocery receipt", summary="Weekly groceries.")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("raw,expected", [
    ("tax forms", "TaxForms"),
    ("MEDICAL BILLS", "MedicalBills"),
    ("RAV4Docs", "RAV4Docs"),
    ("receipts/2026", "Receipts2026"),
    ("../..", "Unfiled"),
    ("", "Unfiled"),
])
def test_sanitize_folder(raw, expected):
    assert sanitize_folder(raw) == expected


def test_sanitize_filename_strips_path_separators_and_control_chars():
    assert sanitize_filename(" a/b\\c:d\x01  e ") == "a-b-c-d e"
    assert sanitize_filename("   ") == "scan"
    assert len(sanitize_filename("x" * 500)) == 120


def test_build_name_appends_date_once():
    assert build_name(filing()) == "Safeway receipt 2026-11-20.pdf"
    assert build_name(filing(filename="Safeway 2026-11-20")) == "Safeway 2026-11-20.pdf"
    assert build_name(filing(date="")) == "Safeway receipt.pdf"


def test_pdf_metadata():
    md = pdf_metadata(filing(), "Receipts")
    assert md["title"] == "Safeway grocery receipt"
    assert md["author"] == "Safeway"
    assert md["subject"] == "Weekly groceries."
    assert md["keywords"] == ["grocery receipt", "Safeway", "Receipts", "2026-11-20", "$71.29"]
    assert md["creationdate"] == datetime(2026, 11, 20)


def test_pdf_metadata_tolerates_missing_fields():
    md = pdf_metadata(filing(vendor="", amount="", date="sometime", summary=""), "Misc")
    assert md["author"] is None and md["subject"] is None
    assert "creationdate" not in md
    assert md["keywords"] == ["grocery receipt", "Misc", "sometime"]


def test_unfiled_name():
    assert unfiled_name(datetime(2026, 11, 24, 9, 5, 7)) == "Scan 2026-11-24 090507.pdf"


def test_segment_orders_dedupes_and_drops_out_of_range():
    from brscan.scanfile import segment
    assert segment(5, [(3, "b"), (0, "a"), (3, "c"), (9, "d")]) == [
        (range(0, 3), "a"), (range(3, 5), "b")]
    assert segment(4, [(2, "b")]) == [(range(0, 4), "b")]
    assert segment(3, []) == []


def test_segment_snaps_to_sheet_starts():
    from brscan.scanfile import segment
    assert segment(5, [(0, "a"), (3, "b")], sheet_starts=[0, 2, 4]) == [
        (range(0, 2), "a"), (range(2, 5), "b")]
    assert segment(4, [(0, "a"), (1, "b")], sheet_starts=[0, 2]) == [(range(0, 4), "a")]


def test_plan_batch_plans_each_document_and_falls_back_to_unfiled(tmp_path):
    from brscan.scanfile import UNFILED, plan_batch
    pages = [tmp_path / f"p{i}.jpg" for i in range(3)]
    other = filing()
    other.folder, other.filename = "bills", "PSE bill"
    docs, turns = plan_batch(lambda pg, fo, **kw: ([(0, filing()), (1, other)], {1: 180, 7: 90}),
                             pages, [])
    assert [(len(p), plan.folder, plan.name) for p, plan in docs] == [
        (1, "Receipts", "Safeway receipt 2026-11-20.pdf"), (2, "Bills", "PSE bill 2026-11-20.pdf")]
    assert turns == {1: 180}

    def boom(*a, **kw):
        raise RuntimeError("API down")
    [(only_pages, plan)], turns = plan_batch(boom, pages, [])
    assert only_pages == pages and plan.folder == UNFILED and plan.error == "API down"
    assert turns == {}
