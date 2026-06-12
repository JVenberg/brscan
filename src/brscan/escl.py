"""eSCL / Apple AirScan client for Brother DSmobile scanners.

Reverse-engineered flow:
  1. POST /eSCL/ScanJobs (XML ScanSettings) -> 201 + Location: /eSCL/ScanJobs/<id>
  2. GET  .../NextDocument -> 200 with one JPEG per page; repeat until 404.

Device quirks handled here:
  * The scan API lives on the eSCL port (8080 on the units tested), NOT port 80
    -- the EWS admin server on :80 mirrors the read-only GETs but POSTs 404 there.
  * These units only emit JPEG (the application/pdf in ScannerCapabilities is a
    template artifact); the PDF is assembled client-side.
  * They are battery/USB powered and sleep aggressively, dropping Wi-Fi mid
    transfer -- a truncated JPEG has no trailing FFD9 marker, so we detect it and
    retry. An explicit ScanRegion with MustHonor=true fixes the JPEG length.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

ESCL_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"

# width, height in 1/300 inch units
PAGE_SIZES = {
    "letter": (2550, 3300),
    "a4": (2480, 3508),
    "legal": (2550, 4200),
    "max": (2550, 4200),
}

COLOR_MODES = {
    "color": "RGB24",
    "rgb": "RGB24",
    "gray": "Grayscale8",
    "grey": "Grayscale8",
    "grayscale": "Grayscale8",
    "bw": "BlackAndWhite1",
    "mono": "BlackAndWhite1",
    "auto": "scan:AutoColorDetection",
}


class Recoverable(Exception):
    """No paper / busy / empty feeder: caller may retry after loading a sheet."""


class Fatal(Exception):
    """Unrecoverable: wrong port, malformed response."""


class Unreachable(Fatal):
    """Network failure talking to the scanner (it may have slept/dropped Wi-Fi)."""


@dataclass
class ScanSettings:
    res: int = 300
    color: str = "RGB24"
    duplex: bool = False
    intent: str = "Document"
    size: str = "letter"
    width: Optional[int] = None
    height: Optional[int] = None

    def _region_xml(self) -> str:
        if self.size == "auto" and not self.width and not self.height:
            return ""
        w = self.width or PAGE_SIZES.get(self.size, (None, None))[0]
        h = self.height or PAGE_SIZES.get(self.size, (None, None))[1]
        if not w or not h:
            return ""
        return (
            '<pwg:ScanRegions pwg:MustHonor="true">'
            "<pwg:ScanRegion>"
            "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
            f"<pwg:Height>{h}</pwg:Height>"
            f"<pwg:Width>{w}</pwg:Width>"
            "<pwg:XOffset>0</pwg:XOffset>"
            "<pwg:YOffset>0</pwg:YOffset>"
            "</pwg:ScanRegion>"
            "</pwg:ScanRegions>"
        )

    def to_xml(self) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:pwg="{PWG_NS}" xmlns:scan="{ESCL_NS}">
  <pwg:Version>2.63</pwg:Version>
  <scan:Intent>{self.intent}</scan:Intent>
  {self._region_xml()}
  <pwg:InputSource>Feeder</pwg:InputSource>
  <scan:Duplex>{str(self.duplex).lower()}</scan:Duplex>
  <scan:ColorMode>{self.color}</scan:ColorMode>
  <scan:XResolution>{self.res}</scan:XResolution>
  <scan:YResolution>{self.res}</scan:YResolution>
  <scan:BlankPageDetection>true</scan:BlankPageDetection>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>"""


def jpeg_complete(path: Path) -> bool:
    """A complete JPEG ends with the EOI marker FFD9."""
    try:
        if path.stat().st_size < 2:
            return False
        with open(path, "rb") as f:
            f.seek(-2, 2)
            return f.read() == b"\xff\xd9"
    except OSError:
        return False


