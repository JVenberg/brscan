# brscan

Trigger a scan on a **Brother DSmobile** portable scanner (DS-640 / DS-740D /
DS-940DW family) straight from the command line over **eSCL / Apple AirScan**,
and get the file back as a PDF or JPEG. No Brother drivers, no GUI, no cloud:
just `bash` + `curl`.

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
heavy and there's no official CLI. This is a ~250-line script that drives the
API directly, with the rough edges of these particular units smoothed over.

## Requirements

- `bash` and `curl` (preinstalled on macOS and most Linux).
- **mDNS discovery:** `dns-sd` (built into macOS) or `avahi-browse` (Linux:
  `sudo apt install avahi-utils`). Optional if you pass `-H <ip>`.
- **PDF output (optional):** one of [`img2pdf`](https://github.com/myollie/img2pdf)
  (best: embeds the JPEG losslessly, preserves DPI), `uvx img2pdf` (via
  [uv](https://github.com/astral-sh/uv)), or ImageMagick. Without any of these,
  brscan falls back to saving JPEG pages.
- **Auto-crop (optional):** ImageMagick (`magick`). Without it, scans are saved
  uncropped.

## Install

```sh
git clone https://github.com/JVenberg/brscan
install -m 0755 brscan/brscan /usr/local/bin/brscan   # or anywhere on $PATH
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
| `-s, --size SIZE` | `letter` / `a4` / `legal` / `max` / `auto` | letter |
| `--width N` / `--height N` | custom region, in 1/300 inch units | |
| `--no-crop` | keep blank/gray padding | crop on |
| `-H, --host HOST` | scanner IP or mDNS name | cache, then discovery |
| `-P, --port N` | eSCL port | discovered, else 8080 |
| `-w, --wait SECS` | how long to wait for a sleeping scanner | 25 |
| `-v, --verbose` | print request XML and HTTP details | |

### Examples

```sh
brscan                          # 300dpi color Letter PDF -> scan-<ts>.pdf
brscan -d -r 600 contract.pdf   # duplex, 600 dpi
brscan -c gray -f jpeg page.jpg # grayscale JPEG (one file per page)
brscan -s a4 --no-crop          # full A4 area, no trimming
brscan --discover               # locate + cache the scanner
brscan -H 192.168.1.50 --status # talk to a specific host
```

Every flag also has a `BRSCAN_*` environment variable (`BRSCAN_RES`,
`BRSCAN_COLOR`, `BRSCAN_HOST`, ...). See `brscan --help`.

## How it finds the scanner

Resolution order: **`-H`/`BRSCAN_HOST` → cached host → mDNS discovery**.

The discovered `.local` name (which encodes the scanner's MAC and therefore
survives DHCP lease changes) is cached at `~/.cache/brscan/scanner`. Later runs
use it instantly; if the cached host stops answering, brscan automatically
re-discovers and updates the cache.

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
  fixed, and verifies each page is a complete JPEG (retrying once if not).
- **It's sheet-fed.** Load one page at a time, top-edge first and centered. A
  flashing exclamation LED usually means a paper jam or unlatched cover.

## Tested on

Brother **DS-940DW** (firmware 2.03), macOS. The protocol is shared across the
DS-640 / DS-740D / DS-940DW family, so those should work too; reports welcome.

## License

MIT — see [LICENSE](LICENSE).
