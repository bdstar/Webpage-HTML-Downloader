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
- **Multiple URLs:** type one URL per line, or click **Upload .txt** to load a file with one URL per line.
  - Blank lines and `#` comments are ignored, and duplicates are removed. Up to 1,000 URLs can be loaded at once.
  - The **URL preview** lists every URL with its own progress bar and status: *Not initiated → Queued → Download progress (42%) → Download Complete* (or *Failed: reason* / *Cancelled*).
  - Downloads run **one at a time**. After each one finishes, the app waits for the **Download Interval Duration** before starting the next. The default is 2 seconds; any value from 0 to 3600 seconds is accepted and remembered. While it waits, the next row counts down ("Waiting 2s before starting"). Changing the value applies to the batch already running.
  - **Download all** starts or retries everything not yet complete, and **Cancel all** stops the batch.
  - After a reload, the list is restored and running downloads continue.
- **Browse Location:** choose the folder where files are saved. Type a path, or click **Browse…** to pick (or create) a folder; the choice is remembered. **Default** goes back to `downloads/`.
  - Because the app itself writes the files, it shows the folders on the computer running the app.
  - For safety, folder browsing and folder selection work only when the page is opened from that same computer (`127.0.0.1`). Other visitors always save to the default folder unless `ALLOW_REMOTE_FOLDER_SELECTION=true`.
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
- The programme importer, using real saved Mastersportal and Bachelorsportal pages in `tests/fixtures/portal/`:
  - Pagination URLs; classification of listing, programme, blocked and unrelated pages.
  - Every listing and detail field, including null handling.
  - Windows-safe file names; deduplication and merge rules; preservation of hand-added records.
  - Category mismatches, blocked pages and retry, and safe HTML deletion.
  - Pause, resume and stop, plus restart recovery and duplicate-task prevention.
  - The dashboard API, including log and to-save list exports.

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
| `ALLOW_REMOTE_FOLDER_SELECTION` | `false` | Let browsers on other machines browse folders and choose the download folder. **Leave off if the app is shared.** |
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

## Programme importer (Mastersportal and Bachelorsportal)

The **Programme importer** page (`http://127.0.0.1:5000/programmes`, linked from the top of the downloader page) builds UK Master's and Bachelor's programme datasets: one JSON file per university.

### Why it imports saved pages instead of crawling

Mastersportal and Bachelorsportal are protected by Cloudflare bot management. Automated clients are redirected to a limited `?bot=true` view without pagination, and any session that requests more than one page receives a `403 Attention Required` challenge. Getting past that would mean evading the sites' access controls, which this project does not do. Instead, you open pages in your normal browser, save them, and the importer does the rest offline. It never contacts the portals.

### Workflow

1. Run `python app.py` and open **Programme importer**.
2. Under **Pages still to save**, the dashboard lists the listing pages you have not imported yet. Page 1 has no parameter; later pages use `?page=N`. The total page count is read from the first listing page you import (for example 985 Master's pages and 1,168 Bachelor's pages). It also lists every programme that has listing data but no saved programme page. Both lists can be downloaded as `.txt`.
3. Open those pages in your browser and save them with **Ctrl+S** (either "Webpage, Complete" or "HTML only" works) into the category's import folder: `imports/master/` or `imports/bachelor/`.
   - **Export folder** sets where the university JSON files and `_index.json` are written. It defaults to `UK/<category>/programs/` (created when needed). Any other folder must already exist and be writable. Each task remembers its export folder.
   - In a custom export folder, a university's file is regenerated from the database whenever one of its programmes is imported. `_index.json` there lists only the files present in that folder.
4. Choose the category and press **Start import**. Listing pages are processed first, in page order, then programme pages.
5. Use **Pause**, **Resume**, **Stop**, **Retry failed**, **Refresh**, **View history** and **Export log** (JSON or CSV) as needed. Pause and stop take effect after the current file finishes.

### What happens to each file

| Result | Meaning | Source HTML |
|---|---|---|
| Imported | Data was saved to SQLite, the university JSON was rewritten and re-read successfully | Deleted, together with the browser's `<name>_files` folder, unless you untick "Delete each HTML file…" (or set `KEEP_IMPORTED_HTML=true`) |
| Blocked | The file is a Cloudflare/CAPTCHA page, not portal content | Kept. Save the page again from your browser, then press **Retry failed** |
| Skipped | The file belongs to the other category, has identical content to a file already imported, or is not a portal listing or programme page | Kept |
| Failed | Parsing or writing failed (the reason is shown in the file table and the log) | Kept. Fix the cause, then press **Retry failed** |