class Scanner:
    def __init__(self, host: str, port: int = 8080, verbose: bool = False,
                 log=None, note=None, retries: int = 3, backoff: float = 1.5):
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}/eSCL"
        self.verbose = verbose
        self._log_fn = log
        self._note_fn = note
        self.retries = max(0, retries)
        self.backoff = backoff
        self.session = requests.Session()

    def _log(self, msg: str) -> None:
        if self.verbose and self._log_fn:
            self._log_fn(msg)

    def _notify(self, msg: str) -> None:
        if self._note_fn:
            self._note_fn(msg)
        else:
            self._log(msg)

    def reachable(self, timeout: float = 4.0) -> bool:
        try:
            r = self.session.get(f"{self.base}/ScannerStatus", timeout=timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def wait_until_reachable(self, secs: float) -> bool:
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if self.reachable(4):
                return True
            time.sleep(2)
        return False

    def status(self) -> str:
        r = self.session.get(f"{self.base}/ScannerStatus", timeout=15)
        r.raise_for_status()
        return r.text

    def capabilities(self) -> str:
        r = self.session.get(f"{self.base}/ScannerCapabilities", timeout=15)
        r.raise_for_status()
        return r.text

    def create_job(self, settings: ScanSettings) -> str:
        body = settings.to_xml()
        self._log("request body:\n" + body)
        delay = self.backoff
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                r = self.session.post(
                    f"{self.base}/ScanJobs",
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml"},
                    timeout=60,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if last:
                    raise Unreachable(f"could not reach scanner at {self.host}:{self.port}") from exc
                self._notify(f"network error contacting scanner; retrying in {delay:.0f}s "
                             f"({attempt + 1}/{self.retries})...")
                time.sleep(delay)
                delay *= 2
                continue

            self._log(f"POST /eSCL/ScanJobs -> {r.status_code}")
            if r.status_code == 201:
                break
            if r.status_code == 409:
                raise Recoverable("no sheet in the feeder (HTTP 409)")
            if r.status_code == 503:
                if last:
                    raise Recoverable("scanner busy / in use by another client (HTTP 503)")
                self._notify(f"scanner busy (HTTP 503); retrying in {delay:.0f}s "
                             f"({attempt + 1}/{self.retries})...")
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 404:
                raise Fatal(
                    f"ScanJobs not found on {self.host}:{self.port}. "
                    "Wrong eSCL port? (try -P 8080)"
                )
            raise Fatal(f"ScanJobs creation failed (HTTP {r.status_code})")

        loc = r.headers.get("Location")
        if not loc:
            raise Fatal("no Location header returned; cannot fetch document")
        if loc.startswith("http"):
            job = loc
        elif loc.startswith("/"):
            job = f"http://{self.host}:{self.port}{loc}"
        else:
            job = f"http://{self.host}:{self.port}/{loc}"
        self._log(f"job: {job}")
        return job

    def fetch_page(self, job_url: str, dest: Path, max_total: float = 300.0) -> str:
        """Fetch one page.

        Returns 'ok' (complete page), 'incomplete' (got a 200 but a truncated
        JPEG, kept anyway), 'done' (HTTP 404/non-200 => job finished), or
        'error' (network failure, nothing usable). A network failure is NOT
        treated as end-of-job; it is retried first, so a transient drop does not
        silently cut the document short.
        """
        delay = self.backoff
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            start = time.monotonic()
            try:
                with self.session.get(
                    f"{job_url}/NextDocument", stream=True, timeout=(10, 60)
                ) as r:
                    code = r.status_code
                    if code != 200:
                        self._log(f"GET NextDocument -> {code} (0 bytes)")
                        return "done"
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(65536):
                            if chunk:
                                f.write(chunk)
                            if time.monotonic() - start > max_total:
                                break
            except requests.RequestException as exc:
                self._log(f"GET NextDocument -> error: {exc}")
                if not last:
                    self._notify(f"network error fetching page; retrying in {delay:.0f}s "
                                 f"({attempt + 1}/{self.retries})...")
                    time.sleep(delay)
                    delay *= 2
                    continue
                if dest.exists() and dest.stat().st_size > 0:
                    return "incomplete"
                if dest.exists():
                    dest.unlink()
                return "error"

            size = dest.stat().st_size if dest.exists() else 0
            self._log(f"GET NextDocument -> 200 ({size} bytes)")
            if size == 0:
                if dest.exists():
                    dest.unlink()
                return "done"
            if jpeg_complete(dest):
                return "ok"
            if not last:
                self._notify(f"page came back truncated; retrying in {delay:.0f}s "
                             f"({attempt + 1}/{self.retries})...")
                time.sleep(delay)
                delay *= 2
                continue
            return "incomplete"
        return "incomplete"
