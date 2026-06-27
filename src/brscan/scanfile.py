"""scanfile - scan a document, classify it with Claude, and auto-file it.

Companion to brscan: drives a scan, sends the page image(s) to Claude for
classification, and moves the resulting PDF into the matching folder under your
iCloud Documents (or any base directory). Fully automatic by default.

Requires the `ai` extra (the Anthropic SDK) and an API key in SCANFILE_API_KEY
(or ANTHROPIC_API_KEY as a fallback):
    uv tool install --force "brscan[ai] @ git+https://github.com/JVenberg/brscan"
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List

from .cli import _have_tty, find_scanner, note, prompt_continue, scan_one_job
from .escl import COLOR_MODES, Fatal, Recoverable, ScanSettings, Unreachable
from .output import assemble_pdf, crop

DEFAULT_BASE = "~/Library/Mobile Documents/com~apple~CloudDocs/Documents/Scans"
DEFAULT_MODEL = "claude-opus-4-8"


def die(msg: str, code: int = 1) -> "None":
    print(f"scanfile: {msg}", file=sys.stderr)
    raise SystemExit(code)


def base_dir(args) -> Path:
    raw = args.base_dir or os.environ.get("SCANFILE_BASE_DIR") or DEFAULT_BASE
    return Path(raw).expanduser()


def existing_folders(base: Path) -> List[str]:
    try:
        return sorted(p.name for p in base.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def sanitize_folder(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    out = []
    for w in words:
        # uppercase or lowercase tokens get title-cased; mixed-case tokens
        # (already Pascal/camelCase, e.g. "RAV4Docs") keep their interior caps.
        if w.isupper() or w.islower():
            out.append(w[:1].upper() + w[1:].lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return "".join(out) or "Unfiled"


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("/", "-").replace("\\", "-").replace(":", "-")
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120].strip() or "scan"


def build_name(filing) -> str:
    base = sanitize_filename(filing.filename)
    parts = [base]
    if filing.amount and filing.amount not in base:
        parts.append(f"${filing.amount}")
    if filing.date and filing.date not in base:
        parts.append(filing.date)
    return " ".join(parts) + ".pdf"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanfile",
        description="Scan a document, classify it with Claude, and file it under iCloud.",
    )
    p.add_argument("-d", "--duplex", action="store_true", help="scan both sides")
    p.add_argument("-r", "--res", type=int, default=int(os.environ.get("BRSCAN_RES", "300")),
                   help="resolution dpi (default 300)")
    p.add_argument("-c", "--color", default=os.environ.get("BRSCAN_COLOR", "color"),
                   help="color | gray | bw | auto (default color)")
    p.add_argument("-s", "--size", default=os.environ.get("BRSCAN_SIZE", "letter"),
                   choices=["letter", "a4", "legal", "max", "auto"])
    p.add_argument("--no-crop", dest="crop", action="store_false", default=True,
                   help="keep blank/gray padding")
    p.add_argument("--single", action="store_true",
                   help="scan one sheet and file it; skip the interactive multi-page loop")
    p.add_argument("--base-dir", help=f"destination root (default: {DEFAULT_BASE})")
    p.add_argument("--folder", help="force this destination folder; skip Claude's folder choice")
    p.add_argument("--model", default=os.environ.get("SCANFILE_MODEL", DEFAULT_MODEL),
                   help=f"Claude model (default {DEFAULT_MODEL}; set a cheaper one to economize)")
    p.add_argument("--dry-run", action="store_true",
                   help="scan and classify but don't move the file")
    p.add_argument("-H", "--host", default=os.environ.get("BRSCAN_HOST") or None)
    p.add_argument("-P", "--port", type=int, default=int(os.environ.get("BRSCAN_PORT", "0")) or None)
    p.add_argument("-w", "--wait", type=int, default=int(os.environ.get("BRSCAN_WAKE_SECS", "25")))
    p.add_argument("--retries", type=int, default=int(os.environ.get("BRSCAN_RETRIES", "3")))
    p.add_argument("--discover-secs", type=float, default=float(os.environ.get("BRSCAN_DISCOVER_SECS", "4")))
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from . import classify as classify_mod
    except ImportError:
        die("the AI extra is not installed. Install it with:\n"
            '  uv tool install --force "brscan[ai] @ git+https://github.com/JVenberg/brscan"\n'
            "or add anthropic to the existing tool:\n"
            "  uv tool install --force --with anthropic git+https://github.com/JVenberg/brscan")

    api_key = os.environ.get("SCANFILE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        die("no API key found. Set SCANFILE_API_KEY (preferred) or "
            "ANTHROPIC_API_KEY and retry.")

    base = base_dir(args)
    color = COLOR_MODES.get(args.color.lower(), args.color)
    settings = ScanSettings(res=args.res, color=color, duplex=args.duplex, size=args.size)

    scanner = find_scanner(host=args.host, port=args.port, wait=args.wait,
                           retries=args.retries, discover_secs=args.discover_secs,
                           verbose=args.verbose)
    if scanner is None:
        die("no scanner reachable. Power it on and ensure it's on the same network.")

    interactive = not args.single
    if interactive and not _have_tty():
        note("no terminal available for interactive mode; scanning a single sheet.")
        interactive = False

    mode = "duplex" if args.duplex else "simplex"
    note(f"scanning on {scanner.host}:{scanner.port}  "
         f"({color.split(':')[-1]} {args.res}dpi {mode}, {args.size})")

    pagedir = Path(tempfile.mkdtemp(prefix="scanfile-"))
    page_count = 0
    try:
        if interactive:
            note("multi-page mode: load a sheet and press Enter to scan it; "
                 "Esc or q to finish and file." +
                 (" (duplex: each sheet adds 2 pages)" if args.duplex else ""))
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
                scan_one_job(scanner, settings, pagedir, 0, args.wait)
            except Recoverable as exc:
                die(f"{exc}. Load a page and retry.")
            except Fatal as exc:
                die(str(exc))

        pages = sorted(pagedir.glob("page-*.jpg"))
        if not pages:
            die("no pages scanned.")
        if args.crop:
            for p in pages:
                crop(p)

        note(f"classifying {len(pages)} page(s) with {args.model}...")
        folders = [args.folder] if args.folder else existing_folders(base)
        filing = classify_mod.classify(pages, folders, model=args.model, api_key=api_key)
        if filing is None:
            die("Claude could not classify the document (no structured output).")

        folder = sanitize_folder(args.folder or filing.folder)
        dest_dir = base / folder
        dest = unique_path(dest_dir / build_name(filing))

        note(f"-> {filing.document_type}: {filing.summary}")

        tmp_pdf = pagedir / "out.pdf"
        assemble_pdf(tmp_pdf, pages)

        if args.dry_run:
            keep = Path(tempfile.gettempdir()) / dest.name
            shutil.copy(tmp_pdf, keep)
            note(f"[dry-run] would file as: {dest}")
            note(f"[dry-run] pdf left at:   {keep}")
            return 0

        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_pdf), str(dest))
        note(f"filed: {dest}")
        return 0
    finally:
        shutil.rmtree(pagedir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
