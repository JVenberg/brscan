# brscan

Trigger a scan on a **Brother DSmobile** portable scanner (DS-640 / DS-740D /
DS-940DW family) straight from the command line over **eSCL / Apple AirScan**,
and get the file back as a PDF or JPEG. No Brother drivers, no GUI, no cloud:
just a single Python CLI you install with [uv](https://github.com/astral-sh/uv).

```console
$ brscan contract.pdf
brscan: discovering scanner via mDNS (_uscan._tcp)...
brscan: found BRxxxxxxxxxxxx.local:8080
brscan: scanning on BRxxxxxxxxxxxx.local:8080  (RGB24 300dpi simplex, letter -> pdf)
brscan: received page 1 (3133208 bytes)
brscan: saved /path/to/contract.pdf  (1 page(s))
```

## Why

The Brother DSmobile scanners expose a perfectly good network scan API (the
same eSCL/AirScan protocol macOS and iOS use), but the desktop software is
heavy and there's no official CLI. This is a small Python CLI that drives the
API directly, with the rough edges of these particular units smoothed over.

## Requirements

- **[uv](https://github.com/astral-sh/uv)** (it provides Python and isolates the
  install). That's the only thing you need on the system.

Everything else is a Python dependency that uv installs into an isolated
environment: [`zeroconf`](https://github.com/python-zeroconf/python-zeroconf)
for mDNS discovery, `requests` for the HTTP flow, and
[`img2pdf`](https://github.com/myollie/img2pdf) + [`Pillow`](https://python-pillow.org/)
for PDF assembly and auto-crop. No `dns-sd`/`avahi`, no `curl`, no ImageMagick:
the dependency wheels bundle their own native codecs.

## Install

```sh
uv tool install git+https://github.com/JVenberg/brscan
```

This puts a `brscan` executable on your PATH (`~/.local/bin`) in its own isolated
environment. To update or remove it:

```sh
uv tool upgrade brscan
uv tool uninstall brscan
```

Or run it without installing:

```sh
uvx --from git+https://github.com/JVenberg/brscan brscan --status
```

## Usage

```
brscan [options] [output-file]
brscan --status            show scanner status (idle/busy, jobs)
brscan --caps              dump raw ScannerCapabilities XML
brscan --discover          find a scanner via mDNS and cache it
brscan --forget            clear the cached scanner
```

| Option | Description | Default |
| --- | --- | --- |
| `-o, --out FILE` | output file | `scan-<timestamp>.<ext>` |
| `-r, --res DPI` | 100 / 150 / 200 / 300 / 400 / 600 | 300 |
| `-c, --color MODE` | `color` / `gray` / `bw` / `auto` | color |
| `-f, --format FMT` | `pdf` / `jpeg` | pdf |
| `-d, --duplex` | scan both sides (duplex ADF) | off |
| `-i, --interactive` | multi-page: scan a sheet, Enter for the next, Esc/q to finish | off |
| `-s, --size SIZE` | `letter` / `a4` / `legal` / `max` / `auto` | letter |
| `--width N` / `--height N` | custom region, in 1/300 inch units | |
| `--no-crop` | keep blank/gray padding | crop on |
| `-H, --host HOST` | scanner IP or mDNS name | cache, then discovery |
| `-P, --port N` | eSCL port | discovered, else 8080 |
| `-w, --wait SECS` | how long to wait for a sleeping scanner | 25 |
| `--retries N` | network retry attempts per request (with backoff) | 3 |
| `-v, --verbose` | print request XML and HTTP details | |

### Examples

```sh
brscan                          # 300dpi color Letter PDF -> scan-<ts>.pdf
brscan -d -r 600 contract.pdf   # duplex, 600 dpi
brscan -i report.pdf            # multi-page: Enter scans each sheet, Esc finishes
brscan -i -d report.pdf         # multi-page duplex (2 pages per sheet)
brscan -c gray -f jpeg page.jpg # grayscale JPEG (one file per page)
brscan -s a4 --no-crop          # full A4 area, no trimming
brscan --discover               # locate + cache the scanner
brscan -H 192.168.1.50 --status # talk to a specific host
```

Every flag also has a `BRSCAN_*` environment variable (`BRSCAN_RES`,
`BRSCAN_COLOR`, `BRSCAN_HOST`, ...). See `brscan --help`.

## Multi-page documents

These are single-sheet feeders, so a multi-page document means feeding one sheet
at a time. `-i`/`--interactive` makes that one command:

```console
$ brscan -i report.pdf
brscan: interactive multi-page mode: load a sheet and press Enter to scan it; Esc or q to finish.
brscan: 0 page(s) captured. Press Enter to scan the next sheet, Esc or q to finish:
brscan: received page 1 (3110402 bytes)
brscan: 1 page(s) captured. Press Enter to scan the next sheet, Esc or q to finish:
brscan: received page 2 (2980173 bytes)
brscan: 2 page(s) captured. Press Enter to scan the next sheet, Esc or q to finish:
brscan: saved report.pdf  (2 page(s))
```

Each Enter scans whatever is in the feeder and appends it; **Esc** (or `q`)
finishes and assembles every page into the one output file. Combine with
`-d`/`--duplex` to add two pages (front and back) per sheet. If you press Enter
with no sheet loaded, brscan says so and re-prompts rather than giving up.

## `scanfile`: scan, classify, and auto-file with Claude

`scanfile` is an optional companion that scans a document, sends the page
image(s) to **Claude** to figure out what it is, and drops the resulting PDF into
the right folder under your iCloud Documents (or any base directory) with a
descriptive name. It is fully automatic: scan a receipt and it lands in
`RECEIPTS/Safeway grocery receipt $71.29 2026-06-12.pdf` without you typing a
name or picking a folder.

It learns your filing scheme from the folders you already have: it lists the
existing subdirectories of the base directory and tells Claude to reuse one when
it fits, only proposing a new folder when none do.

### Install (needs the `ai` extra)

The Anthropic SDK is an optional dependency, so install the `ai` extra:

```sh
uv tool install --force "brscan[ai] @ git+https://github.com/JVenberg/brscan"
```

`scanfile` reads your API key from `SCANFILE_API_KEY` (a dedicated variable so it
won't collide with an `ANTHROPIC_API_KEY` your other tools rely on; it falls back
to `ANTHROPIC_API_KEY` if `SCANFILE_API_KEY` is unset):

```sh
export SCANFILE_API_KEY=sk-ant-...
```

### Usage

```sh
scanfile                 # scan one sheet, classify, file under iCloud Documents
scanfile -d              # duplex (front + back) into one PDF, then file
scanfile --dry-run       # scan + classify, print where it WOULD go, don't move
scanfile --folder TAXES  # force the folder; Claude still names the file
scanfile --model claude-haiku-4-5   # use a cheaper model to economize
```

| Option | Description | Default |
| --- | --- | --- |
| `-d, --duplex` | scan both sides | off |
| `-r, --res` / `-c, --color` / `-s, --size` / `--no-crop` | same as `brscan` | 300 / color / letter / crop on |
| `--base-dir DIR` | destination root | `~/Library/Mobile Documents/com~apple~CloudDocs/Documents` |
| `--folder NAME` | force this folder, skip Claude's folder choice | Claude decides |
| `--model ID` | Claude model (`SCANFILE_MODEL` env) | `claude-opus-4-8` |
| `--dry-run` | classify but don't move; leave the PDF in `/tmp` | off |
| `-H` / `-P` / `-w` / `--retries` / `--discover-secs` / `-v` | same as `brscan` | |

The base directory is also settable with `SCANFILE_BASE_DIR`. The default
`claude-opus-4-8` gives the best classification; set `--model`/`SCANFILE_MODEL`
to a cheaper model (e.g. `claude-haiku-4-5`) to trade some accuracy for cost.

Page images are downscaled and sent to the Messages API, and Claude returns a
structured result (document type, folder, vendor, date, amount, summary, and a
base filename) that drives the destination path. Scans never leave your machine
except as that one classification request.

## How it finds the scanner

Resolution order: **`-H`/`BRSCAN_HOST` → cached host → mDNS discovery**.

Discovery uses `zeroconf` (pure Python, so no `dns-sd`/`avahi` needed). The
discovered `.local` name (which encodes the scanner's MAC and therefore survives
DHCP lease changes) is cached at `~/.cache/brscan/scanner`. Later runs use it
instantly; if the cached host stops answering, brscan automatically re-discovers
and updates the cache.

## The reverse-engineered protocol (eSCL / AirScan)

The scanner advertises `_uscan._tcp` over mDNS with `rs=eSCL` and a SRV record
pointing at `<host>:<port>`. The scan flow is:

1. `POST http://<host>:<port>/eSCL/ScanJobs` with an XML `ScanSettings` body
   → `201 Created` with a `Location: /eSCL/ScanJobs/<id>` header.
2. `GET .../eSCL/ScanJobs/<id>/NextDocument` → `200` with one JPEG per page.
   Repeat until `404` (job complete).

No authentication is required for the eSCL endpoints. (The web-admin password
only guards the EWS configuration UI.)

### Gotchas these units have (and how brscan handles them)

- **The scan API is on port 8080, not 80.** Port 80 is the EWS admin server; it
  *mirrors* the read-only eSCL GETs but `POST /eSCL/ScanJobs` there returns 404.
  Always use the port from the SRV record (8080 on the units tested).
- **They only emit JPEG.** `ScannerCapabilities` advertises `application/pdf`,
  but the mDNS `pdl` list (and reality) is `image/jpeg` only. brscan requests
  JPEG and assembles the PDF client-side.
- **They sleep aggressively and drop Wi-Fi.** A scanner that sleeps mid-transfer
  truncates the JPEG (no trailing `FFD9`, gray tail). brscan wakes/polls first,
  sends an explicit `ScanRegion` with `MustHonor=true` so the JPEG length is
  fixed, and verifies each page is a complete JPEG. Transient network errors and
  truncated pages are retried with exponential backoff (`--retries`, default 3);
  if the scanner vanishes mid-job it is woken and the job is retried, and a drop
  is never mistaken for "end of document".
- **It's sheet-fed.** Load one page at a time, top-edge first and centered. A
  flashing exclamation LED usually means a paper jam or unlatched cover.

## Tested on

Brother **DS-940DW** (firmware 2.03), macOS. The protocol is shared across the
DS-640 / DS-740D / DS-940DW family, so those should work too; reports welcome.

## License

MIT — see [LICENSE](LICENSE).
