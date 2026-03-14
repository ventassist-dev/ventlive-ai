# ══════════════════════════════════════════════════════════════════════════════
# Dockerfile — VentLive AI
# Real-Time Voice AI for ICU Mechanical Ventilation
#
# Build:   docker build -t ventlive-ai .
# Run:     docker run -p 8080:8080 --env-file .env ventlive-ai
# Deploy:  ./deploy.sh cloudrun
#
# IMPORTANT — Single worker only:
#   VentLive AI holds WebSocket session state, Gemini Live audio sessions,
#   and in-process audio generation counters in memory.
#   Multiple workers split this state across processes and break live sessions.
#   Always run with --workers 1 (enforced in CMD below).
# ══════════════════════════════════════════════════════════════════════════════

# ── Base image ────────────────────────────────────────────────────────────────
# python:3.11-slim chosen over alpine:
#   - google-cloud-firestore requires libssl + libffi which alpine needs
#     compiled from source (slow, fragile)
#   - google-genai SDK requires grpcio which has no alpine wheel
#   - slim gives ~140MB base vs alpine's ~50MB but saves build time
#     and avoids runtime crashes from missing system libraries
FROM python:3.11-slim

# ── Build metadata ────────────────────────────────────────────────────────────
LABEL maintainer="VentLive AI"
LABEL description="Real-Time Voice AI Clinical Decision Support — ICU Ventilation"
LABEL version="4.0"

# ── System dependencies ───────────────────────────────────────────────────────
# Required by:
#   grpcio (google-cloud-firestore transport)     → libssl, libffi
#   google-genai audio processing                 → libgomp1
#   uvicorn[standard] websockets C extensions     → gcc, python3-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl-dev \
        libffi-dev \
        libgomp1 \
        gcc \
        python3-dev \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependency installation ───────────────────────────────────────────
# Copy requirements first — Docker layer cache:
#   if requirements.txt unchanged, pip install layer is reused
#   even when source files change. Saves 60-120s on rebuilds.
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Application source files ──────────────────────────────────────────────────
# Copied after pip install to maximise cache hits.
# Order matters — most frequently changed files last.
COPY vent_reasoning.py .
COPY case_memory.py    .
COPY gemini_handler.py .
COPY live_session.py   .
COPY main.py           .
COPY index.html        .

# ── Runtime environment defaults ──────────────────────────────────────────────
# These are defaults only — override at runtime via:
#   docker run --env-file .env ...
#   gcloud run deploy --set-env-vars ...
#   Cloud Run environment variable configuration

# Cloud Run injects PORT automatically — default 8080
ENV PORT=8080

# Google Cloud — override with your project values
ENV GCP_LOCATION="us-central1"
ENV GCP_MODEL="gemini-live-2.5-flash-native-audio"

# API key — override in production
# Default matches README and deploy.sh defaults
ENV VENTLIVE_API_KEY="ventlive-demo-2026"

# Python behaviour
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── Port declaration ──────────────────────────────────────────────────────────
# Cloud Run uses PORT env var — EXPOSE documents the default.
# Does not publish the port — use -p 8080:8080 at runtime.
EXPOSE 8080

# ── Non-root user ─────────────────────────────────────────────────────────────
# Run as non-root for container security best practice.
# Required by some GCP security policies.
RUN groupadd --gid 1001 ventlive \
    && useradd --uid 1001 --gid ventlive --shell /bin/bash --create-home ventlive \
    && chown -R ventlive:ventlive /app

USER ventlive

# ── Health check ──────────────────────────────────────────────────────────────
# Cloud Run uses this to determine instance readiness.
# /health returns {"status":"ok"} — no auth required.
# Interval 30s: allows Gemini credentials to initialize on cold start.
# Timeout 10s:  covers Firestore connectivity test on startup.
# Start period 15s: gives uvicorn time to bind before first check.
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=15s \
    --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ── Entry point ───────────────────────────────────────────────────────────────
# Shell form used (not exec form) so PORT env var is expanded at runtime.
#
# --workers 1  MANDATORY — see note at top of file.
#              VentLive AI cannot run with multiple workers.
#
# --loop uvloop  Faster async event loop (installed via uvicorn[standard]).
#                Required for Gemini Live WebSocket audio throughput.
#
# --ws websockets  Explicit WebSocket implementation.
#                  Avoids wsproto fallback which lacks binary frame support
#                  needed for PCM audio streaming.
#
# --log-level info  Captures startup credential validation messages
#                   ("✅ Firestore connected", "[gemini_handler] ✅ Credentials")
#                   in Cloud Run logs for deployment verification.
#
# --timeout-keep-alive 75  Longer than Cloud Run's 60s idle timeout.
#                          Prevents premature connection drops on
#                          long Gemini Live audio sessions.
CMD uvicorn main:app \
    --host 0.0.0.0 \
    --port ${PORT} \
    --workers 1 \
    --loop uvloop \
    --ws websockets \
    --log-level info \
    --timeout-keep-alive 75
