#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# deploy.sh — VentLive AI Automated Deployment Script
# Version 4.0
#
# Real-Time Voice AI for ICU Mechanical Ventilation
# Powered by Gemini Live 2.5 Flash Native Audio + Vertex AI + Firestore
#
# ── Supported targets ─────────────────────────────────────────────────────────
#   ./deploy.sh local       Run locally with uvicorn (development)
#   ./deploy.sh docker      Build Docker image and run locally in container
#   ./deploy.sh cloudrun    Build + deploy to Google Cloud Run
#   ./deploy.sh vm          Deploy to Google Compute Engine VM via SSH
#   ./deploy.sh stop        Stop and clean up local Docker container
#   ./deploy.sh logs        Stream live logs from running Docker container
#
# ── Quick start ───────────────────────────────────────────────────────────────
#   1. Copy .env.example to .env and fill in your values
#   2. chmod +x deploy.sh
#   3. ./deploy.sh local
#
# ── Prerequisites ─────────────────────────────────────────────────────────────
#   All targets:   Python 3.11+, GCP project, Gemini API key
#   docker:        Docker Desktop or Docker Engine
#   cloudrun:      gcloud CLI authenticated, Docker
#   vm:            gcloud CLI authenticated, existing GCE instance
#
# ── Environment variables ─────────────────────────────────────────────────────
#   Required:  GCP_PROJECT, GEMINI_API_KEY
#   Optional:  GCP_LOCATION, GCP_MODEL, VENTLIVE_API_KEY, PORT
#              SERVICE_NAME, VM_INSTANCE, VM_ZONE, VM_USER
#   See .env.example for full list with defaults documented
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail   # exit on error, unset variable, or pipe failure
IFS=$'\n\t'         # safer word splitting

# ── Terminal colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Logging helpers ───────────────────────────────────────────────────────────
info()    { echo -e "${BLUE}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}══ $* ══${RESET}"; }
blank()   { echo ""; }

# ── Banner ────────────────────────────────────────────────────────────────────
print_banner() {
    echo -e "${BOLD}"
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║                     VentLive AI v4.0                             ║"
    echo "║       Real-Time Voice AI for ICU Mechanical Ventilation           ║"
    echo "║   Gemini Live 2.5 Flash Native Audio · Vertex AI · Firestore     ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo -e "${RESET}"
}

# ── Argument validation ───────────────────────────────────────────────────────
print_usage() {
    echo -e "Usage: ${BOLD}./deploy.sh [target]${RESET}"
    blank
    echo "  Targets:"
    echo "    local      Run locally with uvicorn (development mode, hot-reload)"
    echo "    docker     Build Docker image and run locally in container"
    echo "    cloudrun   Build image and deploy to Google Cloud Run"
    echo "    vm         Deploy to Google Compute Engine VM via SSH"
    echo "    stop       Stop and clean up local Docker container"
    echo "    logs       Stream live logs from running Docker container"
    blank
    echo "  Examples:"
    echo "    ./deploy.sh local"
    echo "    ./deploy.sh docker"
    echo "    ./deploy.sh cloudrun"
    echo "    ./deploy.sh stop"
    blank
}

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    print_banner
    print_usage
    exit 1
fi

VALID_TARGETS=("local" "docker" "cloudrun" "vm" "stop" "logs")
VALID=false
for t in "${VALID_TARGETS[@]}"; do
    [[ "$TARGET" == "$t" ]] && VALID=true && break
done
if [[ "$VALID" == false ]]; then
    print_banner
    error "Unknown target '$TARGET'.\n\n$(print_usage)"
fi

print_banner
info "Target: ${BOLD}$TARGET${RESET}"

