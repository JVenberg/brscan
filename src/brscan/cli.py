"""Command-line interface for brscan."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, discovery
from .escl import COLOR_MODES, Fatal, Recoverable, Scanner, ScanSettings, Unreachable
from .output import assemble_pdf, crop, save_jpegs

MAX_PAGES = 200
NO_SCANNER_MSG = (
    "no scanner reachable. Power it on (single press) and ensure it is on the "
    "same Wi-Fi/subnet; first run discovers it over mDNS. Or pass -H <ip>."
)


def note(msg: str) -> None:
    print(f"brscan: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "None":
    print(f"brscan: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="brscan",
        description="Scan from a Brother DSmobile (eSCL / AirScan) scanner over HTTP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Sheet-fed scanner: load the page(s) first. Finds the scanner over "
            "mDNS and caches it (~/.cache/brscan/scanner); the scanner sleeps to "
            "save power, so brscan polls/wakes it. Device emits JPEG; PDF is "
            "assembled locally. Every flag has a BRSCAN_* environment variable."
        ),
    )
    p.add_argument("out", nargs="?", help="output file (default: scan-<timestamp>.<ext>)")
    p.add_argument("-o", "--out", dest="out_opt", help=argparse.SUPPRESS)
    p.add_argument("-r", "--res", type=int, default=int(_env("BRSCAN_RES", "300")),
                   help="resolution dpi: 100 150 200 300 400 600 (default 300)")
    p.add_argument("-c", "--color", default=_env("BRSCAN_COLOR", "color"),
                   help="color | gray | bw | auto (default color)")
    p.add_argument("-f", "--format", default=_env("BRSCAN_FORMAT", "pdf"),
                   choices=["pdf", "jpeg", "jpg"], help="pdf | jpeg (default pdf)")
    p.add_argument("-d", "--duplex", action="store_true", default=_env_bool("BRSCAN_DUPLEX"),
                   help="scan both sides (if the ADF supports duplex)")
    p.add_argument("-i", "--interactive", action="store_true", default=_env_bool("BRSCAN_INTERACTIVE"),
                   help="multi-page: scan a sheet, Enter for the next, Esc/q to finish")
    p.add_argument("-s", "--size", default=_env("BRSCAN_SIZE", "letter"),
                   choices=["letter", "a4", "legal", "max", "auto"],
                   help="letter | a4 | legal | max | auto (default letter)")
    p.add_argument("--width", type=int, help="scan width in 1/300 inch units (custom region)")
    p.add_argument("--height", type=int, help="scan height in 1/300 inch units (custom region)")
    p.add_argument("--no-crop", dest="crop", action="store_false", default=True,
                   help="keep the full scanned area incl. blank/gray padding")
    p.add_argument("--intent", default=_env("BRSCAN_INTENT", "Document"),
                   help="Document | Photo | Preview | TextAndGraphic")
    p.add_argument("-H", "--host", default=os.environ.get("BRSCAN_HOST") or None,
                   help="scanner IP or mDNS name (default: cache, then discovery)")
    p.add_argument("-P", "--port", type=int,
                   default=int(_env("BRSCAN_PORT", "0")) or None,
                   help="eSCL port (default: discovered, else 8080)")
    p.add_argument("-w", "--wait", type=int, default=int(_env("BRSCAN_WAKE_SECS", "25")),
                   help="poll a sleeping scanner up to SECS (default 25)")
    p.add_argument("--retries", type=int, default=int(_env("BRSCAN_RETRIES", "3")),
                   help="network retry attempts per request, with backoff (default 3)")
    p.add_argument("--discover-secs", type=float, default=float(_env("BRSCAN_DISCOVER_SECS", "4")),
                   help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true", help="print request XML and HTTP details")
    p.add_argument("--version", action="version", version=f"brscan {__version__}")

    actions = p.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_const", dest="action", const="status",
                         help="show scanner status (idle/busy, jobs)")
    actions.add_argument("--caps", action="store_const", dest="action", const="caps",
                         help="dump raw ScannerCapabilities XML")
    actions.add_argument("--discover", action="store_const", dest="action", const="discover",
                         help="find a scanner via mDNS and cache it")
    actions.add_argument("--forget", action="store_const", dest="action", const="forget",
                         help="clear the cached scanner")
    p.set_defaults(action="scan")
    return p


# --------------------------------------------------------------------------- #
# scanner location: explicit host > cache (quick probe) > mDNS > wake-poll
# --------------------------------------------------------------------------- #
def locate(args) -> Optional[Scanner]:
    explicit = bool(args.host)
    if explicit:
        cand_host, cand_port = args.host, args.port or discovery.DEFAULT_PORT
    else:
        cached = discovery.read_cache()
        if cached:
            cand_host, cand_port = cached
        else:
            cand_host, cand_port = None, args.port or discovery.DEFAULT_PORT

    def make(host, port):
        return Scanner(host, port, verbose=args.verbose, retries=args.retries,
                       note=note, log=lambda m: note(f"[debug] {m}"))

    if cand_host:
        sc = make(cand_host, cand_port)
        if sc.reachable(4):
            return sc

    if not explicit:
        note("discovering scanner via mDNS (_uscan._tcp)...")
        found = discovery.discover(args.discover_secs)
        if found:
            cand_host, cand_port = found
            note(f"found {cand_host}:{cand_port}")
            discovery.write_cache(cand_host, cand_port)
            sc = make(cand_host, cand_port)
            if sc.reachable(4):
                return sc

    if cand_host:
        note(f"waiting for {cand_host}:{cand_port} to respond (it sleeps to save power)...")
        sc = make(cand_host, cand_port)
        if sc.wait_until_reachable(args.wait):
            return sc
    return None


# --------------------------------------------------------------------------- #
# interactive keypress handling
# --------------------------------------------------------------------------- #
def _have_tty() -> bool:
    return os.path.exists("/dev/tty") and os.access("/dev/tty", os.R_OK)


def read_key() -> str:
    """Read one keypress from the controlling terminal: 'enter'|'esc'|'quit'|'other'."""
    try:
        import termios
        import tty
    except ImportError:
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        return "quit" if line.strip().lower() in ("q", "quit", "esc") else "enter"

    try:
        ttyf = open("/dev/tty", "rb", buffering=0)
    except OSError:
        return "quit"
    fd = ttyf.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # char-at-a-time, no echo, but keep ISIG so Ctrl-C works
        ch = ttyf.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        ttyf.close()

    if not ch:
        return "quit"  # EOF / Ctrl-D
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b"\x1b":
        return "esc"
    if ch in (b"q", b"Q"):
        return "quit"
    return "other"


def prompt_continue(page_count: int) -> bool:
    while True:
        sys.stderr.write(
            f"brscan: {page_count} page(s) captured. "
            "Press Enter to scan the next sheet, Esc or q to finish: "
        )
        sys.stderr.flush()
        key = read_key()
        sys.stderr.write("\n")
        if key == "enter":
            return True
        if key in ("esc", "quit"):
            return False
        # any other key: re-prompt


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
def build_settings(args) -> ScanSettings:
    color = COLOR_MODES.get(args.color.lower(), args.color)
    return ScanSettings(
        res=args.res,
        color=color,
        duplex=args.duplex,
        intent=args.intent,
        size=args.size,
        width=args.width,
        height=args.height,
    )


def scan_one_job(scanner: Scanner, settings: ScanSettings, pagedir: Path,
                 page_count: int, wait: int) -> int:
    """POST one job and append every page it returns. Returns the new page count.

    Raises Recoverable (no sheet / empty), Unreachable (lost the scanner), or Fatal.
    """
    try:
        job = scanner.create_job(settings)
    except Unreachable:
        note("lost contact with the scanner; waking it and retrying...")
        if not scanner.wait_until_reachable(wait):
            raise
        job = scanner.create_job(settings)

    added = 0
    while True:
        n = page_count + 1
        pf = pagedir / f"page-{n:03d}.jpg"
        status = scanner.fetch_page(job, pf)
        if status == "ok":
            page_count, added = n, added + 1
            note(f"received page {page_count} ({pf.stat().st_size} bytes)")
        elif status == "incomplete":
            page_count, added = n, added + 1
            note(f"WARNING: page {page_count} is incomplete after retries; keeping the partial image.")
        elif status == "error":
            note("WARNING: lost the connection mid-document; stopping with the pages captured so far.")
            break
        else:  # done
            break
        if page_count >= MAX_PAGES:
            break
    if added == 0:
        raise Recoverable("scanner accepted the job but returned no pages (empty feeder?)")
    return page_count


def do_scan(scanner: Scanner, args) -> int:
    fmt = "jpeg" if args.format in ("jpeg", "jpg") else "pdf"
    ext = "pdf" if fmt == "pdf" else "jpg"
    out = args.out or f"scan-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"
    out_path = Path(out).expanduser().resolve()

    settings = build_settings(args)
    interactive = args.interactive
    if interactive and not _have_tty():
        note("interactive mode needs a terminal; falling back to a single batch scan.")
        interactive = False

    mode = "duplex" if args.duplex else "simplex"
    color_label = settings.color.split(":")[-1]
    note(f"scanning on {scanner.host}:{scanner.port}  "
         f"({color_label} {args.res}dpi {mode}, {args.size} -> {fmt})")

    pagedir = Path(tempfile.mkdtemp(prefix="brscan-pages-"))
    page_count = 0
    try:
        if interactive:
            note("interactive multi-page mode: load a sheet and press Enter to scan it; "
                 "Esc or q to finish." + (" (duplex: each sheet adds 2 pages)" if args.duplex else ""))
            while prompt_continue(page_count):
                try:
                    page_count = scan_one_job(scanner, settings, pagedir, page_count, args.wait)
                except Unreachable as exc:
                    note(f"{exc}. Wake the scanner, then press Enter to retry or Esc to finish.")
                except Recoverable as exc:
                    note(f"{exc}. Load a sheet and press Enter, or Esc to finish.")
                except Fatal as exc:
                    die(str(exc))
        else:
            try:
                page_count = scan_one_job(scanner, settings, pagedir, page_count, args.wait)
            except Recoverable as exc:
                die(f"{exc} Load a page and retry.")
            except Fatal as exc:  # includes Unreachable
                die(str(exc))

        pages: List[Path] = sorted(pagedir.glob("page-*.jpg"))
        if not pages:
            if interactive:
                note("no pages scanned; nothing saved.")
                return 0
            die("no pages scanned.")

        if args.crop:
            for page in pages:
                crop(page)

        if fmt == "pdf":
            try:
                assemble_pdf(out_path, pages)
                note(f"saved {out_path}  ({len(pages)} page(s))")
            except Exception as exc:  # noqa: BLE001 - degrade to JPEGs on any assembler error
                note(f"PDF assembly failed ({exc}); saving JPEG pages instead.")
                save_jpegs(pages, out_path, note=note)
        else:
            if len(pages) == 1:
                shutil.copy(pages[0], out_path)
                note(f"saved {out_path}")
            else:
                save_jpegs(pages, out_path, note=note)
        return 0
    finally:
        shutil.rmtree(pagedir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.out_opt:  # -o/--out overrides the positional
        args.out = args.out_opt

    try:
        if args.action == "forget":
            if discovery.forget_cache():
                note(f"cleared cached scanner ({discovery.cache_file()})")
            else:
                note(f"no cached scanner to clear ({discovery.cache_file()})")
            return 0

        if args.action == "discover":
            found = discovery.discover(args.discover_secs)
            if not found:
                die(NO_SCANNER_MSG)
            host, port = found
            discovery.write_cache(host, port)
            note(f"found and cached: {host}:{port}  (cache: {discovery.cache_file()})")
            return 0

        scanner = locate(args)
        if scanner is None:
            die(NO_SCANNER_MSG)

        if args.action == "status":
            print(scanner.status())
            return 0
        if args.action == "caps":
            print(scanner.capabilities())
            return 0

        return do_scan(scanner, args)
    except KeyboardInterrupt:
        note("aborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
