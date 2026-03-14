# VentLive AI
### Real-Time Voice AI Clinical Decision Support for ICU Mechanical Ventilation

> Speak naturally at the bedside. Hear a complete, evidence-based ventilation
> assessment in seconds. Powered by Gemini Live 2.5 Flash Native Audio on
> Vertex AI.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Features](#features)
3. [Technologies Used](#technologies-used)
4. [Third-Party Integrations](#third-party-integrations)
5. [Architecture Diagram](#architecture-diagram)
6. [Google Cloud Deployment Proof](#google-cloud-deployment-proof)
7. [Spin-Up Instructions](#spin-up-instructions)
   - [Prerequisites](#prerequisites)
   - [Local Development](#local-development)
   - [Cloud Deployment](#cloud-deployment)
8. [Project Structure](#project-structure)

---

## What It Does

VentLive AI is a real-time, voice-interactive clinical decision support agent
for ICU physicians managing mechanically ventilated patients.

A physician speaks naturally at the bedside — or types in noisy environments —
and VentLive AI responds immediately with a fully structured, evidence-based
assessment covering four clinical scenarios:

- **ARDS** (Acute Respiratory Distress Syndrome)
- **COPD** (Chronic Obstructive Pulmonary Disease / Hypercapnic Respiratory Failure)
- **COPD + ARDS Overlap** (conflicting PEEP goals resolved simultaneously)
- **Weaning and Liberation** (SBT readiness, SBT failure management,
  extubation criteria, post-operative patients)

Two Gemini models work in tandem with strictly separated roles.
**Gemini Live 2.5 Flash Native Audio** handles real-time bidirectional voice:
receiving physician speech as a continuous audio stream, managing natural
turn-taking, and delivering spoken assessments. It operates under a hard
constraint — it never generates clinical content from its own knowledge.
Every word it speaks is verified text injected from the reasoning engine
after the full assessment is complete.
**Gemini 2.5 Flash** handles clinical extraction: parsing structured
ventilator parameters, ABG values, hemodynamics, and weaning indicators
from natural speech — including voice recognition artifacts and mixed
Arabic/English ICU terminology. When the Gemini extraction API is
unavailable, 22 purpose-built regex extractors activate automatically
and the full reasoning engine continues. The system never goes silent.

This architecture enforces a deliberate sequence: the complete evidence-based
clinical assessment finishes first, then the result is injected as spoken
text. The physician hears a complete, structured answer every time —
never a half-formed one generated mid-reasoning.

---

## Features

### Voice Interface
- **Always-on microphone** — no push-to-talk required
- **Natural interruption** — doctor can barge in mid-sentence; Gemini Live
  detects overlapping speech server-side and stops immediately
- **Mute toggle** — freezes mic audio while keeping session alive
- **Automatic session reconnect** — Gemini Live sessions close after each
  turn (code 1000/1011); the server reconnects silently without the
  browser WebSocket ever dropping

### Clinical Extraction
- Extracts 22 structured clinical fields from free-form speech:
  ventilator mode, TV, PEEP, FiO₂, RR, Pplat, Ppeak, auto-PEEP,
  inspiratory flow, pH, PaCO₂, PaO₂, HCO₃, SpO₂, BP, HR, MAP, GCS,
  cough strength, SBT status, vasopressor dose, baseline PaCO₂, prior PaCO₂
- Handles Arabic/English mixed input, voice recognition artifacts,
  noise tags (`<noise>`, `[inaudible]`), and speech-to-text errors
- Gemini 2.5 Flash primary extraction with 3-retry / 20-second timeout
  and exponential backoff (1s → 2s → 4s)
- 22 regex fallbacks activate automatically on Gemini failure —
  full SCCM reasoning continues with warning banner

### Real-Time Metric Calculation
- **Driving Pressure** (Pplat − PEEP) — tiered danger at ≥13, ≥15, ≥20 cmH₂O
- **P/F Ratio** — Berlin ARDS severity with prone and ECMO thresholds
- **S/F Ratio** — Rice 2007 surrogate when ABG unavailable (SpO₂ ≤ 97% gate)
- **TV/IBW** — Devine formula IBW with ARDSNet minimum floors
- **RSBI** — displayed for weaning assessments only
- **MAP** — calculated from systolic/diastolic when not directly reported

### SCCM Assessment Card
- **Color-coded severity** — green (Stable), orange (Worsening), red (Critical)
- **Numbered, color-coded next steps** — red for critical, amber for warnings,
  green for standard
- **Safety flags** — rendered only when clinically meaningful; absent when
  no concerns exist (no alarm fatigue)
- **Trend badges** — PaO₂, pH, MAP, PEEP direction from second assessment onward
- **Context-aware staleness detection** — 30 min post-vent-change,
  2 h for Critical/Worsening, 8 h for Stable
- **ARDSNet PEEP/FiO₂ table** — Lower and Higher PEEP tables cross-referenced
  automatically; ATS/ESICM divergence flagged inline
- **Guideline citations** — every recommendation carries its source inline
  (ATS 2024, ESICM 2023, GOLD 2024, PROSEVA, EOLIA, etc.)
- **Smart Q&A** — follow-up questions answered from the last verified SCCM
  card without generating a new assessment card

### Clinical Safety Guards
- Negation-aware SBT parsing (`_is_negated()` with 50-char window,
  20+ negation patterns) — "was not tolerated" never triggers pass path
- Full-support guard — prevents numeric deterioration signals from being
  misread as SBT failure when patient is on full ventilatory support
- COPD over-correction guard — PaCO₂ drop > 20 mmHg or > 50% within
  24 h triggers intracranial haemorrhage risk warning (GOLD 2024)
- COPD baseline PaCO₂ protection — warns before correcting below
  patient's chronic baseline
- SpO₂ desaturation guard — never carries forward old SpO₂ if a drop
  is reported in the same input

### Patient Management
- Create up to 50 patients per Firestore collection (configurable)
- Persistent case history — vent settings, ABGs, hemodynamics, events,
  AI assessments, SBT attempts
- Paginated patient list (10 per page)
- Baseline PaCO₂ stored per patient for COPD carry-forward
- Session history restored on patient re-selection

### Resilience
- Firestore unavailable → in-memory dict fallback (automatic, silent)
- Gemini Flash fails → 22 regex extractors → full SCCM reasoning continues
- Gemini Live closes → reconnect loop (exponential backoff, no hard cap)
- WebSocket offline → REST `/analyze` fallback with 10-second timer

### Responsive Design
- 5 breakpoints: desktop (≥1024px) through very small phone (<360px)
- Android Chrome 100vh fix (dynamic toolbar)
- iOS AudioContext unlock (silent buffer on first user gesture)
- Safe-area inset support for notched devices

---

## Technologies Used

| Category | Technology |
|---|---|
| **Backend language** | Python 3 |
| **Frontend** | JavaScript (ES6+), HTML5, CSS3 — single-file SPA, no build tools |
| **Web framework** | FastAPI + Uvicorn (ASGI) |
| **Data validation** | Pydantic |
| **Cloud platform** | Google Cloud Platform |
| **AI — voice I/O** | Gemini Live 2.5 Flash Native Audio (Vertex AI) |
| **AI — NLU extraction** | Gemini 2.5 Flash (Google GenAI API) |
| **AI SDK** | Google GenAI Python SDK (`google-genai`) |
| **Database** | Google Cloud Firestore (+ in-memory dict fallback) |
| **Real-time comms** | WebSocket (FastAPI native) |
| **Audio capture** | Web Audio API, AudioWorklet (16kHz PCM) |
| **Audio playback** | Web Audio API, AudioContext (24kHz PCM) |
| **Auth** | API Key (X-API-Key header / query param for WebSocket) |
| **Dev tunnel** | ngrok (HTTPS reverse proxy for local development) |
| **Clinical logic** | Pure Python deterministic rule engine (no external calls) |

---

## Third-Party Integrations

### Google Gemini — Gemini Live 2.5 Flash Native Audio
- **Provider:** Google Cloud / Vertex AI
- **Purpose:** Real-time bidirectional voice streaming, speech-to-text
  transcription, text-to-speech output (Puck voice, en-US), server-side
  voice activity detection, natural interruption (barge-in)
- **SDK:** `google-genai` Python SDK — `client.aio.live.connect()`
- **Model string:** `gemini-live-2.5-flash-native-audio`
- **Authentication:** Vertex AI service account (GCP_PROJECT env var)
- **Configuration:** LiveConnectConfig with AudioTranscriptionConfig,
  RealtimeInputConfig, SpeechConfig, AutomaticActivityDetection

### Google Gemini — Gemini 2.5 Flash (Text)
- **Provider:** Google AI / Vertex AI
- **Purpose:** Clinical NLU — extracting structured ventilator parameters,
  ABG values, hemodynamics, and weaning indicators from free-form physician
  speech transcripts; generating clinical narratives and safety flags
- **SDK:** `google-genai` Python SDK — `client.aio.models.generate_content()`
- **Model string:** `gemini-2.5-flash`
- **Authentication:** GEMINI_API_KEY env var (or Vertex AI project fallback)
- **Call pattern:** 3 retries, 20-second asyncio timeout per attempt,
  exponential backoff 1s → 2s → 4s; returns None on total failure
  triggering regex-only fallback mode

### Google Cloud Firestore
- **Provider:** Google Cloud Platform
- **Purpose:** Persistent patient case storage — ventilator settings history,
  ABG history, hemodynamics, clinical events, AI assessments, SBT attempts
- **SDK:** `google-cloud-firestore` Python SDK
- **Collection:** `vent_cases` (one document per patient, UUID4 key)
- **Write pattern:** ArrayUnion for append-only time-series fields;
  full document set for case creation
- **Fallback:** In-memory Python dict with pending sync queue when
  Firestore is unavailable

### Clinical Guidelines (Embedded — No External API)
The following published guidelines and trials are encoded as deterministic
rule-based logic within `vent_reasoning.py`. They are not called as
external services:

| Guideline | Use |
|---|---|
| ATS 2024 | ARDS mechanical ventilation |
| ESICM 2023 | Ventilation guidelines (divergence from ATS noted) |
| GOLD 2024 | COPD management, auto-PEEP, RR ceilings |
| ATS/ACCP 2017 | Liberation from mechanical ventilation |
| AARC 2024 | Clinical practice guidelines |
| ARDSNet ARMA | PEEP/FiO₂ tables, lung-protective ventilation |
| PROSEVA Trial | Prone positioning (NNT=6 for severe ARDS) |
| EOLIA Trial | VV-ECMO referral criteria |
| Berlin Definition | ARDS classification |
| Rice 2007 | S/F ratio validation (SpO₂ surrogate for P/F) |

---

## Architecture Diagram

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full annotated
system diagram including:
- Complete data flow from doctor speech to spoken response
- Module dependency DAG
- Graceful degradation chain
- WebSocket message protocol (all 17 message types)

---

## Google Cloud Deployment Proof

VentLive AI runs exclusively on Google Cloud infrastructure.

### GCP Services Active in Submitted Code

**1. Vertex AI — Gemini Live (live_session.py, lines 1–15)**
```python
GCP_PROJECT  = os.environ.get("GCP_PROJECT",  "ventlive-ai")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
GCP_MODEL    = os.environ.get("GCP_MODEL",    "gemini-live-2.5-flash-native-audio")

def get_live_client():
    return genai.Client(
        vertexai=True,
        project=GCP_PROJECT,
        location=GCP_LOCATION
    )
