

import json, asyncio, base64, traceback, os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from google.genai import types

from case_memory import (
    create_case, get_case, list_cases, delete_case, delete_all_cases,
    update_vent_settings, update_abg, update_hemodynamics,
    add_ai_assessment, add_event, get_trend,
    storage_status, set_baseline_paco2
)
from gemini_handler import process_text_input
from vent_reasoning import generate_sccm_recommendation
from live_session import build_live_config, get_live_client, run_sccm_analysis, run_sccm_then_speak, _run_clinical_pipeline

GCP_MODEL = os.environ.get("GCP_MODEL", "gemini-live-2.5-flash-native-audio")

API_KEY = os.environ.get("VENTLIVE_API_KEY", "ventlive-demo-2026")
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: str = Depends(_api_key_header)):
    if key != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key"
        )

app = FastAPI(title="VentLive AI", version="4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _is_injection_readback(captured: str, last_injection: str) -> bool:
    """
    Detects whether a voice transcript is Gemini reading back
    an injected script rather than the doctor speaking.

    Uses three checks:
    1. Injection is empty → always real doctor input
    2. Captured text is SHORT (< 20 words) and injection is LONG
       → doctor questions are short, injections are long
       → if captured is short it cannot be a readback
    3. Word overlap → if > 40% of captured words appear in
       the injection text → it's a readback

    This approach works across session boundaries because it
    compares TEXT not a boolean flag.
    """
    if not last_injection:
        return False

    captured_clean  = captured.lower().strip()
    injection_clean = last_injection.lower().strip()

    # Short captured text (< 20 words) cannot be a readback
    # of a long injection — doctor questions are always short
    captured_words  = captured_clean.split()
    injection_words = injection_clean.split()

    if len(captured_words) < 20 and len(injection_words) > 30:
        return False

    # Word overlap check
    # If > 40% of captured words appear in injection → readback
    if len(captured_words) == 0:
        return False

    captured_set  = set(captured_words)
    injection_set = set(injection_words)

    # Remove common stop words that appear in both doctor speech and injections
    stop_words = {
        "the", "is", "a", "an", "of", "to", "and", "in", "for",
        "this", "that", "it", "be", "are", "was", "with", "at",
        "by", "not", "but", "as", "or", "if", "now", "current",
        "patient", "please", "provide", "critical", "alert"
    }
    captured_meaningful  = captured_set  - stop_words
    injection_meaningful = injection_set - stop_words

    if len(captured_meaningful) == 0:
        return False

    overlap = len(captured_meaningful & injection_meaningful)
    overlap_ratio = overlap / len(captured_meaningful)

    is_readback = overlap_ratio > 0.40

    if is_readback:
        print(f"[ReadbackDetect] overlap={overlap_ratio:.2f} "
              f"captured='{captured[:40]}' "
              f"injection='{last_injection[:40]}'")

    return is_readback

@app.middleware("http")
async def ngrok_bypass(request: Request, call_next):
    if request.method == "OPTIONS":
        return JSONResponse(
            content={"status": "ok"},
            headers={
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, PUT, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age":       "86400",
                "ngrok-skip-browser-warning":   "true",
            }
        )
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"]   = "true"
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, PUT, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"

    # Skip strict security headers for Swagger UI routes
    _docs_routes = ["/docs", "/redoc", "/openapi.json"]
    if any(request.url.path.startswith(r) for r in _docs_routes):
        return response

    # Security headers (applied to all non-docs routes)
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]         = "DENY"
    response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]      = "geolocation=(), camera=(), payment=()"


    # Content Security Policy
    # - default-src: deny everything not explicitly listed
    # - script-src: allow inline scripts (needed for single-file HTML app)
    # - connect-src: allow WebSocket + fetch to any https (ngrok URL changes each restart)
    # - media-src: allow microphone audio blobs
    # - style-src: allow inline styles
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "connect-src 'self' https: wss:; "
        "media-src 'self' blob:; "
        "img-src 'self' data: cdn.jsdelivr.net; "
        "font-src 'self' cdn.jsdelivr.net; "
        "frame-ancestors 'none';"
    )
    return response

class NewCaseRequest(BaseModel):
    diagnosis:  str
    height_cm:  Optional[float] = None
    sex:        Optional[str]   = "male"

class TextInputRequest(BaseModel):
    case_id:    str
    input_text: str


# ── In-memory log capture ─────────────────────────────────
import logging as _logging
import sys as _sys

_LOG_BUFFER = []

