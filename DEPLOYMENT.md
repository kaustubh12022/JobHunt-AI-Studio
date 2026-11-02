# 🌐 JobHunt AI Studio — Production Deployment Guide

This guide details deploying **JobHunt AI Studio** to [Render](https://render.com) using Docker with persistent disk storage for state durability across deploys and container restarts.

---

## 1. Architectural Highlights & Deployment Model

- **Runtime**: Docker (`python:3.12-slim`) with non-root security (`appuser`).
- **Persistent Storage**: Render Persistent Disk mounted at `/var/data` preserving SQLite run metadata (`jobhunt_studio.db`) and verified PDF resume artifacts (`/var/data/runs/`).
- **Single-Instance Constraint**: Render persistent disks are mounted to a single instance (`numInstances: 1`). Horizontal multi-container scaling requires migrating the database to managed PostgreSQL and object storage (e.g. S3/R2).
- **Ephemeral Credentials**: User API keys are stored in run-scoped memory only and never written to disk, database, or shared caches.
- **Restart Recovery**: When the server restarts or container updates, active runs transition into `awaiting_credentials`. Users supply their private recovery code and API key to resume without losing prior work.
- **Automated Lifecycle**: Inactive runs expire after 24 hours. Completed runs are purged by the background janitor, preserving storage.

---

## 2. Step-by-Step Deployment on Render

### Step 1: Push Code to GitHub / GitLab
Ensure your repository contains `Dockerfile`, `render.yaml`, and `.dockerignore`.
```bash
git add .
git commit -m "feat: JobHunt AI Studio production release"
git push origin main
```

### Step 2: Create Web Service on Render
1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** $\rightarrow$ **Web Service**.
3. Select your repository.
4. Render detects the `Dockerfile` and `render.yaml`:
   - **Environment**: Docker
   - **Plan**: Standard or Starter (at least 1 GB RAM recommended for Playwright Chromium)
   - **Region**: Oregon or Frankfurt
   - **Branch**: `main`

### Step 3: Attach Persistent Disk
In the Web Service settings under **Disks**:
- **Disk Name**: `jobhunt-data`
- **Mount Path**: `/var/data`
- **Size**: `1 GB` (or larger depending on expected volume)

### Step 4: Environment Variables
Configure the following in the Render dashboard:
- `PORT`: `8501`
- `PERSISTENT_DATA_DIR`: `/var/data`

*(Do NOT set API keys in Render environment variables; visitors supply their own keys in their browser session).*

### Step 5: Deploy
Click **Create Web Service**. Render builds the image, installs Playwright Chromium, mounts the persistent disk, and launches the service.

---

## 3. Operations, Maintenance & Disaster Recovery

### Server Restart & Crash Recovery
1. If the host container restarts while a batch is in progress, the worker lease expires.
2. Upon restart, `RunStore.recover_interrupted_runs()` transitions uncompleted runs to `awaiting_credentials`.
3. The user enters their 12-character recovery code (e.g. `jh-a1b2-c3d4-e5f6`) and re-enters their API key in the studio interface to resume processing right where it left off.

### Backup & Restore Procedure
- **Backup**:
  SSH or use Render Shell to create a tarball of the persistent disk:
  ```bash
  tar -czf /var/data/backup_$(date +%Y%m%d).tar.gz /var/data/jobhunt_studio.db /var/data/runs/
  ```
- **Restore**:
  Extract the archive back into `/var/data`:
  ```bash
  tar -xzf /var/data/backup_YYYYMMDD.tar.gz -C /
  ```

### Rollback Instructions
If a new release encounters issues:
1. In the Render Dashboard, go to **Deploys**.
2. Locate the previous successful build.
3. Click **Rollback to this deploy**. The previous Docker image will be restored with the persistent disk intact.

---

## 4. Verification & Testing

Every deployed build should verify:
1. Health check returns healthy: `curl -f http://<app-domain>/_stcore/health`
2. Non-root user permissions permit writing to `/var/data`
3. Single-page PDF generation passes the universal ATS verification suite.
