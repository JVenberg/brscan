import shutil
from types import SimpleNamespace

import pytest

from brscan import watch
from brscan.escl import Jam

STATUS = b"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Idle</pwg:State>
  <scan:AdfState>ScannerAdfLoaded</scan:AdfState>
  <scan:Jobs><scan:JobInfo><pwg:JobState>Completed</pwg:JobState></scan:JobInfo></scan:Jobs>
</scan:ScannerStatus>"""


def test_parse_status():
    assert watch.parse_status(STATUS) == ("Idle", "ScannerAdfLoaded")
    assert watch.parse_status(b"not xml") is None


class FakeDrive:
    remote = "fake:Scans"

    def __init__(self, fail=False):
        self.fail = fail
        self.files = {}

    def folders(self):
        return sorted({f for f, _ in self.files})

    def unique_name(self, folder, name):
        return name if (folder, name) not in self.files else name.replace(".pdf", " (2).pdf")

    def upload(self, src, folder, name):
        if self.fail:
            raise RuntimeError("offline")
        self.files[(folder, name)] = src.read_bytes()
        return f"https://drive.example/{name}"


def filing():
    return SimpleNamespace(document_type="receipt", folder="Receipts", vendor="Safeway",
                           date="2026-11-20", amount="1.00", filename="Safeway receipt",
                           title="t", summary="s")


@pytest.fixture
def make_watcher(tmp_path, monkeypatch):
    from PIL import Image
    sample = tmp_path / "sample.jpg"
    Image.new("RGB", (60, 80), "white").save(sample, "JPEG")

    def factory(script, classify=lambda pages, folders, **kw: filing(), drive=None):
        """script: per-sheet outcomes, 'ok' or an exception, fed in order."""
        outcomes = list(script)
        ready = []

        def fake_scan(scanner, settings, pagedir, count, wait):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            shutil.copy(sample, pagedir / f"page-{count + 1:03d}.jpg")
            return count + 1

        monkeypatch.setattr(watch, "scan_one_job", fake_scan)
        args = watch.build_parser().parse_args([
            "--drive", "fake:Scans", "--data-dir", str(tmp_path / "data"),
            "--idle", "0.05", "--jam", "0.05", "--poll", "0.01", "--settle", "0"])
        args.api_key = "sk-test"
        notes = []
        w = watch.Watcher(args, drive or FakeDrive(), lambda *a: notes.append(a), classify,
                          sleep=lambda s: None)
        w.scanner = SimpleNamespace(host="h", port=8080)
        # every sheet after the first is "inserted" once the previous one is scanned
        monkeypatch.setattr(w, "sheet_ready", lambda: bool(outcomes) and ready.append(1) is None)
        return w, notes

    return factory


def test_sheets_fed_within_window_become_one_document(make_watcher):
    drive = FakeDrive()
    w, notes = make_watcher(["ok", "ok", "ok"], drive=drive)
    assert w.collect_document()
    assert list(drive.files) == [("Receipts", "Safeway receipt 2026-11-20.pdf")]
    assert notes == [("Scan filed", "Receipts/Safeway receipt 2026-11-20.pdf (3 pages)",
                      "https://drive.example/Safeway receipt 2026-11-20.pdf")]
    assert not list(w.work.iterdir())


def test_jam_notifies_and_keeps_collecting(make_watcher):
    w, notes = make_watcher(["ok", Jam("possible paper jam"), "ok"])
    w.collect_document()
    assert notes[0][0] == "Scanner problem" and "1 page(s) kept" in notes[0][1]
    assert notes[1][1].endswith("(2 pages)")


def test_nothing_scanned_returns_false(make_watcher):
    from brscan.escl import Recoverable
    w, notes = make_watcher([Recoverable("no sheet in the feeder (HTTP 409)")])
    assert w.collect_document() is False
    assert notes == []


def test_classification_failure_files_to_unfiled(make_watcher):
    def boom(pages, folders, **kw):
        raise RuntimeError("API down")
    drive = FakeDrive()
    w, notes = make_watcher(["ok"], classify=boom, drive=drive)
    w.collect_document()
    (folder, name), = drive.files
    assert folder == "Unfiled" and name.startswith("Scan ")
    assert notes[0][0] == "Scan filed in Unfiled" and "API down" in notes[0][1]


def test_upload_failure_spools_then_retries(make_watcher):
    drive = FakeDrive(fail=True)
    w, notes = make_watcher(["ok"], drive=drive)
    w.collect_document()
    assert notes[0][0] == "Scan saved on the server"
    assert [p.name for p in w.pending.glob("*/*.pdf")] == ["Safeway receipt 2026-11-20.pdf"]

    drive.fail = False
    w.retry_pending()
    assert list(drive.files) == [("Receipts", "Safeway receipt 2026-11-20.pdf")]
    assert not list(w.pending.glob("*/*.pdf"))
    assert notes[-1][0] == "Scan filed"


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_drive_against_local_rclone_remote(tmp_path):
    root = tmp_path / "remote"
    drive = watch.Drive(str(root))
    assert drive.folders() == []
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    assert drive.upload(src, "Receipts", "x.pdf") is None
    assert (root / "Receipts" / "x.pdf").read_bytes() == b"%PDF-1.4 test"
    assert drive.folders() == ["Receipts"]
    assert drive.unique_name("Receipts", "x.pdf") == "x (2).pdf"
    assert drive.unique_name("Receipts", "y.pdf") == "y.pdf"