class _BufferHandler(_logging.Handler):
    def emit(self, record):
        _LOG_BUFFER.append(self.format(record))
        if len(_LOG_BUFFER) > 200:
            _LOG_BUFFER.pop(0)

_buf_handler = _BufferHandler()
_buf_handler.setFormatter(_logging.Formatter("%(asctime)s %(message)s"))
_logging.getLogger().addHandler(_buf_handler)




class _BufferHandler(_logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _LOG_BUFFER.append(msg)
            if len(_LOG_BUFFER) > 200:
                _LOG_BUFFER.pop(0)
        except Exception:
            pass  # never crash on logging

_buf_handler = _BufferHandler()
_buf_handler.setFormatter(_logging.Formatter("%(message)s"))

# Capture uvicorn + app logs into buffer
for _logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", ""]:
    _lg = _logging.getLogger(_logger_name)
    _lg.addHandler(_buf_handler)

# Redirect print() statements to the root logger instead of patching builtins
# This keeps print() working normally everywhere while also capturing output
_logging.basicConfig(
    level=_logging.INFO,
    format="%(message)s",
    handlers=[_logging.StreamHandler(_sys.stdout)]
)

# Custom print that logs instead of patching builtins globally
def log(msg, *args):
    """Use log() instead of print() in this file for buffer capture."""
    full = str(msg) + (" " + " ".join(str(a) for a in args) if args else "")
    _LOG_BUFFER.append(full)
    if len(_LOG_BUFFER) > 200:
        _LOG_BUFFER.pop(0)
    _logging.getLogger("ventlive").info(full)

# Patch builtins.print globally — captures all modules including live_session.py
import builtins as _builtins
_orig_print = _builtins.print
def _captured_print(*args, **kwargs):
    try:
        msg = " ".join(str(a) for a in args)
        _LOG_BUFFER.append(msg)
        if len(_LOG_BUFFER) > 200:
            _LOG_BUFFER.pop(0)
    except Exception:
        pass
    _orig_print(*args, **kwargs)
_builtins.print = _captured_print

@app.get("/logs")
async def get_logs(auth=Depends(verify_api_key)):
    return {"logs": _LOG_BUFFER[-50:]}

@app.get("/health")
async def health():
    from case_memory import storage_status
    st = storage_status()
    return {
        "status":   "ok",
        "service":  "VentLive AI v4.0",
        "model":    GCP_MODEL,
        "platform": "Vertex AI",
        "storage":  st
    }

@app.get("/storage/status")
async def get_storage_status():
    from case_memory import storage_status
    return storage_status()

@app.get("/live/status")
async def live_status():
    return {
        "status":   "ok",
        "model":    GCP_MODEL,
        "platform": "Vertex AI",
        "location": os.environ.get("GCP_LOCATION", "us-central1")
    }

@app.post("/cases/new")
async def new_case(req: NewCaseRequest, auth=Depends(verify_api_key)):
    case = create_case(req.diagnosis, req.height_cm, req.sex)
    return {"success": True, "case": case}

@app.get("/cases")
async def list_cases_endpoint(
    limit: int = 20,
    offset: int = 0,
    auth=Depends(verify_api_key)
):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 1-100")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    return list_cases(limit=limit, offset=offset)

@app.options("/cases")
async def options_cases():
    return {"status": "ok"}

@app.get("/cases/{case_id}")
async def get_case_by_id(case_id: str, auth=Depends(verify_api_key)):
    case = get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return {"case": case}

@app.delete("/cases/{case_id}")
async def delete_case_endpoint(case_id: str, auth=Depends(verify_api_key)):
    if not delete_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return {"success": True}

@app.delete("/cases")
async def delete_all_cases_endpoint(auth=Depends(verify_api_key)):
    count = delete_all_cases()
    return {"success": True, "deleted": count}

@app.post("/cases/{case_id}/baseline_paco2")
async def set_baseline_paco2_endpoint(case_id: str, req: dict, auth=Depends(verify_api_key)):
    from case_memory import set_baseline_paco2
    val = req.get("value")
    if val is None:
        raise HTTPException(status_code=400, detail="value required")
    if not set_baseline_paco2(case_id, val):
        raise HTTPException(status_code=404, detail="Case not found")
    return {"success": True, "baseline_paco2": val}

@app.options("/cases/{case_id}")
async def options_case(case_id: str):
    return {"status": "ok"}

@app.post("/analyze")
async def analyze(req: TextInputRequest, auth=Depends(verify_api_key)):
    try:
        case = get_case(req.case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")

        from live_session import _run_clinical_pipeline
        result = await _run_clinical_pipeline(req.case_id, case, req.input_text)

        if len(result) == 6:
            # Question path — answer only, no SCCM card
            narrative, safety_flags, extracted, rec, trend, answer = result
            return {
                "success":          True,
                "type":             "question_answer",
                "gemini_narrative": answer,
                "safety_flags":     safety_flags,
                "extracted_data":   extracted,
            }
        else:
            narrative, safety_flags, extracted, rec, trend = result
            return {
                "success":             True,
                "type":                "assessment",
                "gemini_narrative":    narrative,
                "safety_flags":        safety_flags,
                "sccm_recommendation": rec,
                "extracted_data":      extracted,
                "trend":               trend
            }

    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════
# TRUE GEMINI LIVE API WEBSOCKET — Vertex AI
#
# ARCHITECTURE: Persistent browser WebSocket + internal
# Gemini session reconnect loop.
#
# KEY FIX: The browser WebSocket stays alive across all
# Gemini session reconnects. A shared asyncio.Queue bridges
# browser messages to whichever Gemini session is currently
# alive. When Gemini closes with 1000 (after each turn),
# we silently reconnect without the browser ever knowing.
# ════════════════════════════════════════════════════════════
@app.websocket("/ws/live/{case_id}")
async def live_websocket(websocket: WebSocket, case_id: str, api_key: str = ""):
    await websocket.accept()
    if api_key != API_KEY:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Unauthorized"
        }))
        await websocket.close(code=4401)
        return
    print(f"[LiveWS] Connected: {case_id[:8]}...")

    case = get_case(case_id)
    if not case:
        await websocket.send_text(json.dumps({
            "type": "error", "message": "Case not found"
        }))
        await websocket.close()
        return


    client = get_live_client()

    # ── Shared state across session reconnects ────────────
    # msg_queue: browser messages waiting to reach Gemini
    # session_ref: always points to the CURRENT live session
    # current_analysis_task: cancellable SCCM task
    msg_queue                = asyncio.Queue()
    session_ref              = [None]
    current_analysis_task    = [None]
    doctor_transcript_buffer = [""]
    turn_generation          = [0]
    browser_alive            = [True]
    last_injection_text      = [""]
    pending_speech_inject    = [None]
    session_lock             = asyncio.Lock()



    # ── Browser reader — runs for the ENTIRE WebSocket life ──
    # Puts all browser messages into msg_queue.
    # Never restarts. Only exits on WebSocketDisconnect.
    async def browser_reader():
        try:
            while True:
                raw = await websocket.receive_text()
                await msg_queue.put(json.loads(raw))
        except WebSocketDisconnect:
            browser_alive[0] = False   # ← mark dead before queuing
            # Signal the session loop to stop
            await msg_queue.put({"type": "__disconnect__"})
        except Exception as e:
            print(f"[BrowserReader] Error: {e}")
            browser_alive[0] = False   # ← mark dead before queuing
            await msg_queue.put({"type": "__disconnect__"})

    browser_task = asyncio.create_task(browser_reader())

    # ── Session reconnect loop ────────────────────────────
    # Each iteration opens one Gemini session.
    # When it dies (1000 / 1011), we loop and open a new one.
    # The browser never sees this — its WebSocket stays open.
    session_count = 0
    try:
        while True:
            # Stop reconnecting if browser already disconnected
            if not browser_alive[0]:
                print(f"[LiveWS] Browser gone — stopping reconnect loop")
                raise WebSocketDisconnect()

            session_count += 1
            print(f"[LiveWS] Opening Gemini session #{session_count} for {case_id[:8]}...")

            try:
                fresh_case = get_case(case_id) or case
                config = build_live_config(fresh_case)
                async with client.aio.live.connect(
                    model=GCP_MODEL,
                    config=config
                ) as session:

                    session_ref[0] = session
                    print(f"[LiveWS] Gemini Live opened: {case.get('diagnosis','?')} (session #{session_count})")

                    # Tell browser session is ready (on every reconnect so
                    # mic restarts and status bar updates correctly)
                    await websocket.send_text(json.dumps({
                        "type":      "session_ready",
                        "case_id":   case_id,
                        "diagnosis": case.get("diagnosis", ""),
                        "ibw_kg":    case.get("ibw_kg"),
                        "model":     GCP_MODEL,
                        "platform":  "Vertex AI"
                    }))

                    # Re-inject any pending speech that didn't
                    # get delivered before the previous session closed
                    if pending_speech_inject[0]:
                        pending = pending_speech_inject[0]
                        print(f"[LiveWS] 🔄 Re-injecting pending speech: {pending['text'][:60]}...")
                        await msg_queue.put(pending)
                        pending_speech_inject[0] = None

                    # ── Direct speech injection — bypasses msg_queue ──
                    async def inject_speech(text, is_qa=False):
                        """
                        Inject text directly into the CURRENT Gemini session.
                        Called from SCCM tasks instead of putting in msg_queue.
                        This survives session reconnects because it waits for
                        a live session instead of relying on the sender task.
                        """
                        max_wait = 10  # seconds to wait for a live session
                        waited = 0
                        while waited < max_wait:
                            s = session_ref[0]
                            if s is not None:
                                break
                            await asyncio.sleep(0.5)
                            waited += 0.5
                            print(f"[InjectSpeech] Waiting for session... {waited}s")

                        s = session_ref[0]
                        if s is None:
                            print("[InjectSpeech] ❌ No session after 10s — speech lost")
                            return

                        try:
                            last_injection_text[0] = text.lower().strip()

                            if is_qa:
                                await websocket.send_text(json.dumps({
                                    "type": "suppress_transcript"
                                }))
                            else:
                                await websocket.send_text(json.dumps({
                                    "type": "suppress_transcript"
                                }))

                            await asyncio.sleep(0.3)
                            await s.send(input=text, end_of_turn=True)
                            print(f"[InjectSpeech] ✅ Spoke: {text[:60]}...")
                            pending_speech_inject[0] = None

                        except Exception as e:
                            print(f"[InjectSpeech] ⚠️ send() failed: {e}, trying realtime...")
                            try:
                                await s.send_realtime_input(text=text)
                                print(f"[InjectSpeech] ✅ Fallback spoke: {text[:60]}...")
                                pending_speech_inject[0] = None
                            except Exception as e2:
                                print(f"[InjectSpeech] ❌ Both failed: {e2}")
                                pending_speech_inject[0] = {
                                    "type": "speech_inject",
                                    "text": text,
                                    "is_qa": is_qa
                                }

                    # ── Sender: msg_queue → Gemini ─────────
                    async def sender():
                        try:
                            while True:
                                msg = await msg_queue.get()
                                mt  = msg.get("type")

                                # Browser disconnected — stop everything
                                if mt == "__disconnect__":
                                    raise WebSocketDisconnect()

                                if mt == "audio_chunk":
                                    audio_bytes = base64.b64decode(msg["data"])
                                    await session.send_realtime_input(
                                        media=types.Blob(
                                            mime_type="audio/pcm;rate=16000",
                                            data=audio_bytes
                                        )
                                    )

                                elif mt == "interrupt_gemini":
                                    # Send silent audio to trigger barge-in
                                    # and stop Gemini's auto-response
                                    await session.send_realtime_input(
                                        media=types.Blob(
                                            mime_type="audio/pcm;rate=16000",
                                            data=bytes(3200)  # 100ms silence
                                        )
                                    )
                                    print("[LiveWS] ⛔ Gemini interrupted")

                                elif mt == "speech_inject":
                                    text  = msg.get("text", "").strip()
                                    is_qa = msg.get("is_qa", False)
                                    if text:
                                        print(f"[LiveWS] 🔊 Speaking (qa={is_qa}): {text[:60]}...")
                                        last_injection_text[0] = text.lower().strip()
                                        # Save in case session closes before delivery
                                        pending_speech_inject[0] = {
                                            "type":  "speech_inject",
                                            "text":  text,
                                            "is_qa": is_qa
                                        }
                                        # Always suppress voice transcript
                                        await websocket.send_text(json.dumps({
                                            "type": "suppress_transcript"
                                        }))
                                        # Wait for Gemini to settle after any
                                        # interrupt_gemini silence that was sent
                                        # before this message in the queue
                                        await asyncio.sleep(0.4)
                                        try:
                                            await session.send(
                                                input=text,
                                                end_of_turn=True
                                            )
                                            print(f"[LiveWS] ✅ Text injected via session.send()")
                                        except Exception as send_err:
                                            print(f"[LiveWS] ⚠️ session.send() failed: {send_err}")
                                            try:
                                                await session.send_realtime_input(text=text)
                                                print(f"[LiveWS] ✅ Fallback: send_realtime_input()")
                                            except Exception as fallback_err:
                                                print(f"[LiveWS] ❌ Both send methods failed: {fallback_err}")
                                                # pending_speech_inject stays set
                                                # → will be re-injected on reconnect
                                                continue
                                        # Only clear after confirmed delivery
                                        pending_speech_inject[0] = None

                                elif mt == "text_input":
                                    text = msg.get("text", "").strip().lstrip("⌨️ ").strip()
                                    if text:
                                        if len(text) < 3:
                                            await websocket.send_text(json.dumps({
                                                "type": "error",
                                                "message": "Input too short — please provide clinical details"
                                            }))
                                            continue
                                        if len(text) > 2000:
                                            await websocket.send_text(json.dumps({
                                                "type": "error",
                                                "message": "Input too long — please keep under 2000 characters"
                                            }))
                                            continue
                                        print(f"[LiveWS] Text: {text[:50]}...")
                                        # Cancel any in-flight SCCM task
                                        if (current_analysis_task[0] and
                                                not current_analysis_task[0].done()):
                                            current_analysis_task[0].cancel()
                                            print("[LiveWS] Previous analysis cancelled")
                                        fresh_case = get_case(case_id) or case
                                        current_analysis_task[0] = asyncio.create_task(
                                            run_sccm_then_speak(
                                                websocket.send_text,
                                                case_id,
                                                fresh_case,
                                                text,
                                                msg_queue,
                                                inject_fn=inject_speech
                                            )
                                        )

                                elif mt == "stop_audio":
                                    # Just cancel in-flight task — do NOT send
                                    # any signal to Gemini (that kills the session)
                                    if (current_analysis_task[0] and
                                            not current_analysis_task[0].done()):
                                        current_analysis_task[0].cancel()
                                        print("[LiveWS] Audio stopped by request")

                                elif mt == "ping":
                                    await websocket.send_text(
                                        json.dumps({"type": "pong"})
                                    )

                        except WebSocketDisconnect:
                            raise
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            print(f"[Sender] Error: {e}")
                            raise

                    # ── Receiver: Gemini → browser ─────────
                    async def receiver():
                        ai_text_buffer = ""
                        try:
                            async for response in session.receive():
                                if response is None:
                                    continue

                                sc = getattr(response, "server_content", None)

                                # Interrupted — doctor spoke over AI
                                if sc and getattr(sc, "interrupted", False):
                                    print("[LiveWS] Interrupted!")
                                    ai_text_buffer = ""
                                    last_injection_text[0] = ""
                                    pending_speech_inject[0] = None
                                    await websocket.send_text(json.dumps({
                                        "type": "interrupted"
                                    }))
                                    continue

                                # Audio output → browser
                                if response.data:
                                    audio_b64 = base64.b64encode(
                                        response.data
                                    ).decode("utf-8")
                                    await websocket.send_text(json.dumps({
                                        "type": "audio_chunk",
                                        "data": audio_b64
                                    }))

                                if sc:
                                    # AI speech transcript
                                    ot = getattr(sc, "output_transcription", None)
                                    if ot:
                                        t = getattr(ot, "text", "")
                                        if t:
                                            ai_text_buffer += t
                                            await websocket.send_text(json.dumps({
                                                "type": "transcript",
                                                "text": t,
                                                "role": "model"
                                            }))

                                    # Doctor speech transcript
                                    it = None
                                    for _field in ["input_transcription",
                                                   "input_audio_transcription"]:
                                        _obj = getattr(sc, _field, None)
                                        if _obj is not None and getattr(_obj, "text", ""):
                                            it = _obj
                                            break

                                    if it:
                                        t = getattr(it, "text", "").strip()
                                        if t:
                                            print(f"[LiveWS] 🎤 Doctor: '{t[:80]}'")
                                            doctor_transcript_buffer[0] += t + " "
                                            # Doctor is speaking — any pending
                                            # injection is now stale, discard it
                                            pending_speech_inject[0] = None
                                            await websocket.send_text(json.dumps({
                                                "type": "transcript",
                                                "text": t,
                                                "role": "user"
                                            }))

                                    # Turn complete
                                    if getattr(sc, "turn_complete", False):
                                        print(f"[LiveWS] ===== TURN COMPLETE =====")
                                        captured = doctor_transcript_buffer[0].strip()
                                        doctor_transcript_buffer[0] = ""
                                        turn_generation[0] += 1          # ← stamp this turn
                                        my_generation = turn_generation[0]
                                        ai_text_buffer = ""

                                        await websocket.send_text(json.dumps({
                                            "type": "turn_complete"
                                        }))

                                        if captured and len(captured) > 5:
                                            _is_noise = (
                                                len(captured.strip()) < 10 or
                                                all(c in " .,!?-_<>()" for c in captured.strip())
                                            )
                                            if _is_noise:
                                                print(f"[LiveWS] ⚠️ Noise-only transcript skipped: '{captured[:40]}'")
                                            elif _is_injection_readback(captured, last_injection_text[0]):
                                                print(f"[LiveWS] ⏭️ Injection readback detected — skipping: '{captured[:40]}'")
                                                last_injection_text[0] = ""
                                            else:
                                                print(f"[LiveWS] ✅ SCCM on: {captured[:80]}")
                                                last_injection_text[0] = ""
                                                fresh_case = get_case(case_id) or case
                                                asyncio.create_task(
                                                    run_sccm_analysis(
                                                        websocket.send_text,
                                                        case_id,
                                                        fresh_case,
                                                        captured,
                                                        msg_queue,
                                                        inject_fn=inject_speech
                                                    )
                                                )
                                        else:
                                            # Late transcription check
                                            async def _late_check(cid, c, ws_fn, sess, gen):
                                                await asyncio.sleep(0.6)
                                                # If generation changed, a new turn started — don't steal its buffer
                                                if turn_generation[0] != gen:
                                                    print(f"[LiveWS] ⚠️ Late check aborted — new turn already started")
                                                    return
                                                late = doctor_transcript_buffer[0].strip()
                                                if late and len(late) > 5:
                                                    print(f"[LiveWS] ✅ Late transcript: '{late[:80]}'")
                                                    fresh = get_case(cid) or c
                                                    await run_sccm_analysis(ws_fn, cid, fresh, late, msg_queue, inject_fn=inject_speech)
                                                    doctor_transcript_buffer[0] = ""
                                                else:
                                                    print("[LiveWS] ⚠️ No transcript after 600ms")
                                            asyncio.create_task(
                                                _late_check(case_id, case,
                                                            websocket.send_text, session, my_generation)
                                            )

                        except WebSocketDisconnect:
                            raise
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            err_str = str(e)
                            print(f"[Receiver] Error: {err_str[:120]}")
                            # 1000 = normal Gemini session end after a turn
                            # 1011 = keepalive timeout
                            # Both are clean exits — receiver returns, session
                            # loop will reconnect automatically
                            if ("1000" in err_str or
                                    "1011" in err_str or
                                    "operation was cancelled" in err_str.lower() or
                                    "keepalive" in err_str.lower()):
                                print("[Receiver] Clean Gemini close — will reconnect")
                                return   # exit receiver, triggers reconnect
                            traceback.print_exc()
                            raise

                    # ── Run sender + receiver for this session ──
                    sender_task   = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    # Wait for EITHER to finish.
                    # receiver finishes first on clean Gemini close (1000/1011).
                    # sender only finishes on browser disconnect.
                    done, pending = await asyncio.wait(
                        {sender_task, receiver_task},
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # Cancel the other task
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    # Check if browser disconnected
                    browser_disconnected = False
                    for t in done:
                        exc = t.exception() if not t.cancelled() else None
                        if isinstance(exc, WebSocketDisconnect):
                            browser_disconnected = True

                    if browser_disconnected:
                        print(f"[LiveWS] Browser disconnected — stopping")
                        raise WebSocketDisconnect()

                    # Gemini session ended cleanly — loop to reconnect
                    print(f"[LiveWS] Session #{session_count} ended — reconnecting in 0.5s")
                    await asyncio.sleep(0.5)

            except WebSocketDisconnect:
                raise   # Propagate to outer handler — stop the loop

            except asyncio.CancelledError:
                raise

            except Exception as e:
                print(f"[LiveWS] Session #{session_count} error: {e} — retrying in 1s")
                await asyncio.sleep(1.0)
                # Continue loop = reconnect

    except WebSocketDisconnect:
        print(f"[LiveWS] Disconnected: {case_id[:8]}...")
    except Exception as e:
        print(f"[LiveWS] Fatal error: {e}")
        traceback.print_exc()
        try:
            await websocket.send_text(json.dumps({
                "type": "error", "message": str(e)
            }))
        except Exception:
            pass
    finally:
        browser_task.cancel()
        await asyncio.gather(browser_task, return_exceptions=True)
        print(f"[LiveWS] Cleaning up: {case_id[:8]}...")
        try:
            await websocket.close()
        except Exception:
            pass