Each file is identified by its `window.PortalType`, canonical URL or "saved from url" comment, never by its file name. A Bachelor's page dropped into the Master's folder is therefore reported rather than mixed into the wrong dataset.

### Output

```
UK/master/programs/<University name>.json
UK/master/programs/_index.json          # every university file with programme counts
UK/bachelor/programs/<University name>.json
UK/bachelor/programs/_index.json
data/programmes.sqlite3                 # tasks, files, programmes, events (source of truth)
```

Each university file contains `university_name`, `category`, `portal`, `generated_at`, `programme_count` and `programs`. A programme record has:

- **From the listing card:** `university_name`, `university_location`, `program_name`, `program_type`, `cost` (as displayed, in the browser's currency), `program_url` (canonical, without `?bot=true`).
- **From the programme page:** `degree_type`, `tuition_fee`, `tuition_fee_details`, `delivery`, `official_url`, `duration`, `duration_notes`, `apply_date`, `application_deadline_notes`, `intakes`, `start_dates`, `field_of_study`, `fields_of_study`, `top_ranking_percent`, `ranking`, `language_score`, `language_score_details`, `credits`, `academic_requirements`, `other_requirements`.
  - `tuition_fee` has `international` and `domestic` entries, each `{amount, currency, per, note}`. Amounts are the institution's original amounts (for example GBP), not the converted amount the page displays.
  - `official_url` has its tracking parameters removed.
  - `language_score` holds `{duolingo, toefl, pte, ielts}`. When the portal lists TOEFL iBT as a 0–120 total plus a 1–6 band, the total is used and both values appear in `language_score_details`.
- **Status and provenance:** `study_id`, `category`, `detail_status` (`complete`, `listing_only` or `detail_only`), `extraction_warnings`, and `source`, which records the portal, listing page and position, file names, import times and task IDs.

Missing values are `null`. They are never guessed. Re-importing a page updates the record in place, keyed by study ID, so there are no duplicates. A `null` in a newer page never erases a value from an earlier page. Records that someone added to a university file by hand (whose URL the database does not know) are preserved. If an existing university file cannot be read as JSON, it is left untouched and the import of that page fails with an explanation.

### Restarts and recovery

Task state, per-file status, and every event are stored in SQLite. If the app stops mid-import, the task appears as **Interrupted** on the next start. **Resume** continues with the files that had not finished. Processing a file is idempotent, so one that was interrupted halfway is simply processed again.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PROGRAMME_IMPORT_ROOT` | `./imports` | Parent of the `master/` and `bachelor/` import folders |
| `PROGRAMME_OUTPUT_ROOT` | `.` | Root under which `UK/<category>/programs/` is written |
| `PROGRAMME_DATA_DIR` | `./data` | Location of `programmes.sqlite3` |
| `KEEP_IMPORTED_HTML` | `false` | Untick "delete HTML after import" by default |

The older single-file tool `python extract_programs.py page.html` (listing cards to `programs.json`) still works. The importer reuses its card parser.

## Universities importer

The **Universities importer** page (`http://127.0.0.1:5000/universities`) turns saved university profile pages (for example `https://www.mastersportal.com/universities/357/university-of-southampton.html`) into one JSON file per university. It uses the same task engine as the Programme importer (pause, resume, stop, retry, restart recovery, activity log and exports), but keeps its own history in `data/universities.sqlite3`.

### Workflow

1. Open a university's profile page in your normal browser and save it with **Ctrl+S** into `imports/university/` (or any folder you choose with **Browse…**).
2. Choose the **University Category**: *UK / Master's* (Mastersportal pages) or *UK / Bachelor's* (Bachelorsportal pages). As soon as a folder is entered, the page shows how many HTML files it contains and how many are university profile pages.
   - **Export Folder** sets where the JSON files are written. It defaults to `export/`; any other folder must already exist and be writable. Inside it, each university goes to a folder for its country (see Output). The page shows how many university JSON files the folder already holds.
3. Choose what to do when a university already has a JSON file: **Update existing records** (default), **Create new records only** or **Skip already imported records**.
4. Tick or untick **Delete each HTML file after its data has been saved and verified**, then press **Start Import**. Only one university import runs at a time.

For each file the importer:

1. Parses the saved file locally. It never downloads anything.
2. Validates the extracted record.
3. Writes the JSON atomically (temporary file, then replace) and re-reads it to confirm the content.
4. Records the result in SQLite.
5. Only then, if deletion is enabled, deletes the HTML file and its `<name>_files` folder.

If any step fails, including writing the task history, the HTML file is kept and the reason is shown. Use **Retry failed** once the cause is fixed. Pages from the other portal, Cloudflare/CAPTCHA pages, byte-identical copies and other HTML files are reported as skipped or blocked, never silently ignored.

### Output

```
export/UK/universities/<University name>.json        # United Kingdom
export/France/universities/<University name>.json    # every other country uses its own name
export/Unknown country/universities/...              # pages without a country (reported as a warning)
data/universities.sqlite3          # tasks, per-file history, university-to-file mapping, events
```

Master's and Bachelor's imports of the same university update the same file, which then holds both `number_master_program` and `number_bachelor_program`. Set `UNIVERSITY_EXPORT_ROOT` to change the default export folder.

```json
{
  "university": "University of Southampton",
  "location": "Southampton,",
  "country": "United Kingdom",
  "global_top": "2%",
  "worldwide_number": "96",
  "official_webiste": "https://www.southampton.ac.uk/",
  "number_master_program": "402",
  "qs_rank": "111",
  "institution_type": "Public"
}
```

| Field | Taken from |
|---|---|
| `university` | `span.OrganisationName[data-cy="OrganisationName"]`, without the "verified partner" badge |
| `location`, `country` | The `div.OrganisationLocation` spans. The `+N` venue count is ignored |
| `global_top` | `span.GlobalRank` ("Global Rank Top 2%" becomes `2%`) |
| `worldwide_number` | `span.GlobalRankTop` ("# 96 Worldwide" becomes `96`) |
| `official_webiste` | `a.UniversityWebsiteLink[data-description]`, with entities decoded and the query string removed. The tracking `href` is never used |
| `number_master_program` | The `QuickFactData` of the "Master's" quick fact, checked against the "Masters" key statistic |
| `qs_rank` | The `.LatestRanking` inside the ranking block titled "QS World University Rankings". U.S. News and ARWU ranks are ignored |
| `institution_type` | The key statistic labelled "Institution Type" |

Notes on the schema:

- **Field names.** `official_webiste` keeps the spelling the schema asked for; no other file in the project uses an `official_website` field. Bachelor's files use `number_bachelor_program` instead of `number_master_program`, because the count on a Bachelorsportal page is the number of Bachelor's programmes.
- **Values.** All values are strings or `null`. A missing value is `null` and is listed under "Missing fields" in the task, and the file counts as "imported with missing fields". The literal string `"null"` is never written.
- **File naming.** Files are named after the extracted university name, made safe for Windows: invalid characters are replaced and reserved names such as `CON` are prefixed. The mapping from the Studyportals university ID to the file is stored, so re-importing updates the same file.
  - If a different institution already owns that file name, the new one is written to `<name> (2).json` rather than overwriting it.
  - An unreadable existing JSON file is never overwritten.
- **Updates.** An update never replaces a stored value with `null`, and keys added by hand are kept.

| Variable | Default | Meaning |
|---|---|---|
| `UNIVERSITY_IMPORT_FOLDER` | `./imports/university` | Default import folder |

`PROGRAMME_OUTPUT_ROOT`, `PROGRAMME_DATA_DIR` and `KEEP_IMPORTED_HTML` apply to this importer too. Browsing or entering a folder other than the default is limited to the computer running the app, unless `ALLOW_REMOTE_FOLDER_SELECTION=true` is set.

## Responsible use and limitations

- The tool does not bypass logins, CAPTCHAs, paywalls, bot protection or rate limits. A `401`, `403` or `429` is reported to the user, not retried. Respect each site's terms of service and robots policies.
- Query parameters such as `?page=5` are sent exactly as entered. If the site redirects to a URL that drops one of them (for example `?page=5` becoming `?bot=true`), or if a paginated page reports a different `data-current-page`, the download fails with `content_mismatch` and nothing is saved. This keeps page 1 from being stored as if it were page 5. Mastersportal and Bachelorsportal do this to automated tools. For those sites, save the page from your browser and use the Programme importer.
- Cancellation is checked between chunks. A stalled server can therefore delay cancellation by up to `READ_TIMEOUT` seconds. In rendered mode, cancellation is checked between page-load stages.
- In rendered mode, every browser request (including sub-resources) is checked against the same SSRF policy, and the main document's redirect chain is checked again before anything is saved. Chromium does its own DNS resolution, though, so a rebinding attack against a *sub-resource* is mitigated but not fully pinned. Keep rendered mode disabled on servers reachable by untrusted users unless the host is network-isolated.
- Jobs are kept in memory. After a restart, earlier jobs are forgotten, but their saved files remain in `downloads/`.
- `python app.py` uses Flask's threaded development server, which is suitable for local or single-user use. For a shared deployment, run it behind a production WSGI server that supports streaming responses (for example `waitress-serve --call app:create_app`).