# ══════════════════════════════════════════════════════════════════════════════
# STOP target — handle before env validation (no env needed to stop)
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "stop" ]]; then
    step "Stopping VentLive AI Docker Container"

    # Check if any ventlive-ai containers are running
    RUNNING=$(docker ps -q --filter ancestor=ventlive-ai 2>/dev/null || true)
    STOPPED=$(docker ps -aq --filter ancestor=ventlive-ai 2>/dev/null || true)

    if [[ -z "$RUNNING" && -z "$STOPPED" ]]; then
        warn "No ventlive-ai containers found — nothing to stop"
        exit 0
    fi

    if [[ -n "$RUNNING" ]]; then
        info "Stopping running container(s)..."
        # SIGTERM → uvicorn graceful shutdown → Gemini Live sessions close cleanly
        docker stop $(docker ps -q --filter ancestor=ventlive-ai)
        success "Container(s) stopped gracefully"
    else
        info "No running containers found (already stopped)"
    fi

    if [[ -n "$STOPPED" ]]; then
        info "Removing container(s)..."
        docker rm $(docker ps -aq --filter ancestor=ventlive-ai)
        success "Container(s) removed"
    fi

    blank
    echo -e "  To also remove the image (forces full rebuild next time):"
    echo -e "  ${BOLD}  docker rmi ventlive-ai${RESET}"
    blank
    success "Cleanup complete"
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# LOGS target — handle before env validation (no env needed to stream logs)
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "logs" ]]; then
    step "Streaming VentLive AI Container Logs"

    RUNNING=$(docker ps -q --filter ancestor=ventlive-ai 2>/dev/null || true)
    if [[ -z "$RUNNING" ]]; then
        error "No running ventlive-ai container found.\n\n  Start one first:\n  ./deploy.sh docker"
    fi

    info "Streaming logs — press Ctrl+C to stop"
    blank
    docker logs -f $(docker ps -q --filter ancestor=ventlive-ai)
    exit 0
fi

# ── Required files check ──────────────────────────────────────────────────────
step "Checking Required Project Files"

REQUIRED_FILES=(
    "main.py"
    "live_session.py"
    "gemini_handler.py"
    "vent_reasoning.py"
    "case_memory.py"
    "index.html"
    "requirements.txt"
)

MISSING_FILES=()
for f in "${REQUIRED_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        success "Found $f"
    else
        MISSING_FILES+=("$f")
    fi
done

