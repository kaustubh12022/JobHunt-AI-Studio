# JobHunt AI Studio

[![Live App](https://img.shields.io/badge/Render-Live_App-2563eb?logo=render&logoColor=white)](https://jobhunt-ai-studio.onrender.com)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-green.svg?logo=playwright&logoColor=white)](https://playwright.dev/)
[![Tests](https://img.shields.io/badge/Tests-44_Passed-success.svg)](https://github.com/kaustubh12022/JobHunt-AI-Studio)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**JobHunt AI Studio** is an autonomous, production-ready AI resume tailoring and batch evaluation engine. It enables job seekers, freshers, and experienced engineers to upload batch job listings (CSV), evaluate candidate-job relevance with zero-hallucination guardrails, and generate tailored, single-page, ATS-compliant PDF resumes bundled into ready-to-download ZIP archives.

---

## 🌐 Live Web Studio

You can test and use the live application right now without installing anything locally:

👉 **[https://jobhunt-ai-studio.onrender.com](https://jobhunt-ai-studio.onrender.com)**

---

## 📖 Table of Contents
1. [Core Features & Architecture](#-core-features--architecture)
2. [Key Terms & System Concepts](#-key-terms--system-concepts)
3. [CSV Format & Scraper Configuration](#-csv-format--scraper-configuration)
4. [Supported AI Providers & Models](#-supported-ai-providers--models)
5. [Candidate Profile Customization](#-candidate-profile-customization)
6. [Local Setup & Quick Start](#-local-setup--quick-start)
7. [Running Tests](#-running-tests)
8. [Production Deployment (Render)](#-production-deployment-render)
9. [Security, Privacy & Safety](#-security-privacy--safety)

---

## 🌟 Core Features & Architecture

- **Setup → Live Pipeline → Results Workflow**: Clean, minimalist UI built with Streamlit, optimized for desktop and mobile browsers.
- **Full JD Context Preservation**: Sends raw, complete job descriptions to the AI (zero aggressive stripping of technical criteria, qualifications, or requirements).
- **Dynamic Role Lens Alignment**: Automatically detects job archetypes:
  - **Testing / QA / SDET Roles**: Dynamically prioritizes **Work Experience before Projects**.
  - **Developer / Java / Full Stack Roles**: Dynamically prioritizes **Projects before Work Experience**.
- **Automated Single-Page Playwright Renderer**: Converts Jinja2 templates into exact A4 single-page PDFs with automated font density and margin compaction.
- **Programmatic ATS Verification Gate**: Every generated PDF is audited with `ats_verifier.py` (strict 1-page bounds, clean plain-text URLs, 4-digit years `YYYY`, contact location, and zero kerning glitches).
- **Restart & Crash Recovery**: Interrupted batch runs automatically transition to `awaiting_credentials`. Users supply their recovery code to resume right where they left off without paying duplicate AI scoring tokens.
- **Zero API Key Persistence**: API keys exist solely in memory during active processing runs. Keys are never saved to disk, SQLite, environment variables, or shared caches.
- **Multi-Tenant State Isolation**: Every batch execution is backed by an isolated SQLite WAL store and private session workspace.

---

## 🔑 Key Terms & System Concepts

If you are a new user or developer, here are the essential terms used throughout the studio:

| Term | Meaning & Purpose |
| :--- | :--- |
| **Master Profile** | Your single source of truth (`.yaml` format). Contains verified degrees, real projects, real employers, verified dates, and master skill inventory. The AI **never** hallucinates outside this profile. |
| **Recovery Code** | A cryptographically random 12-character token (e.g. `jh-a1b2-c3d4-e5f6`) issued to every batch. Allows you to resume interrupted runs or reload results from any browser. |
| **Role Lens** | The architectural category detected from the job title and JD (e.g. `qa`, `java`, `dotnet`, `fullstack`). Dictates layout ordering and technical emphasis. |
| **Match Threshold (%)** | Relevance score (0–100%) cutoff. Only jobs scoring equal to or above this threshold have tailored resumes generated (default: `70%`). |
| **Resume Cap** | Maximum number of tailored resumes generated per batch to conserve API quota and budget. |
| **ATS Audit** | Automated programmatic validation inspecting PDF dimensions, text streams, and structure to guarantee 100% Applicant Tracking System compliance. |

---

## 📋 CSV Format & Scraper Configuration

If you are scraping jobs from LinkedIn, Indeed, Naukri, or n8n webhooks, configure your scraper to output a standard CSV.

### Recommended CSV Schema

```csv
company_name,job_role,job_description,direct_apply_link,location
```

### Column Specifications

| Column Header | Status | Description & Rules | Example |
| :--- | :---: | :--- | :--- |
| **`company_name`** *(or `company`)* | **Required** | Hiring organization name. Used in prompts, resume headers, and filenames. | `Amazon`, `Stripe` |
| **`job_role`** *(or `job_title`, `title`)* | **Required** | Position title. Used for role lens detection and resume designation. | `Software Dev Engineer I`, `Java Developer` |
| **`job_description`** *(or `jd_text`, `description`)* | **Required** | Complete raw job posting text. Minimum 50 characters, max 30,000 characters. HTML tags are stripped automatically. | `Looking for a backend engineer with Java, Spring Boot, MySQL...` |
| **`direct_apply_link`** *(or `apply_link`, `url`)* | **Recommended** | Direct application URL. Rendered as a clickable link in summary tables. | `https://amazon.jobs/en/jobs/2049182` |
| **`location`** | **Optional** | Location or work mode. Defaults to candidate's city if missing. | `Pune, India / Hybrid`, `Remote` |

### Sample Copy-Paste CSV

```csv
company_name,job_role,job_description,direct_apply_link,location
"Amazon","Software Dev Engineer I (SDE 1)","We are looking for entry-level software developers. Responsibilities include building scalable distributed systems, writing clean code in Java or Python, working with relational databases and AWS services. Requirements: Bachelor's degree in CS/IT, strong problem solving in Data Structures and Algorithms.","https://amazon.jobs/en/jobs/2049182","Bangalore, India"
"Innovaccer","Associate Software Engineer - Java","Looking for graduate engineers to design and build scalable microservices using Java, Spring Boot, and REST APIs. Responsibilities include database design with MySQL, writing unit tests with JUnit, and collaborating with Agile teams. Knowledge of SQL and Git is required.","https://careers.innovaccer.com/jobs/ase-java","Noida, India / Hybrid"
```

> **Scraper Best Practices**:
> - Enclose job descriptions in standard quotes (`""`) to protect multi-line paragraphs and newlines.
> - Output in standard **UTF-8** encoding.
> - The parser includes **fuzzy column synonym matching**, so headers like `Company Name`, `Role`, `Job Description`, or `Job Link` are recognized automatically.

---

## 🤖 Supported AI Providers & Models

JobHunt AI Studio supports three enterprise AI providers. You can bring your own API key:

### 1. Google Gemini
- **Free Tier (Default)**: Automatically switches to **`gemini-3.5-flash-lite`** with a 15 requests-per-minute (RPM) rate limit and multi-key rotation pool. Costs **$0.00**.
- **Paid Tier**: Allows selecting higher-tier models such as `gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-1.5-flash` with configurable RPM limits.
- Supports providing multiple Gemini keys (comma-separated) for seamless rotation.

### 2. DeepSeek
- Models: `deepseek-chat` (DeepSeek-V3) and `deepseek-reasoner` (DeepSeek-R1).
- Direct high-speed API integration at competitive token pricing.

### 3. OpenAI
- Models: `gpt-4o-mini` (Default economical model) and `gpt-4o`.

---

## 👤 Candidate Profile Customization

The system generates resumes by tailoring your master profile. You can inspect or modify your profile in **Tab 2 ("Candidate Profile")**:

1. **In-Browser Editor**: Edit your personal information, skills, education, internships, and projects directly in the UI and click **Apply Profile Changes**.
2. **Upload YAML**: Upload an existing `profile.yaml` file conforming to the standard schema:

```yaml
personal_information:
  name: "Jane"
  surname: "Doe"
  email: "jane.doe@example.com"
  phone: "+1-555-0199"
  city: "San Francisco"
  country: "United States"
  linkedin: "linkedin.com/in/janedoe"
  github: "github.com/janedoe"

education_details:
  - degree: "Bachelor of Science in Computer Science"
    university: "University of California, Berkeley"
    dates: "2022 - 2026"
    score: "Cumulative GPA: 3.8 / 4.0"

experience_details:
  - company: "Tech Innovations Inc"
    position: "Software Engineering Intern"
    dates: "Jun 2025 - Aug 2025"
    location: "San Francisco, CA"
    key_responsibilities:
      - "Engineered backend microservices using Java and Spring Boot, reducing latency by 22%."
      - "Integrated RESTful APIs and optimized SQL database queries for high-throughput transactions."

projects:
  - name: "Distributed Task Scheduler"
    tech_stack: "Python, Redis, Docker, FastAPI"
    link: "github.com/janedoe/task-scheduler"
    description_bullets:
      - "Architected a distributed background worker pool processing 10,000+ jobs per minute."
      - "Implemented Redis pub/sub messaging and deployed containerized services via Docker."

skills:
  - "Java"
  - "Python"
  - "Spring Boot"
  - "FastAPI"
  - "PostgreSQL"
  - "Docker"
  - "Git"
```

3. **Public Sample Profile**: If testing the studio for the first time, click **Load Sample Profile** to prefill fictional credentials.

---

## 🚀 Local Setup & Quick Start

If you want to run JobHunt AI Studio locally on your computer:

### 1. Prerequisites
- Python 3.10, 3.11, or 3.12
- Git

### 2. Clone Repository
```bash
git clone https://github.com/kaustubh12022/JobHunt-AI-Studio.git
cd JobHunt-AI-Studio
```

### 3. Set Up Virtual Environment
```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Dependencies & Playwright
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
playwright install chromium
```

### 5. Start Application
```bash
# Windows quick-launcher
start.bat

# Or direct command
streamlit run app.py
```
Open your browser at `http://localhost:8501`.

---

## 🧪 Running Tests

The test suite includes 44 unit and integration tests covering SQLite ACID transactions, worker lease recovery, input safety, URL sanitization, multi-role tailoring, and Playwright ATS PDF verification:

```bash
pytest tests -o pythonpath=. -v
```

---

## ☁️ Production Deployment (Render)

JobHunt AI Studio is turnkey-ready for deployment on [Render](https://render.com) using Docker and persistent disks.

### 1-Click Blueprint Deployment
1. Fork or clone this repository to your GitHub account.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** → **Blueprint**.
3. Select your repository `JobHunt-AI-Studio`.
4. Render reads [`render.yaml`](render.yaml) automatically:
   - Sets runtime to **Docker** (`python:3.12-slim-bookworm`).
   - Attaches a **1 GB Persistent Disk** (`jobhunt-data`) mounted at `/var/data` for SQLite durability.
   - Sets `PORT=8501` and `PERSISTENT_DATA_DIR=/var/data`.
5. Click **Apply**. Render builds the container, installs Chromium, mounts the disk, and takes the service live!

For advanced disaster recovery and backup instructions, refer to [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## 🛡️ Security, Privacy & Safety

JobHunt AI Studio was architected with enterprise data privacy standards:

1. **Ephemeral Keys**: Your API keys are held exclusively in process memory for the duration of your active batch and discarded immediately afterward.
2. **Encrypted Recovery Codes**: Batches are indexed by a SHA-256 cryptographic hash of the recovery code. Raw recovery codes are never stored in plaintext.
3. **CSV Formula Injection Defense**: All exported cells starting with `=`, `+`, `-`, `@`, `\t`, or `\r` are neutralized with prepended quotes to prevent spreadsheet execution vulnerabilities.
4. **Hardened Headless Chromium**: PDF rendering runs with `java_script_enabled=False` and blocks external network requests to prevent XSS payloads and SSRF leaks.
5. **Non-Root Container**: Production Docker builds run strictly under the unprivileged `appuser` (UID 10001).

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
