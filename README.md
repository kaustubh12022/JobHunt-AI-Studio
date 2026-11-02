# 🎯 JobHunt AI Studio

**JobHunt AI Studio** is a decoupled, production-ready web application and batch processing engine. It empowers candidates and recruiters to upload job listings, match them against their candidate profile, and generate verified single-page tailored PDF resumes, executive summary spreadsheets, and packaged ZIP archives.

---

## 🌟 Core Architecture & Features

- **Decoupled Run Orchestration**: Processing logic is separated from the UI into `RunService`, SQLite-backed `RunStore` (WAL mode), and bounded background workers.
- **Persistent Disk State**: Designed for single-instance persistent storage on Render (`/var/data`), preserving run metadata and resumes across server deploys and restarts.
- **Zero API Key Persistence**: User API keys exist strictly in run-scoped memory during processing and are **never** persisted to database, disk, environment variables, or shared caches.
- **Cryptographic Recovery Code**: Every batch receives a random recovery code (e.g. `jh-a1b2-c3d4-e5f6`). Only its SHA-256 hash is persisted. All status, download, retry, and cancellation operations verify ownership.
- **Server Restart Recovery**: Interrupted runs transition into `awaiting_credentials` status. Users supply their recovery code and re-enter their API key to resume without repeating completed work.
- **Truthful ATS Verification Gate**: Every generated resume is audited with `ats_verifier.py` (strict 1-page bounds, clean plain URLs, 4-digit years `YYYY`, contact location, and zero kerning glitches). Unverified PDFs are excluded from the verified bundle.
- **Input & Output Safety**:
  - 5 MB CSV limit, 100 jobs per run limit, 1 MB profile limit.
  - Rejects oversized descriptions (> 30,000 characters) without silent truncation.
  - CSV formula injection neutralization (prepends `'` to leading `=`, `+`, `-`, `@`, `\t`, `\r`).
  - Safe URL scheme enforcement (blocks `javascript:`, `data:`, `file:` links).
  - Jinja2 HTML autoescaping and Playwright external network request blocking.
- **Centralized Model Catalog & Dated Pricing**:
  - Google Gemini: `gemini-3.5-flash-lite` (default, Free Tier 15 RPM cap / Paid), `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-1.5-flash`.
  - OpenAI: `gpt-4o-mini` (default), `gpt-4o`.
  - DeepSeek: `deepseek-chat` (DeepSeek-V3 / Flash API), `deepseek-reasoner` (R1).
  - Explicit error upon invalid model (no silent model substitution).

---

## 🚀 Quick Start (Local Run)

### 1. Prerequisites
- Python 3.10+ (Python 3.11 or 3.12 recommended)
- Playwright Chromium (`playwright install chromium`)

### 2. Installation
```bash
# Clone and enter directory
cd JobHunt_AI_Studio

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright Chromium browser
playwright install chromium
```

### 3. Launch Web Dashboard
```bash
# Start Streamlit application
streamlit run app.py
```
Open your browser at `http://localhost:8501`.

---

## 📁 Repository Structure

```text
JobHunt_AI_Studio/
├── app.py                      # Streamlit dashboard interface
├── ui.py                       # Escaped presentation components & styles
├── core/
│   ├── models.py               # Typed domain contracts (RunConfig, JobResult, etc.)
│   ├── models_catalog.py       # Centralized model specifications & dated pricing
│   ├── security.py             # Input limits, URL safety, CSV formula neutralization
│   ├── run_store.py            # SQLite persistent ACID store with WAL mode & worker leases
│   ├── run_service.py          # Bounded background worker & batch stage orchestration
│   ├── ai_engine.py            # ScopedAIClient & Gemini project-level rate limiter
│   ├── scorer.py               # ATS relevance scoring (truthful failure reporting)
│   ├── resume_tailor.py        # Source-bound tailoring & factual sanitization
│   ├── pdf_generator.py        # Playwright Chromium 1-page PDF renderer (concurrency=1)
│   ├── ats_verifier.py         # Universal ATS acceptance test suite
│   ├── csv_parser.py           # Fuzzy column mapping & input validator
│   ├── cost_estimator.py       # Pre-run token and cost projection calculator
│   ├── packager.py             # Atomic ZIP packager for verified outputs
│   ├── summary_reporter.py     # Neutralized CSV and autoescaped HTML reports
│   ├── profile_manager.py      # Profile normalizer (no invented defaults)
│   ├── health.py               # System diagnostics & health check
│   └── session_manager.py      # Session workspace manager
├── templates/
│   ├── resume_template.html    # Master single-page ATS resume Jinja2 template
│   └── summary_template.html   # Executive batch summary table template
├── data/
│   ├── sample_profile.yaml     # Fictional sample profile for testing
│   └── sample_jobs.csv         # Validated sample jobs CSV
├── tests/                      # Pytest verification suites
├── Dockerfile                  # Non-root production container
├── .dockerignore               # Docker build exclusion rules
├── render.yaml                 # Render persistent disk blueprint
├── DEPLOYMENT.md               # Production deployment & disaster recovery guide
└── requirements.txt            # Minimal standalone dependencies
```

---

## 🔒 Security & Privacy Invariants

1. **Ephemeral Keys**: API keys are held strictly in temporary run memory and are purged once processing finishes.
2. **Access Control**: Every run is protected by an independent, unpersisted recovery code. Operations require proof of recovery ownership.
3. **No Fabricated Data**: If an AI provider call fails, the stage fails explicitly. The engine never invents fake scores (such as a 60% fallback) or fabricated candidate facts.
