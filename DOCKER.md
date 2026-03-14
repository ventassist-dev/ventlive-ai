# ══════════════════════════════════════════════════════════════════════════════
# VentLive AI — Docker Command Reference
# ══════════════════════════════════════════════════════════════════════════════

# ── Build ─────────────────────────────────────────────────────────────────────
docker build -t ventlive-ai .

# ── Run ───────────────────────────────────────────────────────────────────────

# Recommended — env file keeps secrets out of shell history and process list
docker run --env-file .env -p 8080:8080 ventlive-ai

# Run detached (background)
docker run --env-file .env -p 8080:8080 -d ventlive-ai

# If port 8080 is already in use, map to a different local port:
docker run --env-file .env -p 9090:8080 ventlive-ai
# Then access at http://localhost:9090 and update index.html BACKEND accordingly

# Run with service account key for Firestore + Vertex AI
docker run \
  --env-file .env \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa.json \
  -v /path/to/your-service-account.json:/secrets/sa.json:ro \
  -p 8080:8080 \
  ventlive-ai

# ── Inline environment variables (alternative to --env-file) ──────────────────
# Security note: prefer --env-file over -e for API keys —
# values passed via -e appear in shell history (~/.bash_history)
# and in `docker inspect` output in plaintext.
# Use this form only for non-sensitive defaults:
docker run \
  -e GCP_PROJECT=your-project-id \
  -e GCP_LOCATION=us-central1 \
  -e GCP_MODEL=gemini-live-2.5-flash-native-audio \
  -e VENTLIVE_API_KEY=ventlive-demo-2026 \
  -p 8080:8080 \
  ventlive-ai
# For GEMINI_API_KEY always use --env-file instead of -e

# ── Verify ────────────────────────────────────────────────────────────────────

# Health check — confirms server is up and storage backend is active
curl http://localhost:8080/health

# Expected response:
# {
#   "status": "ok",
#   "service": "VentLive AI v4.0",
#   "model": "gemini-live-2.5-flash-native-audio",
#   "platform": "Vertex AI",
#   "storage": {"backend": "firestore", "collection": "vent_cases", ...}
# }

# View startup logs — confirms Firestore connection and credential validation
docker logs $(docker ps -q --filter ancestor=ventlive-ai)

# Expected startup output:
# ✅ Firestore connected — cases will persist to cloud
# [gemini_handler] ✅ Credentials validated — project=your-project-id
# INFO:     Uvicorn running on http://0.0.0.0:8080

# If Firestore is unavailable you will see (app still runs):
# ⚠️  Firestore unavailable (DefaultCredentialsError) — using in-memory fallback

# Stream logs live (useful during a demo or judge review)
docker logs -f $(docker ps -q --filter ancestor=ventlive-ai)

# ── Stop ──────────────────────────────────────────────────────────────────────

# Stop the running container gracefully (SIGTERM → uvicorn shutdown)
docker stop $(docker ps -q --filter ancestor=ventlive-ai)

# Stop immediately (SIGKILL — use only if graceful stop hangs)
docker kill $(docker ps -q --filter ancestor=ventlive-ai)

# ── Cleanup ───────────────────────────────────────────────────────────────────

# Remove stopped container (required before re-running on same port)
docker rm $(docker ps -aq --filter ancestor=ventlive-ai)

# Remove the image (frees disk space, forces full rebuild next time)
docker rmi ventlive-ai

# Full cleanup — stop + remove container + remove image in one sequence
docker stop $(docker ps -q --filter ancestor=ventlive-ai) 2>/dev/null || true \
  && docker rm $(docker ps -aq --filter ancestor=ventlive-ai) 2>/dev/null || true \
  && docker rmi ventlive-ai 2>/dev/null || true

# ── Rebuild after code changes ────────────────────────────────────────────────

# Stop existing container, rebuild image, restart
docker stop $(docker ps -q --filter ancestor=ventlive-ai) 2>/dev/null || true
docker rm $(docker ps -aq --filter ancestor=ventlive-ai) 2>/dev/null || true
docker build -t ventlive-ai . \
  && docker run --env-file .env -p 8080:8080 -d ventlive-ai \
  && echo "✅ VentLive AI restarted" \
  && curl -s http://localhost:8080/health

# ── Port conflict detection ───────────────────────────────────────────────────

# Check if port 8080 is already in use before running
lsof -i :8080 2>/dev/null && echo "⚠️  Port 8080 in use — use -p 9090:8080 instead" \
  || echo "✅ Port 8080 available"

# macOS / Linux alternative
ss -tlnp | grep :8080 2>/dev/null \
  && echo "⚠️  Port 8080 in use — use -p 9090:8080 instead" \
  || echo "✅ Port 8080 available"
