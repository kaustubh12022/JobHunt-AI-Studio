# JobHunt AI Studio

[![Private Studio](https://img.shields.io/badge/Access-Owner--only-111827?logo=lock&logoColor=white)](#-locked-by-design)
[![Live App](https://img.shields.io/badge/Render-Live_App-2563eb?logo=render&logoColor=white)](https://jobhunt-ai-studio.onrender.com)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/AI-DeepSeek_V4.1_Flash-4d6bfe)](https://platform.deepseek.com/)
[![Playwright](https://img.shields.io/badge/PDF-Playwright_Chromium-green.svg?logo=playwright&logoColor=white)](https://playwright.dev/)
[![Tests](https://img.shields.io/badge/Tests-66_Passed-success.svg)](https://github.com/kaustubh12022/JobHunt-AI-Studio)

**JobHunt AI Studio** is the private, single-owner resume tailoring and batch job-evaluation
pipeline built for **Kaustubh Kale**. It scores an uploaded batch of job listings against his
master profile, researches the shortlisted companies, drafts and renders tailored single-page
ATS resumes, sends every rendered PDF through an independent DeepSeek review, and packages only
the approved resumes into a downloadable bundle.

---

## 🌐 Live Studio

👉 **[https://jobhunt-ai-studio.onrender.com](https://jobhunt-ai-studio.onrender.com)**

The deployment is locked: a visitor must present the owner passphrase before any profile,
workspace or run data is rendered.

---

## 📖 Table of Contents
1. [Locked by Design](#-locked-by-design)
2. [Using the Studio](#-using-the-studio)
3. [Owner Profile](#-owner-profile)
4. [Pipeline](#-pipeline)
5. [Jobs CSV Format](#-jobs-csv-format)
6. [Local Setup](#-local-setup)
7. [Running Tests](#-running-tests)
8. [Production Deployment (Render)](#-production-deployment-render)
9. [Security Model](#-security-model)
10. [Project Structure](#-project-structure)

---

## 🖥️ Using the Studio

The interface is one linear flow with four plain screens, built to be driven by a human or
by an automated browser agent:

| Screen | Heading | You can |
| :--- | :--- | :--- |
| Locked | `Owner access only` | Enter the owner passphrase (`Unlock studio`). |
| Ready | `Upload a jobs CSV` | Upload a CSV (or paste CSV text) and press `Start batch`. |
| Running | `Batch running` | Watch progress and the activity log, or `Pause batch`. |
| Done | `Batch complete` | Download the ZIP / summary CSV / HTML report, or `Start a new batch`. |

There are no tabs, sliders, key fields or profile editors. Every screen shows a **STATUS**
banner (`READY`, `RUNNING`, `PAUSED`, `COMPLETE`, `FAILED`) with machine-readable
`data-studio-state` / `data-jobs-total` / `data-approved` attributes, plus a JSON snapshot
panel. Results are rendered as a plain HTML table, so every value is readable from the DOM.

The full automation contract — selectors, labels, CSV variants, timings and a Playwright
recipe — lives in [`AGENT_GUIDE.md`](AGENT_GUIDE.md).

---

## 🔒 Locked by Design

The original open studio let any visitor enter an API key, upload a profile and run batches.
That model has been removed completely:

| Concern | Locked behaviour |
| :--- | :--- |
| **Entry** | Owner passphrase (`STUDIO_ACCESS_KEY`) required before anything private renders. Fails closed if unset. |
| **Candidate profile** | Bundled at [`data/profile.yaml`](data/profile.yaml); reloaded on every run. No upload, editor, sample switch or browser restore. |
| **AI provider** | DeepSeek only. No provider, model, tier or quota controls. |
| **API keys** | One server-owned `DEEPSEEK_API_KEY`. No key inputs, no per-session keys, no local storage of credentials. |
| **Batch policy** | Fixed in [`core/settings.py`](core/settings.py): 70% match threshold, 20 resumes, 3 drafts, 85/100 approval. No sliders. |
| **Persistence** | No "remember me" / browser profile or credential storage. |

Changing the identity, engine or policy now requires editing repository code and redeploying —
not clicking around in the web UI.

---

## 👤 Owner Profile

[`data/profile.yaml`](data/profile.yaml) is the single source of truth for every score and every
tailored resume. It contains Kaustubh's real, verified information:

- **Identity** — Kaustubh Kale · Pune, Maharashtra, India · `kaustubh.kale.work@gmail.com`
- **Education** — B.E. Information Technology, SKNSITS Lonavala (SPPU), 2022–2026, CGPA 7.51/10
- **Experience** — Java Developer Intern, CWIPedia Technologies (Jan–Feb 2025)
- **Projects (6)** — SmartApply, CampFlow, Neon-Pulse, Personal AI OS, MedFlow, C54-Casino
- **Skills (60+)** — Java, Python, SQL, JavaScript, TypeScript, Spring Boot, Hibernate/JPA,
  React, Next.js, Node.js, Express, MongoDB, FastAPI, LangChain, DeepSeek API, Prompt
  Engineering, RAG, MCP tool calling, AsyncIO, Pandas, Docker-adjacent tooling, Git, Azure
  Fundamentals, and more, grouped into Core Languages, Java & Backend, MERN & Frontend,
  Applied AI & Automation, CS Fundamentals, and Cloud/DevOps/Tools
- **Certifications** — Microsoft Azure Fundamentals (AZ-900), Google Cybersecurity Certificate
- **Target roles** — Java Developer, Software Engineer, Backend Developer, Full Stack Developer,
  AI / GenAI Developer

The **Master profile (read-only)** panel at the bottom of the workspace renders all of it and
offers a YAML download.

---

## ⚙️ Pipeline

1. **Validate** — strict CSV parsing (5 MB, 100 jobs, 30k chars per JD, stable job IDs).
2. **Score** — DeepSeek Flash returns a 0–100 match score, a 10–14 word reason, a role summary,
   genuinely missing skills and a testing-role flag.
3. **Research** — bounded, SSRF-hardened fetch of the posting and official company pages.
4. **Tailor** — DeepSeek rewrites the summary, skills and bullet points strictly inside the
   owner profile; a sanitizer rejects invented employers, degrees, projects and skills.
5. **Render** — Playwright renders a single-page A4 PDF with JavaScript disabled.
6. **Check** — local ATS audit (one page, A4 bounds, no clipping, readable text, clean URLs).
7. **Challenge** — DeepSeek independently reviews the actual PDF text *and* a page image, scoring
   requirements, factual accuracy, layout, company match and writing. Below 85/100 or any
   blocking finding triggers a revision, up to 3 drafts.
8. **Package** — only approved, hash-verified resumes are bundled with the CSV/HTML reports.

Runs are checkpointed in SQLite; an interrupted batch can be resumed with its recovery code
without re-paying for completed scoring.

---

## 📋 Jobs CSV Format

```csv
company_name,job_role,job_description,direct_apply_link,location
```

| Column | Status | Accepted headers |
| :--- | :---: | :--- |
| **Company** | Required | `company`, `company_name`, `employer`, `organization`, `hiring_organization`, `client`, `brand` |
| **Job title** | Required | `title`, `job_title`, `role`, `position`, `designation`, `job_posting_title`, `vacancy` |
| **Description** | Required | `job_description`, `jd_text`, `description`, `content`, `job_summary`, `role_description` |
| **Apply URL** | Optional | `url`, `apply_link`, `apply_url`, `job_url`, `job_link` |
| **Company website** | Optional | `company_website`, `official_website` |
| **Location** | Optional | `location`, `city`, `work_mode`, `work_location` |

Headers are matched case-insensitively with synonym detection, most-specific match first.
Exports from LinkedIn, Naukri, Indeed or a spreadsheet work as-is: the parser detects the
delimiter (comma, semicolon, tab, pipe) and the encoding (UTF-8/BOM, Windows-1252, Latin-1).

Limits: 5 MB per file, 100 jobs per batch, 30,000 characters per description, and at least
50 characters of description per row. A sample CSV is downloadable from the
**Recovery, runs and lock** panel and lives at [`data/sample_jobs.csv`](data/sample_jobs.csv).

---

## 🚀 Local Setup

### 1. Prerequisites
- Python 3.12
- Git

### 2. Clone and install
```bash
git clone https://github.com/kaustubh12022/JobHunt-AI-Studio.git
cd JobHunt-AI-Studio
python -m venv .venv

# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-dev.txt
playwright install chromium
```

### 3. Configure secrets
Copy [`.env.example`](.env.example) to `.env` and set:

```ini
DEEPSEEK_API_KEY=sk-your-deepseek-api-key
STUDIO_ACCESS_KEY=a-long-random-owner-passphrase
```

Generate the passphrase with:
```bash
python -c "import secrets; print('jh-'+secrets.token_urlsafe(24))"
```

`.env` is git-ignored and is never baked into the Docker image.

### 4. Start
```bash
streamlit run app.py
```
Open `http://localhost:8501`, enter the owner passphrase, then upload a jobs CSV.

---

## 🧪 Running Tests

The suite covers the locked profile, owner gate, DeepSeek client retries and credential
errors, truthfulness of failures, CSV safety, SQLite leases/recovery, PDF rendering and ATS
auditing, reviewer parsing, packaging and UI behaviour:

```bash
pytest tests -o pythonpath=. -v
```

66 tests, no live provider calls required.

---

## ☁️ Production Deployment (Render)

The repository ships a Render Blueprint ([`render.yaml`](render.yaml)) and Docker image.

1. In the Render Dashboard, create a **Blueprint** from this repository.
2. Render builds the Docker image (`python:3.12-slim-bookworm`), installs Chromium, and mounts
   the 1 GB persistent disk at `/var/data` (paid plan; the current live instance runs on the
   free tier without the disk, so its run history is ephemeral).
3. Set the two server-owned secrets in the service's **Environment** tab:

| Variable | Purpose |
| :--- | :--- |
| `DEEPSEEK_API_KEY` | The one key used for scoring, drafting and review. |
| `STUDIO_ACCESS_KEY` | Owner passphrase that unlocks the studio (min. 12 chars). |

4. Deploy. Existing environment variables are never copied into the image or repository.

Optionally set `DEEPSEEK_INPUT_PER_M` / `DEEPSEEK_OUTPUT_PER_M` to display USD pricing in the
batch estimate; without them the studio shows a token-only estimate rather than inventing a price.

Health check: `GET /_stcore/health` → `ok`. Operational notes live in [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 🛡️ Security Model

1. **Fails closed** — no `STUDIO_ACCESS_KEY` means the studio refuses to open at all.
2. **No secret in git** — `.env`, keys and passphrases are excluded from the image and repo.
3. **Ephemeral credentials** — the DeepSeek key lives in process memory for the request only.
4. **Hashed recovery codes** — runs are indexed by SHA-256, raw codes are never stored.
5. **CSV injection defense** — exported cells beginning with `= + - @` are neutralized.
6. **Hardened rendering** — headless Chromium runs with JavaScript disabled and external
   requests blocked; company research rejects private/loopback addresses.
7. **Non-root container** — production runs as `appuser` (UID 10001).

---

## 🗂️ Project Structure

```
app.py                     Locked Streamlit studio (owner gate + four screens)
ui.py                      Status banner, JSON snapshot, plain HTML tables, log console
AGENT_GUIDE.md             Selectors, labels and Playwright recipe for automation
core/
  settings.py              Locked identity, engine, policy
  access.py                Owner passphrase gate
  ai_engine.py             DeepSeek client (retries, JSON mode, multimodal)
  models_catalog.py        DeepSeek-only model catalog
  scorer.py                Match scoring
  resume_tailor.py         Profile-bound tailoring + hallucination sanitizer
  resume_review.py         PDF inspection + DeepSeek challenger
  pdf_generator.py         Playwright A4 renderer
  ats_verifier.py          ATS acceptance audit
  run_service.py           Worker orchestration, checkpoints, packaging
  run_store.py             SQLite runs/jobs/events/artifacts
  packager.py              ZIP bundle builder
  profile_manager.py       Bundled owner profile loader
  csv_parser.py            Tolerant CSV ingestion (encodings, delimiters, synonyms)
data/profile.yaml          Kaustubh's locked master profile
tests/                     66 unit/integration tests
Dockerfile · render.yaml   Production container and Render blueprint
```

---

## 📄 License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for details.
