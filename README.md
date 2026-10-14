# Webpage HTML Downloader

A small Flask web app that downloads the HTML source of a public webpage. Progress is shown live in the browser, and the saved file can be downloaded back through the UI.

## Features

- **URL input, Download button, and live progress bar.** Progress is pushed with Server-Sent Events, and the page falls back to polling if SSE is unavailable.
  - When the server sends `Content-Length`, the bar shows a real percentage and "X of Y" bytes.
  - When the size is unknown, the bar shows an *indeterminate* animation. It never invents a percentage.
  - The bar reaches 100% only after the file has been written to its final location.
- **Status stages:** Initializing → Connecting → Downloading HTML (or Rendering page) → Saving file → Completed / Failed / Cancelled.
- **Result panel:** shows the filename, file size, full local path, final URL after redirects, and content type. It also has a **Download HTML file** button.
- **Cancellation:** works in both modes, and duplicate jobs for the same URL are prevented. If you reload the page, it reattaches to the running job.
- **Two modes:**
  - **Raw HTML** (default) streams the exact HTTP response body in chunks.
  - **Rendered DOM** (optional) uses Playwright and headless Chromium to save the HTML after JavaScript has run.

### What is *not* downloaded

Only the HTML document is saved. Stylesheets, scripts, images, fonts, iframes and API/XHR responses are separate requests and are **not** downloaded. A saved page may therefore look unstyled or incomplete when opened offline. Rendered mode captures the DOM as it stands after scripts have run, but it still does not save those asset files.

## Setup

Requires Python 3.10+.

```bash
cd webpage_html_downloader
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. Stop the app with `Ctrl+C`; run the same command to start it again. Saved files persist in `downloads/`.

### Optional: rendered (JavaScript) mode

```bash
pip install -r requirements-render.txt
python -m playwright install chromium
```

Then start the app with rendering enabled:

```powershell
# PowerShell
$env:ENABLE_BROWSER_RENDERING="true"; python app.py
```

```bash
# bash
ENABLE_BROWSER_RENDERING=true python app.py
```

Rendered mode starts a full browser for each job, so it is slower and uses far more memory than raw mode. A rendered page has no byte stream to measure, so the bar stays indeterminate while the page loads and the UI reports the current stage instead of a percentage.

## Running tests

```bash
python -m pytest
```

The tests start a throwaway local HTTP server and cover:

- URL validation and SSRF blocking, including DNS rebinding, redirects to private or metadata addresses, IPv4-mapped IPv6, and proxy environment variables.
- Successful downloads with known size, unknown size, gzip, and redirects.
- Progress calculation (no 100% until the file is saved; `None` when the size is unknown).
- Error handling: 403, 404, 429 (with `Retry-After`), 500, empty and 204 responses, non-HTML content, size limits, truncated transfers, read timeouts, refused connections, DNS failures, permission errors, and cancellation.
- File creation: safe names, no overwrites, concurrent finalisation, and no leftover temp files.
- The Flask API and SSE stream end to end.

Tests that need the local server allow `127.0.0.1` explicitly through `allowed_networks`. Other tests check that loopback is blocked by default.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Address the web UI listens on |
| `DOWNLOAD_DIR` | `./downloads` | Where HTML files are saved |
| `CONNECT_TIMEOUT` | `10` | Seconds to establish a connection |
| `READ_TIMEOUT` | `20` | Max seconds without receiving data |
| `TOTAL_TIMEOUT` | `120` | Max seconds for a whole download |
| `MAX_DOWNLOAD_BYTES` | `26214400` (25 MB) | Response size limit (checked against both `Content-Length` and actual bytes) |
| `MAX_REDIRECTS` | `5` | Redirect hops allowed |
| `ALLOWED_PORTS` | `80,443,8080,8443` | Destination ports allowed; `*` for any |
| `ALLOW_NON_HTML` | `false` | Accept non-HTML content types |
| `MAX_ACTIVE_JOBS` | `4` | Concurrent downloads |
| `USER_AGENT` | identifies the tool | User-Agent header sent to sites |
| `ENABLE_BROWSER_RENDERING` | `false` | Enable Playwright rendered mode |
| `RENDER_TIMEOUT` / `RENDER_IDLE_TIMEOUT` | `45` / `10` | Page-load and network-idle waits (seconds) |
| `ALLOWED_NETWORKS` | *(empty)* | CIDRs exempted from the private-address block. **Leave empty in production.** |

## How it works

```
app.py                  Flask routes: UI, job API, SSE stream, file download
downloader/
  security.py           URL validation, IP policy, DNS-pinned requests session
  fetcher.py            Streaming download, redirects, limits, error mapping
  renderer.py           Optional Playwright rendering with per-request SSRF guard
  jobs.py               Background jobs, progress state, duplicate/concurrency control
  filenames.py          Safe filenames and atomic, non-overwriting finalisation
  config.py             Settings from environment variables
