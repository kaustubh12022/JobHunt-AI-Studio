# Automation Guide — driving JobHunt AI Studio

This studio is built to be operated by a browser agent (Playwright, Puppeteer, or a
vision/computer-use model) as well as by a human. One linear flow, stable labels,
plain HTML text, and a machine-readable state block.

- **Base URL:** https://jobhunt-ai-studio.onrender.com (or `http://localhost:8501`)
- **Auth:** owner passphrase in `STUDIO_ACCESS_KEY`

---

## 1. The four screens

| Screen | `<h1>` heading | `data-studio-state` | Primary control |
| :--- | :--- | :--- | :--- |
| Locked | `Owner access only` | – | `Unlock studio` |
| Ready | `Upload a jobs CSV` | `ready` | `Start batch` |
| Running | `Batch running` / `Batch paused` / `Batch starting` | `running` / `paused` | `Pause batch` |
| Done | `Batch complete` / `Batch failed` | `complete` / `failed` | `Download ZIP` |

The `<h1>` is the cheapest, most reliable screen detector:
`page.get_by_role("heading", name="Batch complete")`.

There are no tabs, no sidebar and no modal dialogs. Streamlit's header, menu and
toolbar are hidden by CSS, so the page contains only studio content.

---

## 2. Machine-readable state

Every unlocked screen starts with a status banner:

```html
<div class="status-banner"
     data-studio-state="complete"
     data-jobs-total="4"
     data-jobs-scored="4"
     data-approved="3"
     data-attention="1"
     data-zip="1"
     data-run-id="run_9f2c1a4b77de">
  <span class="status-label">STATUS</span>
  <strong class="status-word">COMPLETE</strong>
  <span class="status-detail">4 jobs processed · 3 approved PDFs · 1 need attention</span>
</div>
```

Read it without a screenshot:

```python
banner = page.locator("[data-studio-state]").first
state = banner.get_attribute("data-studio-state")   # ready | running | paused | complete | failed
approved = int(banner.get_attribute("data-approved") or 0)
has_zip = banner.get_attribute("data-zip") == "1"
```

The same data is printed as JSON in the **Machine-readable state (JSON)** panel at the
bottom of the ready / running / complete screens, if you prefer text extraction over
attributes.

---

## 3. Widget contract

| Purpose | Label (accessible name) | Streamlit `key` | Notes |
| :--- | :--- | :--- | :--- |
| Passphrase | `Owner passphrase` | `owner_passphrase` | `input[type=password]` |
| Unlock | `Unlock studio` | `unlock_studio` | |
| CSV file | `Jobs CSV` | `jobs_csv` | `input[type=file]` inside `[data-testid="stFileUploaderDropzone"]` |
| Paste CSV | `CSV text` | `csv_text` | inside the `Paste CSV text instead of a file` expander |
| Start | `Start batch` | `start_batch` | always clickable; explains what is missing if it cannot start |
| Pause | `Pause batch` | `pause_batch` | running screen only |
| Resume | `Resume batch` | `resume_batch` | paused / failed screens |
| ZIP | `Download ZIP` | `download_zip` | `jobhunt_tailored_batch.zip` |
| Summary CSV | `Download summary CSV` | `download_summary_csv` | `batch_applied_summary.csv` |
| HTML report | `Download HTML report` | `download_html_report` | `shortlisted_jobs_report.html` |
| Single PDF | `Download PDF` | `download_pdf` | for the job selected under `Inspect a job` |
| PDF check | `Check PDF` | `check_pdf` | local ATS audit of the selected PDF |
| Job picker | `Inspect a job` | `inspect_job` | one row per processed job |
| Start over | `Start a new batch` | `new_batch` | clears the workspace |
| Recovery | `Recovery code` + `Find run` | `recovery_code_input` / `find_run` | inside `Recovery, runs and lock` |
| Lock | `Lock studio` | `lock_studio` | inside `Recovery, runs and lock` |

Buttons are full-width. Primary actions are blue with white text.

---

## 4. Playwright recipe