if [[ ${#MISSING_FILES[@]} -gt 0 ]]; then
    error "Missing required files: ${MISSING_FILES[*]}\n\n  Are you running deploy.sh from the project root directory?"
fi

# ── Load .env if present ──────────────────────────────────────────────────────
step "Loading Environment Configuration"

if [[ -f ".env" ]]; then
    info "Found .env — loading environment variables"
    # Export each non-comment, non-empty line
    set -a
    # Read line by line to handle values with spaces safely
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        # Strip inline comments from value
        value="${value%%#*}"
        # Strip leading/trailing whitespace
        key="${key// /}"
        value="${value%"${value##*[![:space:]]}"}"
        export "$key=$value"
    done < .env
    set +a
    success ".env loaded"
else
    warn ".env not found — using existing shell environment"
    warn "Copy .env.example to .env and fill in your values for local development"
fi

# ── Environment variable validation ──────────────────────────────────────────
step "Validating Environment Variables"

MISSING_VARS=()

if [[ -z "${GCP_PROJECT:-}" ]]; then
    MISSING_VARS+=("GCP_PROJECT")
fi
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    MISSING_VARS+=("GEMINI_API_KEY")
fi

if [[ ${#MISSING_VARS[@]} -gt 0 ]]; then
    error "Missing required environment variables: ${MISSING_VARS[*]}\n\n  Set them in your .env file or shell environment.\n  See .env.example for the full list with documented defaults."
fi

# Apply defaults for optional variables
GCP_LOCATION="${GCP_LOCATION:-us-central1}"
GCP_MODEL="${GCP_MODEL:-gemini-live-2.5-flash-native-audio}"
VENTLIVE_API_KEY="${VENTLIVE_API_KEY:-ventlive-demo-2026}"
PORT="${PORT:-8000}"
DOCKER_PORT="${DOCKER_PORT:-8080}"
HOST_PORT="${HOST_PORT:-8080}"
SERVICE_NAME="${SERVICE_NAME:-ventlive-ai}"
IMAGE_TAG="${IMAGE_TAG:-ventlive-ai}"
VM_INSTANCE="${VM_INSTANCE:-ventlive-vm}"
VM_ZONE="${VM_ZONE:-us-central1-a}"
VM_USER="${VM_USER:-$(whoami)}"

# Display validated configuration (mask secrets)
success "GCP_PROJECT      = $GCP_PROJECT"
success "GCP_LOCATION     = $GCP_LOCATION"
success "GCP_MODEL        = $GCP_MODEL"
success "VENTLIVE_API_KEY = ${VENTLIVE_API_KEY:0:8}... (${#VENTLIVE_API_KEY} chars)"
success "GEMINI_API_KEY   = ${GEMINI_API_KEY:0:8}... (${#GEMINI_API_KEY} chars)"

# ── Google Cloud credentials check ───────────────────────────────────────────
check_gcp_credentials() {
    step "Checking Google Cloud Credentials"
    if [[ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
        if [[ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]]; then
            success "Service account key: $GOOGLE_APPLICATION_CREDENTIALS"
        else
            warn "GOOGLE_APPLICATION_CREDENTIALS set but file not found:"
            warn "  $GOOGLE_APPLICATION_CREDENTIALS"
            warn "Falling back to application default credentials"
        fi
    elif command -v gcloud &>/dev/null && \
         gcloud auth application-default print-access-token &>/dev/null 2>&1; then
        success "Application default credentials available"
    else
        warn "No GCP credentials detected"
        warn "Run: gcloud auth application-default login"
        warn "Firestore will use in-memory fallback — app will still start"
    fi
}

# ── Port conflict detection ───────────────────────────────────────────────────
check_port() {
    local port="$1"
    local in_use=false

    # Try lsof first (macOS + Linux), fall back to ss (Linux only)
    if command -v lsof &>/dev/null; then
        lsof -i :"$port" &>/dev/null 2>&1 && in_use=true || true
    elif command -v ss &>/dev/null; then
        ss -tlnp 2>/dev/null | grep -q ":$port " && in_use=true || true
    fi

    if [[ "$in_use" == true ]]; then
        warn "Port $port is already in use on this machine"
        warn "Options:"
        warn "  1. Stop the process using port $port"
        warn "  2. Set a different port: HOST_PORT=9090 ./deploy.sh $TARGET"
        if [[ "$TARGET" == "docker" ]]; then
            warn "  3. Run manually: docker run --env-file .env -p 9090:8080 ventlive-ai"
            warn "     Then update index.html: const BACKEND = \"http://localhost:9090\";"
        fi
        blank
        read -r -p "Continue anyway? [y/N] " response
        [[ "$response" =~ ^[Yy]$ ]] || exit 1
    else
        success "Port $port is available"
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
# TARGET: local
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "local" ]]; then

    step "Local Development Deployment"

    # ── Python version check ───────────────────────────────────────────────────
    step "Checking Python Version"
    PYTHON_BIN=""
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            # Extract major.minor as integers for comparison
            PY_MAJOR=$("$cmd" -c 'import sys; print(sys.version_info.major)')
            PY_MINOR=$("$cmd" -c 'import sys; print(sys.version_info.minor)')
            if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
                PYTHON_BIN="$cmd"
                break
            fi
        fi
    done

    if [[ -z "$PYTHON_BIN" ]]; then
        error "Python 3.11+ not found.\n\n  Install from: https://python.org/downloads\n  Or via pyenv: pyenv install 3.11.9"
    fi
    success "Python: $($PYTHON_BIN --version)"

    # ── Virtual environment ────────────────────────────────────────────────────
    step "Setting Up Virtual Environment"
    if [[ ! -d "venv" ]]; then
        info "Creating virtual environment..."
        "$PYTHON_BIN" -m venv venv
        success "Virtual environment created at ./venv"
    else
        info "Using existing virtual environment at ./venv"
    fi

    # Activate — handle both Unix and Windows paths
    if [[ -f "venv/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    elif [[ -f "venv/Scripts/activate" ]]; then
        # shellcheck disable=SC1091
        source venv/Scripts/activate
    else
        error "Could not activate virtual environment.\n\n  Delete ./venv and re-run: ./deploy.sh local"
    fi
    success "Virtual environment activated: $(which python)"

    # ── Install dependencies ───────────────────────────────────────────────────
    step "Installing Python Dependencies"
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    success "All dependencies installed from requirements.txt"

    # ── GCP credentials ────────────────────────────────────────────────────────
    check_gcp_credentials

    # ── Firestore connectivity test (non-fatal) ────────────────────────────────
    step "Testing Firestore Connectivity"
    "$PYTHON_BIN" - <<PYEOF 2>/dev/null \
        && success "Firestore reachable — cases will persist to cloud" \
        || warn "Firestore unavailable — in-memory fallback will activate on startup"
import os
from google.cloud import firestore
db = firestore.Client(project=os.environ.get("GCP_PROJECT", "ventlive-ai"))
list(db.collections())
PYEOF

    # ── Port check ─────────────────────────────────────────────────────────────
    step "Checking Port Availability"
    check_port "$PORT"

    # ── ngrok guidance ─────────────────────────────────────────────────────────
    step "HTTPS Tunnel Guidance"
    info "Browser microphone access requires HTTPS."
    blank
    if command -v ngrok &>/dev/null; then
        success "ngrok found: $(ngrok --version)"
        blank
        echo -e "  ${YELLOW}After the server starts, open a second terminal and run:${RESET}"
        echo -e "  ${BOLD}    ngrok http $PORT${RESET}"
        blank
        echo -e "  Then update ${BOLD}index.html${RESET} (~line 750):"
        echo -e "  ${BOLD}    const BACKEND = \"https://YOUR-NGROK-URL.ngrok-free.app\";${RESET}"
    else
        warn "ngrok not found"
        warn "Install from: https://ngrok.com/download"
        warn "Alternatives: Cloudflare Tunnel, localtunnel, VS Code port forwarding"
        blank
        info "For HTTP-only testing (no mic), access directly:"
        info "  http://localhost:$PORT"
    fi

    # ── Start server ───────────────────────────────────────────────────────────
    step "Starting VentLive AI Server"
    blank
    echo -e "  ${GREEN}Server:${RESET}     http://localhost:$PORT"
    echo -e "  ${GREEN}Health:${RESET}     http://localhost:$PORT/health"
    echo -e "  ${GREEN}API docs:${RESET}   http://localhost:$PORT/docs"
    echo -e "  ${GREEN}Logs API:${RESET}   http://localhost:$PORT/logs"
    echo -e "              (requires X-API-Key: $VENTLIVE_API_KEY header)"
    blank
    info "Press Ctrl+C to stop"
    blank

    # Export all required vars for the server process
    export GCP_PROJECT GCP_LOCATION GCP_MODEL GEMINI_API_KEY VENTLIVE_API_KEY

    # --reload enables hot-reload on source file changes (development only)
    uvicorn main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        --reload \
        --log-level info

fi

# ══════════════════════════════════════════════════════════════════════════════
# TARGET: docker
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "docker" ]]; then

    step "Local Docker Deployment"

    # ── Docker check ───────────────────────────────────────────────────────────
    if ! command -v docker &>/dev/null; then
        error "Docker not found.\n\n  Install from: https://docs.docker.com/get-docker/"
    fi
    success "Docker: $(docker --version)"

    # Check Docker daemon is running
    if ! docker info &>/dev/null 2>&1; then
        error "Docker daemon is not running.\n\n  Start Docker Desktop or run: sudo systemctl start docker"
    fi
    success "Docker daemon is running"

    # ── Stop existing container ────────────────────────────────────────────────
    step "Checking for Existing Containers"
    EXISTING=$(docker ps -aq --filter ancestor=ventlive-ai 2>/dev/null || true)
    if [[ -n "$EXISTING" ]]; then
        warn "Existing ventlive-ai container(s) found — stopping and removing"
        docker stop $(docker ps -q --filter ancestor=ventlive-ai) 2>/dev/null || true
        docker rm $(docker ps -aq --filter ancestor=ventlive-ai) 2>/dev/null || true
        success "Existing container(s) cleaned up"
    else
        info "No existing containers found"
    fi

    # ── Port conflict check ────────────────────────────────────────────────────
    step "Checking Port Availability"
    check_port "$HOST_PORT"

    # ── Build image ────────────────────────────────────────────────────────────
    step "Building Docker Image"
    info "Image: $IMAGE_TAG"
    info "This may take 2-3 minutes on first build (downloading base image + pip install)"
    blank

    docker build \
        --tag "$IMAGE_TAG" \
        --label "ventlive.version=4.0" \
        --label "ventlive.target=local-docker" \
        .

    success "Image built: $IMAGE_TAG"

    # Show image size
    IMAGE_SIZE=$(docker image inspect "$IMAGE_TAG" \
        --format '{{.Size}}' 2>/dev/null || echo "0")
    IMAGE_SIZE_MB=$(( IMAGE_SIZE / 1024 / 1024 ))
    info "Image size: ${IMAGE_SIZE_MB} MB"

    # ── Run container ──────────────────────────────────────────────────────────
    step "Starting Docker Container"

    # Security note: --env-file keeps secrets out of docker inspect and ps output
    # Never use -e GEMINI_API_KEY=value — visible in process list and docker inspect
    CONTAINER_ID=$(docker run \
        --env-file .env \
        --publish "$HOST_PORT:$DOCKER_PORT" \
        --name ventlive-ai-local \
        --detach \
        --restart unless-stopped \
        "$IMAGE_TAG")

    success "Container started: ${CONTAINER_ID:0:12}"

    # ── Wait for health check ──────────────────────────────────────────────────
    step "Waiting for Server to Be Ready"
    info "Allowing 15s for uvicorn startup + credential validation..."

    READY=false
    for i in {1..10}; do
        sleep 2
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            "http://localhost:$HOST_PORT/health" 2>/dev/null || echo "000")
        if [[ "$HTTP_STATUS" == "200" ]]; then
            READY=true
            break
        fi
        echo -n "."
    done
    blank

    if [[ "$READY" == true ]]; then
        success "Server is ready"

        # Show health response
        HEALTH=$(curl -s "http://localhost:$HOST_PORT/health" 2>/dev/null || echo "{}")
        blank
        echo -e "  ${GREEN}Health response:${RESET}"
        echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "  $HEALTH"
    else
        warn "Server did not respond within 20s"
        warn "Check logs: ./deploy.sh logs"
        blank
        info "Showing last 20 log lines:"
        docker logs --tail 20 ventlive-ai-local 2>/dev/null || true
    fi

    # ── Summary ────────────────────────────────────────────────────────────────
    step "Docker Deployment Complete"
    blank
    echo -e "  ${GREEN}Container:${RESET}   ventlive-ai-local"
    echo -e "  ${GREEN}Server:${RESET}      http://localhost:$HOST_PORT"
    echo -e "  ${GREEN}Health:${RESET}      http://localhost:$HOST_PORT/health"
    echo -e "  ${GREEN}API docs:${RESET}    http://localhost:$HOST_PORT/docs"
    blank
    echo -e "  ${YELLOW}Microphone access requires HTTPS.${RESET}"
    if command -v ngrok &>/dev/null; then
        echo -e "  Run in a second terminal: ${BOLD}ngrok http $HOST_PORT${RESET}"
        echo -e "  Then update index.html:   ${BOLD}const BACKEND = \"https://YOUR-URL.ngrok-free.app\";${RESET}"
    else
        echo -e "  Install ngrok: https://ngrok.com/download"
    fi
    blank
    echo -e "  ${CYAN}Useful commands:${RESET}"
    echo -e "    Stream logs:   ${BOLD}./deploy.sh logs${RESET}"
    echo -e "    Stop:          ${BOLD}./deploy.sh stop${RESET}"
    echo -e "    Rebuild:       ${BOLD}./deploy.sh stop && ./deploy.sh docker${RESET}"
    blank

    # Port conflict note
    if [[ "$HOST_PORT" != "8080" ]]; then
        warn "Running on non-standard port $HOST_PORT"
        warn "Update index.html: const BACKEND = \"http://localhost:$HOST_PORT\";"
    fi

fi

# ══════════════════════════════════════════════════════════════════════════════
# TARGET: cloudrun
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "cloudrun" ]]; then

    step "Google Cloud Run Deployment"

    # ── Tool checks ────────────────────────────────────────────────────────────
    if ! command -v gcloud &>/dev/null; then
        error "gcloud CLI not found.\n\n  Install from: https://cloud.google.com/sdk/docs/install\n  Then run:     gcloud auth login"
    fi
    success "gcloud: $(gcloud --version | head -1)"

    if ! command -v docker &>/dev/null; then
        error "Docker not found.\n\n  Install from: https://docs.docker.com/get-docker/"
    fi
    success "Docker: $(docker --version)"

    # ── Set active project ─────────────────────────────────────────────────────
    step "Configuring GCP Project"
    ACTIVE_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
    if [[ "$ACTIVE_PROJECT" != "$GCP_PROJECT" ]]; then
        info "Setting active project to: $GCP_PROJECT"
        gcloud config set project "$GCP_PROJECT" --quiet
    fi
    success "Active GCP project: $GCP_PROJECT"

    # ── Enable required APIs ───────────────────────────────────────────────────
    step "Enabling Required GCP APIs"
    REQUIRED_APIS=(
        "run.googleapis.com"
        "cloudbuild.googleapis.com"
        "containerregistry.googleapis.com"
        "aiplatform.googleapis.com"
        "firestore.googleapis.com"
    )
    for api in "${REQUIRED_APIS[@]}"; do
        info "Enabling $api..."
        gcloud services enable "$api" \
            --project="$GCP_PROJECT" \
            --quiet
        success "$api"
    done

    # ── Firestore database check ───────────────────────────────────────────────
    step "Checking Firestore Database"
    if gcloud firestore databases list \
        --project="$GCP_PROJECT" \
        --quiet 2>/dev/null | grep -q "projects/"; then
        success "Firestore database found"
    else
        warn "Firestore database not found — creating default database"
        gcloud firestore databases create \
            --region="$GCP_LOCATION" \
            --project="$GCP_PROJECT" \
            --quiet \
        && success "Firestore database created" \
        || warn "Could not create Firestore database — app will use in-memory fallback"
    fi

    # ── Build image via Cloud Build ────────────────────────────────────────────
    step "Building Docker Image via Cloud Build"
    GCR_IMAGE="gcr.io/$GCP_PROJECT/$SERVICE_NAME:latest"
    info "Image: $GCR_IMAGE"
    info "Building in Google Cloud — this takes 3-5 minutes on first run"
    blank

    gcloud builds submit \
        --tag "$GCR_IMAGE" \
        --project "$GCP_PROJECT" \
        --quiet

    success "Image built and pushed to Container Registry"
    info "Image: $GCR_IMAGE"

    # ── Deploy to Cloud Run ────────────────────────────────────────────────────
    step "Deploying to Cloud Run"
    info "Service:  $SERVICE_NAME"
    info "Region:   $GCP_LOCATION"
    info "Model:    $GCP_MODEL"
    blank

    # IMPORTANT: --concurrency 80 + --min-instances 1
    # VentLive AI MUST run with a single process (--workers 1 in Dockerfile CMD).
    # Cloud Run concurrency controls simultaneous requests per instance,
    # not OS processes. min-instances 1 prevents cold starts during ICU use.
    # timeout 3600 covers extended Gemini Live voice sessions.
    gcloud run deploy "$SERVICE_NAME" \
        --image "$GCR_IMAGE" \
        --platform managed \
        --region "$GCP_LOCATION" \
        --project "$GCP_PROJECT" \
        --allow-unauthenticated \
        --min-instances 1 \
        --max-instances 10 \
        --concurrency 80 \
        --timeout 3600 \
        --memory 512Mi \
        --cpu 1 \
        --set-env-vars "GCP_PROJECT=$GCP_PROJECT" \
        --set-env-vars "GCP_LOCATION=$GCP_LOCATION" \
        --set-env-vars "GCP_MODEL=$GCP_MODEL" \
        --set-env-vars "GEMINI_API_KEY=$GEMINI_API_KEY" \
        --set-env-vars "VENTLIVE_API_KEY=$VENTLIVE_API_KEY" \
        

    # ── Retrieve service URL ───────────────────────────────────────────────────
    SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
        --platform managed \
        --region "$GCP_LOCATION" \
        --project "$GCP_PROJECT" \
        --format "value(status.url)" 2>/dev/null || echo "")

    if [[ -z "$SERVICE_URL" ]]; then
        warn "Could not retrieve service URL automatically"
        warn "Check GCP Console: https://console.cloud.google.com/run"
    else
        # ── Health check against live service ─────────────────────────────────
        step "Verifying Deployment"
        info "Running health check against: $SERVICE_URL/health"

        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            "$SERVICE_URL/health" 2>/dev/null || echo "000")

        if [[ "$HTTP_STATUS" == "200" ]]; then
            success "Service is live and healthy (HTTP $HTTP_STATUS)"
            HEALTH=$(curl -s "$SERVICE_URL/health" 2>/dev/null || echo "{}")
            blank
            echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "  $HEALTH"
        else
            warn "Health check returned HTTP $HTTP_STATUS"
            warn "Service may still be initializing — check in 60s"
        fi

        # ── Summary ───────────────────────────────────────────────────────────
        step "Cloud Run Deployment Complete"
        blank
        echo -e "  ${GREEN}Service URL:${RESET}   $SERVICE_URL"
        echo -e "  ${GREEN}Health check:${RESET}  $SERVICE_URL/health"
        echo -e "  ${GREEN}API docs:${RESET}      $SERVICE_URL/docs"
        echo -e "  ${GREEN}GCP Console:${RESET}   https://console.cloud.google.com/run/detail/$GCP_LOCATION/$SERVICE_NAME"
        blank
        echo -e "  ${YELLOW}Required — update index.html (~line 750):${RESET}"
        echo -e "  ${BOLD}    const BACKEND = \"$SERVICE_URL\";${RESET}"
        blank
        echo -e "  Then deploy index.html to:"
        echo -e "    Firebase Hosting, Cloud Storage static site, or any CDN"
        blank
    fi

fi

# ══════════════════════════════════════════════════════════════════════════════
# TARGET: vm
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$TARGET" == "vm" ]]; then

    step "Google Compute Engine VM Deployment"

    # ── Tool check ─────────────────────────────────────────────────────────────
    if ! command -v gcloud &>/dev/null; then
        error "gcloud CLI not found.\n\n  Install from: https://cloud.google.com/sdk/docs/install"
    fi
    success "gcloud: $(gcloud --version | head -1)"

    info "VM instance: $VM_INSTANCE"
    info "VM zone:     $VM_ZONE"
    info "VM user:     $VM_USER"
    info "Server port: $PORT"

    # ── Verify VM instance exists ──────────────────────────────────────────────
    step "Verifying VM Instance"
    if ! gcloud compute instances describe "$VM_INSTANCE" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --quiet &>/dev/null; then
        error "VM instance '$VM_INSTANCE' not found in zone '$VM_ZONE'.\n\n  Set VM_INSTANCE and VM_ZONE in your .env file.\n  Or create a VM: https://console.cloud.google.com/compute/instances"
    fi

    VM_STATUS=$(gcloud compute instances describe "$VM_INSTANCE" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --format "value(status)" 2>/dev/null || echo "UNKNOWN")

    if [[ "$VM_STATUS" != "RUNNING" ]]; then
        error "VM instance '$VM_INSTANCE' is not running (status: $VM_STATUS).\n\n  Start it: gcloud compute instances start $VM_INSTANCE --zone $VM_ZONE"
    fi
    success "VM instance $VM_INSTANCE is RUNNING in $VM_ZONE"

    # ── Copy project files to VM ───────────────────────────────────────────────
    step "Copying Project Files to VM"

    PROJECT_FILES=(
        "main.py"
        "live_session.py"
        "gemini_handler.py"
        "vent_reasoning.py"
        "case_memory.py"
        "index.html"
        "requirements.txt"
    )

    # Create target directory on VM first
    gcloud compute ssh "$VM_USER@$VM_INSTANCE" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --command "mkdir -p /home/$VM_USER/ventlive-ai" \
        --quiet

    # Copy files
    gcloud compute scp \
        "${PROJECT_FILES[@]}" \
        "$VM_USER@$VM_INSTANCE:/home/$VM_USER/ventlive-ai/" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --compress \
        --quiet

    success "Files copied to VM: /home/$VM_USER/ventlive-ai/"

    # ── Execute remote setup ───────────────────────────────────────────────────
    step "Installing and Starting VentLive AI on VM"
    info "This may take 2-3 minutes on first run (pip install)"

    # Build remote script — uses heredoc to avoid quoting complexity
    REMOTE_SCRIPT=$(cat <<REMOTE
set -euo pipefail

echo "[VM] Working directory: /home/$VM_USER/ventlive-ai"
cd /home/$VM_USER/ventlive-ai

# Install Python 3.11 if not present
if ! python3 --version 2>/dev/null | grep -q "3\.1[1-9]"; then
    echo "[VM] Installing Python 3.11..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip
fi
echo "[VM] Python: \$(python3 --version)"

# Install dependencies
echo "[VM] Installing Python dependencies..."
pip3 install --quiet --upgrade pip
pip3 install --quiet -r requirements.txt
echo "[VM] Dependencies installed"

# Write environment file
# Security: file is written with restricted permissions
cat > /home/$VM_USER/ventlive-ai/.env <<ENV
GCP_PROJECT=$GCP_PROJECT
GCP_LOCATION=$GCP_LOCATION
GCP_MODEL=$GCP_MODEL
GEMINI_API_KEY=$GEMINI_API_KEY
VENTLIVE_API_KEY=$VENTLIVE_API_KEY
ENV
chmod 600 /home/$VM_USER/ventlive-ai/.env
echo "[VM] Environment file written (permissions: 600)"

# Stop any existing instance cleanly
if pgrep -f "uvicorn main:app" > /dev/null 2>&1; then
    echo "[VM] Stopping existing server..."
    pkill -SIGTERM -f "uvicorn main:app" 2>/dev/null || true
    sleep 3
    # Force kill if still running
    pkill -SIGKILL -f "uvicorn main:app" 2>/dev/null || true
    echo "[VM] Existing server stopped"
fi

# Export environment variables
export \$(grep -v '^#' /home/$VM_USER/ventlive-ai/.env | xargs)

# Start server in background with nohup
echo "[VM] Starting VentLive AI server on port $PORT..."
nohup uvicorn main:app \
    --host 0.0.0.0 \
    --port $PORT \
    --workers 1 \
    --log-level info \
    > /home/$VM_USER/ventlive-ai/ventlive.log 2>&1 &

SERVER_PID=\$!
echo "[VM] Server PID: \$SERVER_PID"
echo "\$SERVER_PID" > /home/$VM_USER/ventlive-ai/ventlive.pid

# Wait for startup
echo "[VM] Waiting for server startup..."
sleep 5

# Health check
HTTP_STATUS=\$(curl -s -o /dev/null -w "%{http_code}" \
    "http://localhost:$PORT/health" 2>/dev/null || echo "000")

if [ "\$HTTP_STATUS" = "200" ]; then
    echo "[VM] ✅ Server is healthy (HTTP \$HTTP_STATUS)"
    curl -s "http://localhost:$PORT/health"
else
    echo "[VM] ⚠️  Health check returned HTTP \$HTTP_STATUS"
    echo "[VM] Last 20 lines of server log:"
    tail -20 /home/$VM_USER/ventlive-ai/ventlive.log 2>/dev/null || true
fi

echo "[VM] Log file: /home/$VM_USER/ventlive-ai/ventlive.log"
echo "[VM] PID file: /home/$VM_USER/ventlive-ai/ventlive.pid"
REMOTE
)

    gcloud compute ssh "$VM_USER@$VM_INSTANCE" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --command "$REMOTE_SCRIPT"

    # ── Get VM external IP ─────────────────────────────────────────────────────
    VM_IP=$(gcloud compute instances describe "$VM_INSTANCE" \
        --zone "$VM_ZONE" \
        --project "$GCP_PROJECT" \
        --format "value(networkInterfaces[0].accessConfigs[0].natIP)" \
        2>/dev/null || echo "")

    # ── Summary ────────────────────────────────────────────────────────────────
    step "VM Deployment Complete"
    blank
    if [[ -n "$VM_IP" ]]; then
        echo -e "  ${GREEN}VM External IP:${RESET}  $VM_IP"
        echo -e "  ${GREEN}Health check:${RESET}    http://$VM_IP:$PORT/health"
        echo -e "  ${GREEN}Server log:${RESET}      SSH → tail -f ~/ventlive-ai/ventlive.log"
        blank
        echo -e "  ${YELLOW}Required — update index.html (~line 750):${RESET}"
        echo -e "  ${BOLD}    const BACKEND = \"http://$VM_IP:$PORT\";${RESET}"
    else
        warn "Could not retrieve VM external IP"
        warn "Check GCP Console: https://console.cloud.google.com/compute/instances"
    fi
    blank
    echo -e "  ${YELLOW}HTTP only — browser microphone requires HTTPS.${RESET}"
    echo -e "  For HTTPS on the VM, use ngrok:"
    echo -e "  ${BOLD}    gcloud compute ssh $VM_USER@$VM_INSTANCE -- ngrok http $PORT${RESET}"
    blank
    echo -e "  ${CYAN}Useful SSH commands:${RESET}"
    echo -e "    Stream logs:   gcloud compute ssh $VM_USER@$VM_INSTANCE -- tail -f ~/ventlive-ai/ventlive.log"
    echo -e "    Stop server:   gcloud compute ssh $VM_USER@$VM_INSTANCE -- pkill -f 'uvicorn main:app'"
    echo -e "    Restart:       ./deploy.sh vm"
    blank

fi

# ── Final success message ─────────────────────────────────────────────────────
echo -e "${BOLD}${GREEN}"
echo "══════════════════════════════════════════════════════════════════"
echo "  VentLive AI — deployment complete"
echo "  Target: $TARGET"
echo "══════════════════════════════════════════════════════════════════"
echo -e "${RESET}"