templates/index.html    UI
static/js/app.js        SSE/polling client, progress rendering, cancel, reattach
static/css/style.css    Styles (determinate + indeterminate progress bar)
tests/                  pytest suite
```

### API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs` `{"url": "...", "mode": "http"\|"rendered"}` | Start a job. Returns `202`. Returns `409` with the existing job if the same URL and mode are already downloading, `400` for an invalid or blocked URL, and `429` if too many jobs are running. |
| `GET` | `/api/jobs/<id>` | Job snapshot (for polling) |
| `GET` | `/api/jobs/<id>/events` | SSE stream of job snapshots |
| `POST` | `/api/jobs/<id>/cancel` | Request cancellation |
| `GET` | `/api/jobs/<id>/file` | Download the saved HTML (`409` until completed) |

### Download pipeline (raw mode)

1. The URL is validated: only `http`/`https`, no embedded credentials, an allowed port, a well-formed host, and no `localhost` or private IP literals.
2. The request is sent with redirects **disabled**. Each `Location` is resolved and validated again, up to `MAX_REDIRECTS` hops.
3. The connection layer resolves the hostname and rejects it if **any** resolved address is loopback, private, link-local, CGNAT, multicast, reserved, unspecified, or an IPv4-mapped/6to4/Teredo form of one. It then connects to that already-validated IP, so DNS rebinding cannot swap the address. TLS certificates are still checked against the real hostname. Proxy environment variables are ignored.
4. The response status and content type are checked, and `Content-Length` is compared with the size limit.
5. The body is streamed in chunks into a hidden `.part` temp file inside `downloads/`. Progress is measured in bytes received on the wire, so it matches `Content-Length` even for gzip responses; the decoded size is limited as well.
6. If the transfer is truncated, empty, over the limit, timed out, cancelled, or fails in any other way, the temp file is deleted.
7. On success the temp file is fsynced and atomically renamed to `<host>_<path>_<YYYYMMDD-HHMMSS>_<random>.html`. If that name already exists, `-1`, `-2`, … is appended rather than overwriting.

### Browser disconnections

Downloads run in background threads that are independent of the HTTP request. Closing or reloading the tab does not stop a download. The SSE stream sends keep-alives, so a dead connection is detected and its generator ends. On reload, the page reattaches to the active job using `sessionStorage`. To stop a download, use **Cancel**.

## Responsible use and limitations

- The tool does not bypass logins, CAPTCHAs, paywalls, bot protection or rate limits. A `401`, `403` or `429` is reported to the user, not retried. Respect each site's terms of service and robots policies.
- Cancellation is checked between chunks. A stalled server can therefore delay cancellation by up to `READ_TIMEOUT` seconds. In rendered mode, cancellation is checked between page-load stages.
- In rendered mode, every browser request (including sub-resources) is checked against the same SSRF policy, and the main document's redirect chain is checked again before anything is saved. Chromium does its own DNS resolution, though, so a rebinding attack against a *sub-resource* is mitigated but not fully pinned. Keep rendered mode disabled on servers reachable by untrusted users unless the host is network-isolated.
- Jobs are kept in memory. After a restart, earlier jobs are forgotten, but their saved files remain in `downloads/`.
- `python app.py` uses Flask's threaded development server, which is suitable for local or single-user use. For a shared deployment, run it behind a production WSGI server that supports streaming responses (for example `waitress-serve --call app:create_app`).
