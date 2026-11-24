"""scanfile-watch: always-on sidecar that files every document fed into the scanner.

Polls the scanner's eSCL status. When a sheet is inserted it scans it, groups
sheets fed within a short window into one document, classifies the document with
Claude, uploads the PDF to Google Drive through rclone, and sends a phone
notification through a Home Assistant webhook. Built to run as a container on an
always-on host; see deploy/ in the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import requests

from . import discovery
from .cli import scan_one_job
from .escl import (COLOR_MODES, ESCL_NS, PWG_NS, Fatal, Jam, Recoverable, Scanner,
                   ScanSettings, Unreachable)
from .output import assemble_pdf, crop
from .scanfile import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORTS, plan_filing, unique_path

LOADED = "ScannerAdfLoaded"
PENDING_RETRY_SECS = 300


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def parse_status(xml: bytes) -> Optional[Tuple[str, str]]:
    """(pwg:State, scan:AdfState) from a ScannerStatus document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    state = root.findtext(f"{{{PWG_NS}}}State") or ""
    adf = root.findtext(f".//{{{ESCL_NS}}}AdfState") or ""
    return state, adf


class Drive:
    """A folder tree on any rclone remote (Google Drive in production)."""

    def __init__(self, remote: str):
        self.remote = remote.rstrip("/")

    def _rclone(self, *args: str, timeout: float = 120) -> str:
        r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"rclone {args[0]} failed: {r.stderr.strip()[-400:]}")
        return r.stdout

    def _ls(self, path: str, *flags: str) -> List[dict]:
        try:
            return json.loads(self._rclone("lsjson", *flags, path) or "[]")
        except RuntimeError as exc:
            if "directory not found" in str(exc):
                return []
            raise

    def folders(self) -> List[str]:
        return sorted(e["Name"] for e in self._ls(self.remote, "--dirs-only")
                      if not e["Name"].startswith("."))

    def unique_name(self, folder: str, name: str) -> str:
        taken = {e["Name"] for e in self._ls(f"{self.remote}/{folder}", "--files-only")}
        stem, ext = os.path.splitext(name)
        cand, i = name, 2
        while cand in taken and i < 1000:
            cand, i = f"{stem} ({i}){ext}", i + 1
        return cand

    def upload(self, src: Path, folder: str, name: str) -> Optional[str]:
        """Copy src to folder/name; return a browser link when the remote has file IDs."""
        dest = f"{self.remote}/{folder}/{name}"
        self._rclone("copyto", str(src), dest, timeout=600)
        try:
            file_id = json.loads(self._rclone("lsjson", "--stat", dest)).get("ID")
        except (RuntimeError, ValueError):
            return None
        return f"https://drive.google.com/file/d/{file_id}/view" if file_id else None


class Notifier:
    """POSTs {title, message, url} to a Home Assistant webhook; logs either way."""

    def __init__(self, url: Optional[str]):
        self.url = url

    def __call__(self, title: str, message: str, link: Optional[str] = None) -> None:
        log(f"notify: {title}: {message}")
        if not self.url:
            return
        try:
            requests.post(self.url, json={"title": title, "message": message, "url": link or ""},
                          timeout=10)
        except requests.RequestException as exc:
            log(f"notification failed: {exc}")


