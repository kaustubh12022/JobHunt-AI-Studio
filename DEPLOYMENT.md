# JobHunt AI Studio — Private Deployment Guide

This guide covers deploying the locked, single-owner studio to
[Render](https://render.com) with Docker and a persistent disk.

---

## 1. Deployment model

- **Runtime**: Docker (`python:3.12-slim-bookworm`), non-root `appuser` (UID 10001).
- **Persistent storage**: Render disk mounted at `/var/data`, holding the SQLite run store
  (`jobhunt_studio.db`) and verified PDF artifacts (`/var/data/runs/`).
- **Single instance**: a persistent disk is mounted to one instance (`numInstances: 1`).
- **Server-owned secrets**: `DEEPSEEK_API_KEY` and `STUDIO_ACCESS_KEY` are Render environment
  variables. They are never committed, baked into the image, or entered by visitors.
- **Restart recovery**: an interrupted run is recovered as `awaiting_credentials`; the owner
  resumes it with the run's 12-character recovery code. No key re-entry is needed because the
  key belongs to the server.
- **Lifecycle**: inactive runs expire after 24 hours and are purged by the janitor.

---

## 2. Deploy on Render

### Step 1: Push the code
```bash
git add .
git commit -m "feat: lock studio to owner and DeepSeek-only engine"
git push origin main
```

### Step 2: Create the Blueprint
1. Open the [Render Dashboard](https://dashboard.render.com).
2. Click **New +** → **Blueprint** and select `kaustubh12022/JobHunt-AI-Studio`.
3. Render reads [`render.yaml`](render.yaml): Docker runtime, `standard` plan, Oregon region,
   1 GB disk at `/var/data`, health check `/_stcore/health`, auto-deploy on `main`.

### Step 3: Set the secrets
In the service's **Environment** tab:

| Key | Value |
| :--- | :--- |
| `DEEPSEEK_API_KEY` | Your DeepSeek API key (`sk-...`) |
| `STUDIO_ACCESS_KEY` | A long random owner passphrase (≥ 12 characters) |
| `DEEPSEEK_INPUT_PER_M` *(optional)* | Input USD per 1M tokens, if you want priced estimates |
| `DEEPSEEK_OUTPUT_PER_M` *(optional)* | Output USD per 1M tokens, if you want priced estimates |

Generate a passphrase locally:
```bash
python -c "import secrets; print('jh-'+secrets.token_urlsafe(24))"
```

> These values are stored only in Render's environment. Never commit them to `.env.example`,
> the README, or the repository.

### Step 4: Deploy
Click **Apply**. Render builds the image, installs Playwright Chromium, mounts the disk and
starts the service. The first build takes a few minutes.

---

## 3. Verify a deployment

1. **Health**: `curl -f https://<service>.onrender.com/_stcore/health` → `ok`
2. **Gate**: open the URL in a private window — you must see *Owner access only* and no
   profile data. A wrong passphrase must be rejected.
3. **Unlock**: enter `STUDIO_ACCESS_KEY`; the header must read *Make every application count.*
   and the sidebar must show *DeepSeek V4.1 Flash* with *credential: configured*.
4. **Profile**: the Master profile tab must show Kaustubh Kale, 60+ skills and 6 projects.
5. **Batch**: upload a small CSV (1–2 jobs), start the batch, and confirm the pipeline reaches
   `BATCH COMPLETE` with an approved PDF and a downloadable ZIP.

---

## 4. Operations

### Restart and crash recovery
1. The worker heartbeat renews a 60-second lease every 15 seconds.
2. On restart, `RunStore.recover_interrupted_runs()` moves orphaned `running` runs to
   `awaiting_credentials`.
3. Paste the run's recovery code into **Run recovery & lookup** and click **Resume saved batch**.

### Backup and restore
```bash
# Backup on the Render Shell
tar -czf /var/data/backup_$(date +%Y%m%d).tar.gz \
  /var/data/jobhunt_studio.db /var/data/runs/

# Restore
tar -xzf /var/data/backup_YYYYMMDD.tar.gz -C /
```

### Rollback
In Render → **Deploys**, select the previous successful deploy → **Rollback to this deploy**.
The persistent disk is preserved.

### Rotating secrets
Update `DEEPSEEK_API_KEY` or `STUDIO_ACCESS_KEY` in the Environment tab and save. Render
restarts the service automatically; active runs keep their checkpoints.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
| :--- | :--- | :--- |
| **Studio locked** page | `STUDIO_ACCESS_KEY` missing or < 12 chars | Set the variable and redeploy |
| *DeepSeek credential missing* warning | `DEEPSEEK_API_KEY` not set | Set the variable and redeploy |
| Batch pauses as `awaiting_credentials` | Key rejected (401/402/403) or balance exhausted | Update `DEEPSEEK_API_KEY`, resume the batch |
| `429` / temporary failures | Provider throttling | The client retries automatically; otherwise resume the batch |
| PDF engine unavailable in health | Chromium missing | Rebuild the image (`playwright install --with-deps chromium`) |