```python
from playwright.sync_api import expect, sync_playwright

BASE = "https://jobhunt-ai-studio.onrender.com"
PASSPHRASE = "..."          # STUDIO_ACCESS_KEY
CSV = "C:/jobs/naukri_export.csv"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.goto(BASE, wait_until="domcontentloaded")

    # 1. Unlock
    page.locator('input[type="password"]').fill(PASSPHRASE)
    page.get_by_role("button", name="Unlock studio").click()
    expect(page.get_by_role("heading", name="Upload a jobs CSV")).to_be_visible(timeout=30_000)

    # 2. Upload
    page.locator('input[type="file"]').set_input_files(CSV)
    expect(page.locator('[data-testid="stAlertContentSuccess"]')).to_contain_text("loaded", timeout=30_000)

    # 3. Start
    page.get_by_role("button", name="Start batch").click()

    # 4. Wait for the batch (scoring + tailoring + review can take minutes)
    expect(page.get_by_role("heading", name="Batch complete")).to_be_visible(timeout=30 * 60_000)
    banner = page.locator("[data-studio-state]").first
    if banner.get_attribute("data-studio-state") != "complete":
        raise RuntimeError("batch failed")

    # 5. Download
    with page.expect_download() as info:
        page.get_by_role("button", name="Download ZIP").click()
    info.value.save_as("batch.zip")
```

Notes:

- Do **not** wait on `networkidle`; Streamlit keeps a websocket open. Wait on headings,
  buttons or the `data-studio-state` attribute instead.
- The first Render request after idle can take ~30–60 s to cold-start the free instance.
  Use a 60 s timeout for the first `goto`/heading.
- `set_input_files` works on the hidden `input[type=file]`; no need to click "Browse files".

---

## 5. Vision / computer-use guidance

- Read the **STATUS** banner at the top: `READY`, `RUNNING`, `COMPLETE`, `FAILED`.
- The only buttons that need clicking are full-width and labelled in plain English; the
  page has no icons-only controls.
- While a batch runs, the page re-renders about every 2 seconds. Areas below the activity
  log can be dimmed leftovers of the previous screen for a moment — ignore anything
  below the status banner while `STATUS` is `RUNNING`, and wait for `COMPLETE`.
- The results table is a plain HTML table (`Company`, `Role`, `Match`, `Challenger`,
  `Status`, `Location`) — read the rows directly, no canvas, no hover needed.
- The app hides Streamlit's header/menu, so there is no "Deploy" or "Settings" to avoid.

---

## 6. CSV input from any source

Accepted: LinkedIn/Naukri/Indeed exports, spreadsheet exports, scraped CSVs, or a
hand-written file. The uploader accepts `.csv`, `.tsv` and `.txt`. Requirements:

- **Required columns** (case-insensitive, synonyms accepted):
  company `company, company_name, employer, organization, hiring_organization, client`;
  title `title, job_title, role, position, designation, job_posting_title, vacancy`;
  description `job_description, description, jd_text, content, job_summary, role_description`.
- Optional: `job_url, apply_link, url, link, location, city, company_website`.
- **Delimiter** is detected: comma, semicolon, tab, or pipe.
- **Encoding** is detected: UTF-8 (with or without BOM), Windows-1252, Latin-1.
- Descriptions shorter than 50 characters are skipped with a row warning.
- Limits: 5 MB per file, 100 jobs per batch, 30,000 characters per description.

A valid sample is downloadable from **Recovery, runs and lock → Download sample jobs CSV**.

If the file cannot be parsed the page shows `Could not read CSV: ...` naming the missing
column and the detected headers.

---

## 7. Timing, pausing and recovery

- A single job takes roughly 30–90 s (score, research, draft, render, review, maybe revise).
- `Pause batch` stops after the current step; the run is checkpointed in SQLite.
- `Resume batch` continues from the last checkpoint without re-scoring.
- Every run gets a 12-character recovery code (shown in `Recovery, runs and lock`).
  Enter it there and press `Find run` to reload a batch — e.g. after a restart, since the
  free Render instance keeps no persistent disk.
- `Failed` batches keep their progress and the recovery controls.

---

## 8. Failure messages you may see

| Message | Meaning |
| :--- | :--- |
| `Upload a jobs CSV first.` | Start pressed with no file and no pasted CSV. |
| `Could not read CSV: Missing required column ...` | Header detection failed; the message lists the headers it saw. |
| `No valid jobs found. Check the row warnings below.` | Every row was blank or had a description under 50 characters. |
| `DEEPSEEK_API_KEY is not configured on this server.` | Server misconfiguration; nothing can run. |
| `Batch interrupted. ...` | The worker stopped; use `Resume batch`. |
| `Needs manual review` (results table) | The reviewer refused to approve the resume (for example a mandatory skill is absent from the profile). No PDF is exported — by design. |