class Watcher:
    def __init__(self, args, drive: Drive, notify: Callable[..., None],
                 classify_fn: Callable, sleep: Callable[[float], None] = time.sleep):
        self.args = args
        self.drive = drive
        self.notify = notify
        self.classify_fn = classify_fn
        self.sleep = sleep
        self.data = Path(args.data_dir)
        self.work = self.data / "work"
        self.pending = self.data / "pending"
        self.heartbeat = self.data / "heartbeat"
        for d in (self.work, self.pending):
            d.mkdir(parents=True, exist_ok=True)
        color = COLOR_MODES.get(args.color.lower(), args.color)
        self.settings = ScanSettings(res=args.res, color=color, duplex=args.duplex, size=args.size)
        self.scanner: Optional[Scanner] = None
        self.last_seen = time.monotonic()
        self.last_pending_try = 0.0

    # ----------------------------------------------------------------- scanner
    def beat(self) -> None:
        self.heartbeat.touch()

    def locate(self) -> Optional[Scanner]:
        a = self.args
        if a.host:
            host, port = a.host, a.port
        else:
            found = discovery.discover(a.discover_secs, prefer_ip=True)
            if not found:
                return self.scanner
            host, port = found
            if not self.scanner or self.scanner.host != host:
                log(f"scanner found at {host}:{port}")
        return Scanner(host, port, retries=a.retries, note=lambda m: log(m))

    def status(self) -> Optional[Tuple[str, str]]:
        if self.scanner is None:
            self.scanner = self.locate()
            if self.scanner is None:
                return None
        try:
            r = self.scanner.session.get(f"{self.scanner.base}/ScannerStatus", timeout=5)
        except requests.RequestException:
            r = None
        if r is None or r.status_code != 200:
            if not self.args.host and time.monotonic() - self.last_seen > self.args.rediscover_secs:
                self.scanner = self.locate()
                self.last_seen = time.monotonic()
            return None
        self.last_seen = time.monotonic()
        return parse_status(r.content)

    def sheet_ready(self) -> bool:
        st = self.status()
        return bool(st) and st[1] == LOADED and st[0] in ("Idle", "")

    def wait_for_sheet(self, window: float) -> bool:
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            self.beat()
            if self.sheet_ready():
                return True
            self.sleep(self.args.poll)
        return False

    # ---------------------------------------------------------------- document
    def collect_document(self) -> bool:
        """Scan the inserted sheet plus any fed within the idle window, then file them.

        Returns False when nothing could be scanned.
        """
        a = self.args
        batch = Path(tempfile.mkdtemp(prefix="doc-", dir=self.work))
        count = 0
        try:
            while True:
                self.sleep(a.settle)
                window = a.idle
                try:
                    count = scan_one_job(self.scanner, self.settings, batch, count, a.wait)
                    log(f"{count} page(s) so far; waiting {a.idle:.0f}s for another sheet")
                except (Jam, Unreachable) as exc:
                    window = a.jam
                    self.notify("Scanner problem",
                                f"{exc}. Clear it and feed that sheet again within "
                                f"{a.jam / 60:.0f} min ({count} page(s) kept so far).")
                except Recoverable as exc:
                    log(f"no sheet scanned: {exc}")
                    if count == 0:
                        return False
                except Fatal as exc:
                    log(f"scan failed: {exc}")
                    if count == 0:
                        return False
                if not self.wait_for_sheet(window):
                    break
            if count:
                self.file_document(batch)
            return count > 0
        finally:
            shutil.rmtree(batch, ignore_errors=True)

    def file_document(self, batch: Path) -> None:
        a = self.args
        pages = sorted(batch.glob("page-*.jpg"))
        if a.crop:
            for p in pages:
                crop(p)
        try:
            folders = self.drive.folders()
        except Exception as exc:
            log(f"could not list Drive folders: {exc}")
            folders = []
        log(f"classifying {len(pages)} page(s) with {a.model}...")
        plan = plan_filing(self.classify_fn, pages, folders, model=a.model,
                           api_key=a.api_key, effort=a.effort)
        pdf = batch / "out.pdf"
        assemble_pdf(pdf, pages, metadata=plan.metadata)
        pages_note = f"{len(pages)} page{'s' if len(pages) != 1 else ''}"
        try:
            name = self.drive.unique_name(plan.folder, plan.name)
            link = self.drive.upload(pdf, plan.folder, name)
        except Exception as exc:
            keep = unique_path(self.pending / plan.folder / plan.name)
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), str(keep))
            self.notify("Scan saved on the server",
                        f"Drive upload failed, will retry: {plan.folder}/{plan.name} ({exc})")
            return
        if plan.error:
            self.notify("Scan filed in Unfiled",
                        f"{plan.folder}/{name} ({pages_note}). Classification failed: {plan.error}",
                        link)
        else:
            self.notify("Scan filed", f"{plan.folder}/{name} ({pages_note})", link)

    def retry_pending(self) -> None:
        if time.monotonic() - self.last_pending_try < PENDING_RETRY_SECS:
            return
        self.last_pending_try = time.monotonic()
        for pdf in sorted(self.pending.glob("*/*.pdf")):
            folder = pdf.parent.name
            try:
                name = self.drive.unique_name(folder, pdf.name)
                link = self.drive.upload(pdf, folder, name)
            except Exception as exc:
                log(f"pending upload still failing: {exc}")
                return
            pdf.unlink()
            self.notify("Scan filed", f"{folder}/{name} (uploaded after a retry)", link)

    # -------------------------------------------------------------------- loop
    def run(self) -> None:
        a = self.args
        log(f"watching for documents; filing to {self.drive.remote} with {a.model}")
        idle_logged = False
        while True:
            self.beat()
            self.retry_pending()
            st = self.status()
            if st is None:
                if not idle_logged:
                    log("scanner not reachable (asleep or off); still watching")
                    idle_logged = True
                self.sleep(a.offline_poll)
                continue
            if idle_logged:
                log(f"scanner reachable at {self.scanner.host}:{self.scanner.port}")
                idle_logged = False
            if st[1] == LOADED and st[0] in ("Idle", ""):
                log("sheet detected")
                if not self.collect_document():
                    self.sleep(a.cooldown)
            else:
                self.sleep(a.poll)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanfile-watch",
        description="Watch the scanner; scan, classify, and file every document into Google Drive.",
    )
    p.add_argument("--drive", default=os.environ.get("SCANFILE_DRIVE_REMOTE"),
                   help="rclone destination root, e.g. scans:Scans (SCANFILE_DRIVE_REMOTE)")
    p.add_argument("--notify-url", default=os.environ.get("SCANFILE_NOTIFY_URL"),
                   help="Home Assistant webhook URL for notifications (SCANFILE_NOTIFY_URL)")
    p.add_argument("--data-dir", default=_env("SCANFILE_DATA_DIR", "/data"))
    p.add_argument("--model", default=_env("SCANFILE_MODEL", DEFAULT_MODEL))
    p.add_argument("--effort", choices=EFFORTS, default=_env("SCANFILE_EFFORT", DEFAULT_EFFORT))
    p.add_argument("-H", "--host", default=os.environ.get("BRSCAN_HOST") or None,
                   help="scanner IP (default: mDNS discovery)")
    p.add_argument("-P", "--port", type=int, default=int(_env("BRSCAN_PORT", "8080")))
    p.add_argument("-r", "--res", type=int, default=int(_env("BRSCAN_RES", "300")))
    p.add_argument("-c", "--color", default=_env("BRSCAN_COLOR", "color"))
    p.add_argument("-s", "--size", default=_env("BRSCAN_SIZE", "letter"),
                   choices=["letter", "a4", "legal", "max", "auto"])
    p.add_argument("-d", "--duplex", action="store_true",
                   default=_env("BRSCAN_DUPLEX", "0").lower() in ("1", "true", "yes"))
    p.add_argument("--no-crop", dest="crop", action="store_false", default=True)
    p.add_argument("--poll", type=float, default=float(_env("SCANFILE_POLL_SECS", "3")),
                   help="seconds between feeder checks while the scanner is awake")
    p.add_argument("--offline-poll", type=float, default=float(_env("SCANFILE_OFFLINE_POLL_SECS", "10")),
                   help="seconds between checks while the scanner is unreachable")
    p.add_argument("--idle", type=float, default=float(_env("SCANFILE_IDLE_SECS", "45")),
                   help="a sheet fed within this many seconds joins the same document")
    p.add_argument("--jam", type=float, default=float(_env("SCANFILE_JAM_SECS", "300")),
                   help="after a jam, seconds to wait for the sheet to be fed again")
    p.add_argument("--settle", type=float, default=float(_env("SCANFILE_SETTLE_SECS", "1.5")),
                   help="pause after a sheet is detected so it seats in the feeder")
    p.add_argument("-w", "--wait", type=int, default=int(_env("BRSCAN_WAKE_SECS", "25")))
    p.add_argument("--retries", type=int, default=int(_env("BRSCAN_RETRIES", "5")))
    p.add_argument("--discover-secs", type=float, default=float(_env("BRSCAN_DISCOVER_SECS", "5")))
    p.add_argument("--rediscover-secs", type=float, default=300.0, help=argparse.SUPPRESS)
    p.add_argument("--cooldown", type=float, default=30.0, help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.drive:
        print("scanfile-watch: set --drive or SCANFILE_DRIVE_REMOTE (e.g. scans:Scans)", file=sys.stderr)
        return 2
    args.api_key = os.environ.get("SCANFILE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not args.api_key:
        print("scanfile-watch: set SCANFILE_API_KEY", file=sys.stderr)
        return 2
    from .classify import classify

    watcher = Watcher(args, Drive(args.drive), Notifier(args.notify_url), classify)
    try:
        watcher.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
