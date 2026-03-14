
import json, os, asyncio, traceback, re as _re
from datetime import datetime, timezone
from google import genai
from google.genai import types
from case_memory import (
    get_trend, update_vent_settings, update_abg,
    update_hemodynamics, add_event, add_ai_assessment
)
from gemini_handler import process_text_input
from vent_reasoning import generate_sccm_recommendation

GCP_PROJECT  = os.environ.get("GCP_PROJECT",  "ventlive-ai")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
GCP_MODEL    = os.environ.get("GCP_MODEL",    "gemini-live-2.5-flash-native-audio")

LIVE_SYSTEM_PROMPT = """You are VentLive AI, an expert ICU ventilation assistant.
You speak ONLY from verified clinical data provided to you as text messages.
You NEVER generate clinical information from your own knowledge.

════════════════════════════════════════════════════
RULE 1 — WHEN THE DOCTOR SPEAKS OR TYPES:
════════════════════════════════════════════════════
Say ONLY the single word: "Analyzing."
Then stop completely.
Do NOT assess. Do NOT interpret. Do NOT comment.
Do NOT provide any values, recommendations, or opinions.
Do NOT repeat what the doctor said.
Wait silently for the verified data to arrive.

════════════════════════════════════════════════════
RULE 2 — WHEN YOU RECEIVE A TEXT MESSAGE:
════════════════════════════════════════════════════
That text is the VERIFIED clinical assessment from the
SCCM evidence-based engine. It is 100% accurate.

Read it EXACTLY word for word. Do NOT rephrase, reword,
summarize, or paraphrase ANY part of the text.
Every word in the text must be spoken exactly as written.

Speak in a confident, clear clinical voice.
Maintain a professional pace — not too fast, not too slow.

Do NOT add anything before or after the provided text.
Do NOT add your own clinical opinions.
Do NOT add filler words like "so", "well", "now", "okay".
Do NOT say "according to the data" or "the system says".
Do NOT skip any sentences or sections.
Read every word exactly as provided, from start to finish.

════════════════════════════════════════════════════
RULE 3 — ABSOLUTE PROHIBITIONS:
════════════════════════════════════════════════════
NEVER answer clinical questions from your own knowledge.
NEVER provide drug doses, ventilator settings, or clinical
recommendations that were not given to you in a text message.
NEVER say values like "P/F ratio is 200" unless that exact
value was provided to you in the current text message.
If no text message has arrived yet — say "Analyzing." and wait.

════════════════════════════════════════════════════
RULE 4 — LANGUAGE AND INTERRUPTION:
════════════════════════════════════════════════════
Always speak in English only.
If interrupted mid-sentence — stop immediately and listen.
Never resume a sentence after being interrupted.
"""


def build_live_config(case_context):
    # Build Vertex AI Live config with pre-calculated clinical values
    diagnosis = case_context.get("diagnosis", "Unknown")
    ibw_kg    = case_context.get("ibw_kg", "Not provided")
    vent_mode = case_context.get("vent_mode", "Unknown")

    last_vent_str = "None yet"
    last_vent = case_context.get("vent_settings_history", [])
    if last_vent:
        v = last_vent[-1]
        last_vent_str = (
            f"Mode={v.get('mode','?')} TV={v.get('tv','?')}mL "
            f"PEEP={v.get('peep','?')} FiO2={v.get('fio2','?')} RR={v.get('rr','?')}"
        )

    last_abg_str = "None yet"
    last_abg = case_context.get("abg_history", [])
    if last_abg:
        a = last_abg[-1]
        last_abg_str = (
            f"pH={a.get('ph','?')} PaCO2={a.get('paco2','?')} "
            f"PaO2={a.get('pao2','?')}"
        )

    # Pre-calculate driving pressure so Gemini has it before speaking
    dp_alert       = ""
    status_override = ""
    if last_vent:
        v     = last_vent[-1]
        pplat = v.get("pplat")
        peep  = v.get("peep")
        fio2  = v.get("fio2")
        tv    = v.get("tv")
        if pplat and peep:
            dp = round(float(pplat) - float(peep), 1)
            if dp >= 20:
                dp_alert = (
                    f"CALCULATED Driving Pressure = {dp} cmH2O - DANGEROUS. "
                    f"You MUST say CRITICAL ALERT and tell doctor to reduce TV immediately."
                )
                status_override = "CRITICAL ALERT"
            elif dp >= 15:
                dp_alert = (
                    f"CALCULATED Driving Pressure = {dp} cmH2O - ELEVATED above 15 cmH2O limit. "
                    f"You MUST say Worsening and recommend TV reduction now."
                )
                status_override = "Worsening"
            elif dp >= 13:
                dp_alert = (
                    f"CALCULATED Driving Pressure = {dp} cmH2O - within target below 15 cmH2O. "
                    f"Note it is approaching the 15 cmH2O limit — monitor after every vent change."
                )
                # dp 13-14 — within target, monitoring note only
            else:
                dp_alert = f"CALCULATED Driving Pressure = {dp} cmH2O - within target below 15 cmH2O."

        if fio2 and float(fio2) >= 0.65 and not status_override:
            status_override = "Worsening"
            dp_alert += f" FiO2 = {fio2} - elevated oxygen requirement."

        if tv and ibw_kg and ibw_kg != "Not provided":
            try:
                tv_pkg = round(float(tv) / float(ibw_kg), 1)
                if tv_pkg > 8:
                    status_override = "Worsening"
                    dp_alert += f" TV = {tv_pkg} mL/kg IBW - exceeds 8 mL/kg maximum."
            except Exception:
                pass

    status_instruction = (
        f"PRE-CALCULATED STATUS: You MUST start your response with {status_override}. "
        f"{dp_alert} "
        f"Do NOT say Stable. The numbers require you to say {status_override}."
        if status_override else
        f"PRE-CALCULATED STATUS: Settings appear within targets. "
        f"{dp_alert} You may say Stable if no other concerns."
    )

    patient_prompt = (
        f"{LIVE_SYSTEM_PROMPT}\n\n"
        f"=== CURRENT PATIENT ===\n"
        f"Diagnosis: {diagnosis}\n"
        f"IBW: {ibw_kg} kg\n"
        f"Vent Mode: {vent_mode}\n"
        f"Last settings: {last_vent_str}\n"
        f"Last ABG: {last_abg_str}\n"
        f"{status_instruction}\n"
    )

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Puck"
                )
            ),
            language_code="en-US"
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ALL_INPUT,
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=500,
                silence_duration_ms=2000,
            )
        ),
        system_instruction=types.Content(
            parts=[types.Part(text=patient_prompt)]
        ),
    )


def get_live_client():
    # Get Vertex AI client for Gemini Live.
    return genai.Client(
        vertexai=True,
        project=GCP_PROJECT,
        location=GCP_LOCATION
    )

def _is_stale(record: dict, max_age_minutes: int = 120) -> bool:
    """Returns True if record is older than max_age_minutes."""
    ts = record.get("timestamp")
    if not ts:
        return False
    try:
        recorded = datetime.fromisoformat(ts)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - recorded).total_seconds() / 60
        return age > max_age_minutes
    except Exception:
        return False

def _is_stale_abg(last_abg: dict, last_vent: dict, last_status: str) -> tuple:
    """
    Returns (is_stale: bool, reason: str) based on clinical context.
    Thresholds:
      - After vent change:        30 min
      - Critical / Worsening:    120 min  (2h)
      - Stable:                  480 min  (8h)
      - No prior status:         no check
    """
    ts = last_abg.get("timestamp")
    if not ts:
        return False, ""   # legacy record — no check

    try:
        recorded = datetime.fromisoformat(ts)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        abg_age_min = (datetime.now(timezone.utc) - recorded).total_seconds() / 60

        # Context 1 — vent was changed MORE RECENTLY than last ABG
        vent_ts = last_vent.get("timestamp") if last_vent else None
        if vent_ts:
            vent_recorded = datetime.fromisoformat(vent_ts)
            if vent_recorded.tzinfo is None:
                vent_recorded = vent_recorded.replace(tzinfo=timezone.utc)
            vent_age_min = (datetime.now(timezone.utc) - vent_recorded).total_seconds() / 60
            if vent_age_min < abg_age_min:
                # Vent was changed after last ABG — need new gas in 30 min
                if abg_age_min > 30:
                    return True, (
                        f"ABG is {int(abg_age_min)}min old — "
                        f"ventilator was adjusted after last gas. "
                        f"Repeat ABG within 30 min of vent changes (SCCM)."
                    )

        # Context 2 — patient stability
        if not last_status:
            return False, ""   # first assessment, no check

        if last_status in ["Critical", "Worsening"]:
            if abg_age_min > 120:
                return True, (
                    f"ABG is {int(abg_age_min)}min old — "
                    f"unstable patient requires ABG every 2–4h (SCCM)."
                )
        elif last_status == "Stable":
            if abg_age_min > 480:
                return True, (
                    f"ABG is {int(abg_age_min // 60)}h old — "
                    f"repeat ABG indicated (stable patients: every 6–12h, SCCM)."
                )

        return False, ""

    except Exception:
        return False, ""

def _build_qa_injection(question: str, last_rec: dict) -> str:
    """
    Builds a precise injection text for Gemini to speak as answer
    to a doctor's question. Source is EXCLUSIVELY the latest SCCM card.

    Structure:
      1. Ventilation status
      2. Most important next step
      3. Brief comprehensive context from card data

    If data needed to answer is missing → precise "not calculable,
    provide X" message.
    """
    q = question.lower().strip()

    # ── Extract all available card fields ──────────────────
    status      = last_rec.get("ventilation_status", "")
    physio      = last_rec.get("physiologic_interpretation", "")
    next_steps  = last_rec.get("immediate_next_step", "")
    monitoring  = last_rec.get("monitoring_and_safety", "")
    escalation  = last_rec.get("escalation_criteria", "")
    dp          = last_rec.get("driving_pressure", "")
    pf          = last_rec.get("pf_ratio")
    tv_pkg      = last_rec.get("tv_per_kg_ibw")
    rsbi        = last_rec.get("rsbi")
    map_val     = last_rec.get("map")
    trend       = last_rec.get("trend_summary", "")

    # First next step only (most urgent)
    first_step = next_steps.split(" | ")[0].replace("💡","").strip()                  if next_steps else ""

    # Status opener — natural clinical voice
    status_phrase = {
        "Critical":  "This patient is CRITICAL.",
        "Worsening": "This patient is worsening.",
        "Stable":    "This patient is currently stable.",
    }.get(status, f"Ventilation status is {status}." if status else "")

    # ── Specific question detection ─────────────────────────

    # DRIVING PRESSURE
    if any(k in q for k in ["driving pressure", "dp", "delta p", "driving"]):
        if not dp or "not calculable" in dp.lower():
            # Determine what is missing
            physio_lower = physio.lower()
            if "pplat" not in physio_lower and "plateau" not in physio_lower:
                missing = "plateau pressure"
            elif "peep" not in physio_lower:
                missing = "PEEP"
            else:
                missing = "plateau pressure and PEEP"
            return (
                f"{status_phrase} "
                f"Driving pressure is not calculable — "
                f"please provide {missing}."
            )
        dp_val  = dp.split(" ")[0]
        dp_desc = (
            "dangerously elevated — reduce tidal volume immediately"
            if "DANGEROUS" in dp else
            "elevated above the 15 centimetre limit — reduce tidal volume now"
            if "ELEVATED" in dp else
            "within the safe target below 15 centimetres"
        )
        return (
            f"{status_phrase} "
            f"The driving pressure is {dp_val} centimetres of water, {dp_desc}. "
            f"{first_step}."
        )

    # P/F RATIO
    if any(k in q for k in ["p/f", "pf ratio", "p f ratio",
                              "oxygenation", "pao2", "fio2 ratio"]):
        if not pf:
            return (
                f"{status_phrase} "
                f"P/F ratio is not calculable — "
                f"please provide PaO2 from an arterial blood gas "
                f"and the current FiO2."
            )
        pf_num = float(pf)
        pf_desc = (
            f"{int(pf_num)}, confirming severe ARDS"
            if pf_num < 100 else
            f"{int(pf_num)}, indicating moderate hypoxemia"
            if pf_num < 200 else
            f"{int(pf_num)}, indicating mild hypoxemia"
            if pf_num <= 300 else
            f"{int(pf_num)}, within acceptable range"
        )
        prone_note = ""
        if pf_num <= 150:
            prone_note = " Prone positioning is indicated at this P/F ratio."
        return (
            f"{status_phrase} "
            f"The P/F ratio is {pf_desc}.{prone_note} "
            f"{first_step}."
        )

    # TIDAL VOLUME
    if any(k in q for k in ["tidal volume", "tv", "tidal", "ml/kg",
                              "mL/kg", "ibw", "lung protective"]):
        if not tv_pkg:
            return (
                f"{status_phrase} "
                f"Tidal volume per kilogram IBW is not calculable — "
                f"please provide the tidal volume in millilitres "
                f"and the patient's height and sex for IBW calculation."
            )
        tv_num = float(tv_pkg.split(" ")[0])
        tv_desc = (
            "dangerously high — exceeds the 8 mL per kilogram absolute maximum"
            if tv_num > 8 else
            "above the 6 mL per kilogram target — consider reducing"
            if tv_num > 6 else
            "within the lung protective target of 6 mL per kilogram"
        )
        return (
            f"{status_phrase} "
            f"The tidal volume is {tv_pkg}, which is {tv_desc}. "
            f"{first_step}."
        )

    # RSBI
    if any(k in q for k in ["rsbi", "rapid shallow", "breathing index",
                              "weaning index"]):
        if not rsbi:
            return (
                f"{status_phrase} "
                f"RSBI is not calculable — "
                f"please provide the respiratory rate "
                f"and tidal volume."
            )
        rsbi_num = float(rsbi)
        rsbi_desc = (
            "predicts SBT failure — above 105"
            if rsbi_num >= 105 else
            "borderline — between 80 and 105, proceed cautiously"
            if rsbi_num >= 80 else
            "favourable — below 80, supports weaning readiness"
        )
        return (
            f"{status_phrase} "
            f"The RSBI is {rsbi}, which is {rsbi_desc}. "
            f"{first_step}."
        )

    # MAP / HEMODYNAMICS
    if any(k in q for k in ["map", "mean arterial", "blood pressure",
                              "hemodynamic", "haemodynamic", "pressure"]):
        if not map_val:
            return (
                f"{status_phrase} "
                f"MAP is not calculable — "
                f"please provide the blood pressure "
                f"as systolic over diastolic."
            )
        map_num = float(map_val)
        map_desc = (
            "critically low — below 65 millimetres of mercury"
            if map_num < 65 else
            "borderline — between 65 and 70"
            if map_num < 70 else
            "adequate — above 70 millimetres of mercury"
        )
        return (
            f"{status_phrase} "
            f"The mean arterial pressure is {map_val} millimetres of mercury, {map_desc}. "
            f"{first_step}."
        )

    # PRONING
    if any(k in q for k in ["prone", "proning", "flip", "position"]):
        if not pf:
            return (
                f"{status_phrase} "
                f"Prone positioning eligibility cannot be assessed — "
                f"P/F ratio is not calculable. "
                f"Please provide PaO2 and FiO2."
            )
        pf_num = float(pf)
        if pf_num <= 150:
            return (
                f"{status_phrase} "
                f"Prone positioning IS indicated. "
                f"The P/F ratio is {int(pf_num)}, which meets the threshold "
                f"of 150 or below. "
                f"Target at least 16 hours per day per the PROSEVA trial. "
                f"Initiate within 36 hours of ARDS diagnosis."
            )
        else:
            return (
                f"{status_phrase} "
                f"Prone positioning is NOT currently indicated. "
                f"The P/F ratio is {int(pf_num)}, which is above the 150 threshold. "
                f"Continue current lung protective ventilation."
            )

    # MONITORING
    if any(k in q for k in ["monitor", "monitoring", "watch", "check",
                              "follow", "track"]):
        if not monitoring:
            return (
                f"{status_phrase} "
                f"No monitoring plan available yet — "
                f"please provide clinical data first."
            )
        mon_first = monitoring.split(".")[0].strip()
        return (
            f"{status_phrase} "
            f"{first_step}. "
            f"For monitoring: {mon_first}."
        )

    # ESCALATION
    if any(k in q for k in ["escalat", "worsen", "deteriorat",
                              "when to call", "when should i"]):
        if not escalation:
            return (
                f"{status_phrase} "
                f"No escalation criteria available yet — "
                f"please provide clinical data first."
            )
        esc_first = escalation.split(".")[0].strip()
        return (
            f"{status_phrase} "
            f"Escalate if: {esc_first}. "
            f"Immediate action: {first_step}."
        )

    # STATUS
    if any(k in q for k in ["status", "how is", "how are",
                              "stable", "critical", "worsening"]):
        physio_first = physio.split(".")[0].strip() if physio else ""
        return (
            f"{status_phrase} "
            f"{physio_first}. "
            f"The most important action right now: {first_step}."
        )

    # NEXT STEP (most common question)
    if any(k in q for k in ["next step", "what should", "what do",
                              "what now", "action", "recommend",
                              "suggest", "advise", "do next"]):
        physio_first = physio.split(".")[0].strip() if physio else ""
        return (
            f"{status_phrase} "
            f"{physio_first}. "
            f"The immediate next step is: {first_step}."
        )

    # ── GENERAL / FALLBACK ──────────────────────────────────
    # Question not matched to a specific field —
    # give the full structured SCCM summary
    physio_first = physio.split(".")[0].strip() if physio else ""
    dp_part = ""
    if dp and "not calculable" not in dp.lower():
        dp_val = dp.split(" ")[0]
        dp_sev = (
            "dangerously elevated"
            if "DANGEROUS" in dp else
            "elevated"
            if "ELEVATED" in dp else
            "within target"
        )
        dp_part = f" Driving pressure is {dp_val} centimetres of water, {dp_sev}."

    pf_part = ""
    if pf:
        pf_num = float(pf)
        pf_part = (
            f" P/F ratio is {int(pf_num)}, indicating "
            f"{'severe' if pf_num < 100 else 'moderate' if pf_num < 200 else 'mild'} "
            f"hypoxemia."
        )

    return (
        f"{status_phrase} "
        f"{physio_first}.{dp_part}{pf_part} "
        f"The most important action right now: {first_step}."
    )

async def _run_clinical_pipeline(case_id, case, doctor_input):
    """
    Shared clinical pipeline — runs for ALL three assessment paths.
    Handles steps 1-13: clean → extract → carry-forward → save → reason → store.
    Returns: (narrative, safety_flags, extracted, rec, trend) or raises on failure.
    """
    import re as _re2

    # Step 1 — Clean Arabic/Unicode artifacts from input
    # Map Arabic phonetic medical terms to English FIRST,
    # before stripping — mirrors frontend cleanTranscript()
    _arabic_map = [
        # ICU parameters
        ("بي اتش",              "pH"),
        ("بي اي سي او 2",       "PaCO2"),
        ("بي اي سي او2",        "PaCO2"),
        ("باك او 2",            "PaCO2"),
        ("بي اي او 2",          "PaO2"),
        ("بي اي او2",           "PaO2"),
        ("اف اي او2",           "FiO2"),
        ("اف اي أو 2",          "FiO2"),
        ("بيبي",                "PEEP"),
        ("بيب",                 "PEEP"),
        ("بلاتوه",              "plateau"),
        ("بلاتو",               "plateau"),

        # Ventilator modes
        ("سي بي اي بي",         "CPAP"),
        ("بي اي بي",            "BiPAP"),
        ("بي اس في",            "PSV"),
        ("اي سي",               "AC"),
        ("في سي",               "VC"),

        # Measurements
        ("الفوليوم",            "volume"),
        ("الحجم",               "volume"),
        ("مللتر",               "mL"),
        ("مل",                  "mL"),
        ("ضغط",                 "pressure"),
        ("معدل",                "rate"),
        ("سرعة",                "rate"),

        # Tidal volume
        ("تايدل فوليوم",        "tidal volume"),
        ("تايدال فوليوم",       "tidal volume"),
        ("الحجم المدي",         "tidal volume"),
        ("تي في",               "TV"),
        ("تيدال",               "tidal volume"),

        # PEEP variants
        ("بيبت",                "PEEP"),
        ("بيبت اس",             "PEEP"),
        ("البيب",               "PEEP"),

        # FiO2 variants
        ("اف اي او 2",          "FiO2"),
        ("اف اي",               "FiO2"),
        ("الاكسجين",            "FiO2"),
        ("نسبة الاكسجين",       "FiO2"),

        # RR variants
        ("معدل التنفس",         "respiratory rate"),
        ("سرعة التنفس",         "respiratory rate"),
        ("التنفس",              "respiratory rate"),
        ("ار ار",               "RR"),

        # Plateau pressure
        ("ضغط البلاتو",         "plateau pressure"),
        ("البلاتو",             "plateau"),
        ("بلاتيو",              "plateau"),

        # Mode variants
        ("ايه سي",              "AC"),
        ("ايه سي في سي",        "AC/VC"),
        ("في سي ايه سي",        "AC/VC"),
        ("سيمف",                "SIMV"),
        ("بريشر سابورت",        "pressure support"),

        # Numbers — teens and hundreds
        ("عشرين",               "20"),
        ("ثلاثين",              "30"),
        ("اربعين",              "40"),
        ("خمسين",               "50"),
        ("ستين",                "60"),
        ("سبعين",               "70"),
        ("ثمانين",              "80"),
        ("تسعين",               "90"),
        ("مية",                 "100"),
        ("مئة",                 "100"),
        ("مئتين",               "200"),
        ("تلاتمية",             "300"),
        ("اربعمية",             "400"),
        ("خمسمية",              "500"),
        ("ستمية",               "600"),
        ("سبعمية",              "700"),
        ("تمانمية",             "800"),

        # Decimals
        ("صفر فاصلة",           "0."),
        ("فاصلة",               "."),
        ("نقطة",                "."),

        # Noise tags
        ("<noise>",             ""),
        ("<noise/>",            ""),
        ("<صوت>",               ""),
        ("[noise]",             ""),
        ("[inaudible]",         ""),

        # Arabic number words
        ("صفر",                 "0"),
        ("واحد",                "1"),
        ("اثنين",               "2"),
        ("اتنين",               "2"),
        ("ثلاثة",               "3"),
        ("تلاتة",               "3"),
        ("اربعة",               "4"),
        ("اربعه",               "4"),
        ("خمسة",                "5"),
        ("خمسه",                "5"),
        ("ستة",                 "6"),
        ("سته",                 "6"),
        ("سبعة",                "7"),
        ("سبعه",                "7"),
        ("ثمانية",              "8"),
        ("تمانية",              "8"),
        ("تسعة",                "9"),
        ("تسعه",                "9"),
        ("عشرة",                "10"),
        ("عشره",                "10"),
    ]
    cleaned_input = doctor_input
    for arabic, english in _arabic_map:
        cleaned_input = cleaned_input.replace(arabic, english)
    # Latin-script transliterations from Gemini speech recognition
    _latin_map = [
        (r'Batient',            'Patient'),
        (r'patiant',            'patient'),
        (r'peap',               'PEEP'),
        (r'beep',               'PEEP'),
        (r'Beep',               'PEEP'),
        (r'peeb',               'PEEP'),
        (r'peepee',             'PEEP'),
        (r'pip',                'PEEP'),
        (r'fio\s*2',            'FiO2'),
        (r'fi\s*o\s*2',         'FiO2'),
        (r'paco\s*2',           'PaCO2'),
        (r'pao\s*2',            'PaO2'),
        (r'plato',              'plateau'),
        (r'plteau',             'plateau'),
        (r'plateu',             'plateau'),
        (r'tidle',              'tidal'),
        (r'tidal\s*vol',        'tidal volume'),
        (r'TV\s+is',            'TV'),
        (r'ac\s*vc',            'AC/VC'),
        (r'acvc',               'AC/VC'),
        (r'a\s*[,\.]\s*c\s*[,\.]\s*b\s*[,\.]\s*c', 'AC/VC'),
        (r'a\s*c\s*b\s*c',      'AC/VC'),
        (r'breath\w*\s+on',     'patient on'),
        (r'respiratory\s*rt',   'respiratory rate'),
        (r'RR\s+is',            'RR'),
    ]
    for pattern, replacement in _latin_map:
        cleaned_input = _re.sub(pattern, replacement, cleaned_input, flags=_re.IGNORECASE)
    cleaned_input = _re.sub(r'از', 'is', cleaned_input)
    cleaned_input = _re.sub(r"[؀-ۿݐ-ݿ]", " ", cleaned_input)
    cleaned_input = _re.sub(r'\s+', ' ', cleaned_input).strip()
    print(f"[Pipeline] Input cleaned: {cleaned_input[:80]}")


    # Step 2 — AI extraction via Gemini 2.5 Flash
    narrative, safety_flags, extracted = await process_text_input(
        case, cleaned_input
    )

    # Refresh case from Firestore before carry-forward and staleness checks
    from case_memory import get_case
    fresh_case = get_case(case_id) or case

# Step 2b — Smart routing: question vs new data
    from gemini_handler import classify_input, has_new_clinical_data
    # Get last known extracted values for context bleed detection
    _last_assessments = case.get("ai_assessments", [])
    _last_extracted   = _last_assessments[-1].get("extracted_data", {}) if _last_assessments else {}

    input_type = classify_input(cleaned_input, extracted, _last_extracted)
    print(f"[Router] input='{cleaned_input[:60]}' type={input_type} extracted_keys={[k for k in extracted if not k.startswith('_')]}")

    if input_type == "question":
        # ── Get the latest real SCCM card ──────────────────
        last_assessments = fresh_case.get("ai_assessments", [])
        last_rec = {}
        for _a in reversed(last_assessments):
            if _a.get("sccm_recommendation"):
                last_rec = _a["sccm_recommendation"]
                break

        # ── No SCCM card yet ───────────────────────────────
        if not last_rec:
            injection_text = (
                "No clinical data available yet. "
                "Please provide ventilator settings first, "
                "such as tidal volume, PEEP, FiO2, respiratory rate, "
                "and plateau pressure."
            )
            add_ai_assessment(case_id, {
                "input":               doctor_input,
                "source":              "question_answer",
                "gemini_narrative":    injection_text,
                "verbal_script":       injection_text,
                "safety_flags":        "",
                "extracted_data":      {},
                "sccm_recommendation": None,
                "trend":               {}
            })
            add_event(case_id, f"[Q] {doctor_input[:150]}")
            return narrative, safety_flags, extracted, None, {}, injection_text

        # ── Build injection from SCCM card ─────────────────
        injection_text = _build_qa_injection(doctor_input, last_rec)

        # Save to Firestore — verbal_script is what Gemini will speak
        add_ai_assessment(case_id, {
            "input":               doctor_input,
            "source":              "question_answer",
            "gemini_narrative":    injection_text,
            "verbal_script":       injection_text,
            "safety_flags":        "",
            "extracted_data":      {},
            "sccm_recommendation": None,
            "trend":               {}
        })
        add_event(case_id, f"[Q] {doctor_input[:150]}")
        return narrative, safety_flags, extracted, None, {}, injection_text

    # Step 3 — Inject stored baseline PaCO2 (COPD patients)
    baseline_paco2 = fresh_case.get("baseline_paco2")
    if baseline_paco2:
        extracted["baseline_paco2"] = baseline_paco2


    # Step 4 — Carry-forward missing vent settings from last known state
    last_vent = fresh_case.get("vent_settings_history", [])
    if last_vent:
        prev = last_vent[-1]
        vent_stale = _is_stale(prev, max_age_minutes=240)  # 4h — vents change slowly
        for key in ["tv", "fio2", "peep", "pplat", "rr", "mode"]:
            if key not in extracted and prev.get(key):
                if not vent_stale:
                    extracted[key] = prev[key]
                else:
                    print(f"[Pipeline] Stale vent carry-forward skipped: {key} (>4h old)")
        if vent_stale:
            extracted["_stale_vent_warning"] = True

    # Step 5 — Carry-forward missing ABG values from last known state
    # SpO2 desaturation guard: if doctor mentions a drop, do NOT
    # carry forward the old (higher) SpO2 — it would mask deterioration
    last_assessments = fresh_case.get("ai_assessments", [])
    last_status = ""
    if last_assessments:
        last_rec = None
        for _a in reversed(last_assessments):
            if _a.get("sccm_recommendation"):
                last_rec = _a["sccm_recommendation"]
                break
        last_rec = last_rec or {}
        last_status = last_rec.get("ventilation_status", "")

    last_abg = fresh_case.get("abg_history", [])
    if last_abg:
        prev = last_abg[-1]
        last_vent_rec = last_vent[-1] if last_vent else {}
        abg_stale, stale_reason = _is_stale_abg(prev, last_vent_rec, last_status)
        abg_stale, stale_reason = _is_stale_abg(prev, last_vent_rec, last_status)
        for key in ["ph", "paco2", "pao2", "spo2"]:
            if key not in extracted and prev.get(key):
                if key == "spo2":
                    if any(p in doctor_input.lower() for p in [
                        "spo2 dropped", "desaturated", "desaturation",
                        "spo2 drop", "oxygen dropped", "saturation dropped"
                    ]):
                        continue
                if not abg_stale:
                    extracted[key] = prev[key]
                else:
                    print(f"[Pipeline] Stale ABG skipped: {key} — {stale_reason}")
        if abg_stale:
            extracted["_stale_abg_warning"] = stale_reason

    # Step 6 — Save vent settings to case memory
    vent = {k: extracted[k] for k in
            ["mode", "tv", "peep", "fio2", "rr", "pplat", "ppeak"]
            if k in extracted}
    if vent:
        update_vent_settings(case_id, vent)

    # Step 7 — Save ABG to case memory
    abg = {k: extracted[k] for k in
           ["ph", "paco2", "pao2", "hco3", "spo2"]
           if k in extracted}
    if abg:
        update_abg(case_id, abg)

    # Step 8 — Save hemodynamics to case memory
    hemo = {k: extracted[k] for k in ["bp", "hr", "map"] if k in extracted}
    if hemo:
        update_hemodynamics(case_id, hemo)

    # Step 9 — Calculate trends from history
    trend = get_trend(case_id)

    # Step 10 — Inject prior PaCO2 for COPD rapid-correction delta check
    abg_hist = case.get("abg_history", [])
    if len(abg_hist) >= 2 and abg_hist[-2].get("paco2"):
        extracted["prior_paco2"] = abg_hist[-2]["paco2"]

    # Step 11 — Run SCCM clinical reasoning engine
    extracted["_raw_input"] = doctor_input
    rec = generate_sccm_recommendation(
        diagnosis=case.get("diagnosis", ""),
        data=extracted,
        ibw_kg=case.get("ibw_kg"),
        trend=trend
    )

    # Step 12 — Log the event
    add_event(case_id, f"[Live] {doctor_input[:150]}")

    # Step 13 — Save full AI assessment to case memory
    add_ai_assessment(case_id, {
        "input":               doctor_input,
        "source":              "gemini_live",
        "gemini_narrative":    narrative,
        "safety_flags":        safety_flags,
        "extracted_data":      extracted,
        "sccm_recommendation": rec,
        "trend":               trend,
        "verbal_script":       _build_verbal_script(rec, doctor_input)
    })

    return narrative, safety_flags, extracted, rec, trend

def _build_verbal_script(rec, doctor_input=""):
    """
    Shared verbal script builder — converts SCCM recommendation dict
    into a natural spoken assessment for Gemini Live voice injection.
    Single source of truth for all voice output across all live paths.
    """
    status   = rec.get("ventilation_status", "")
    dp       = rec.get("driving_pressure", "")
    physio   = rec.get("physiologic_interpretation", "")
    nextstep = rec.get("immediate_next_step", "").split(" | ")[0][:120]
    monitor  = rec.get("monitoring_and_safety", "").split(".")[0]
    escalate = rec.get("escalation_criteria", "").split(".")[0]
    pf       = rec.get("pf_ratio", "")
    tv_pkg   = rec.get("tv_per_kg_ibw", "")

    status_opener = {
        "Critical":  "CRITICAL ALERT.",
        "Worsening": "Worsening.",
        "Stable":    "Stable."
    }.get(status, status + ".")

    verbal_parts = [status_opener]

    # Physiology — first sentence only, using re.split for clean sentence boundary
    if physio:
        phys_first = _re.split(r'(?<=[a-zA-Z])\. ', physio)[0].strip()
        if len(phys_first) > 10:
            verbal_parts.append(phys_first + ".")

    # P/F ratio alert
    if pf:
        try:
            pf_val = float(pf)
            if pf_val < 100:
                verbal_parts.append(
                    f"The P to F ratio is {int(pf_val)}, confirming severe ARDS."
                )
            elif pf_val < 200:
                verbal_parts.append(
                    f"The P to F ratio is {int(pf_val)}, indicating moderate hypoxemia."
                )
        except ValueError:
            pass

    # Driving pressure — tiered response
    if dp and "Not calculable" not in dp:
        dp_val = dp.split(" ")[0]
        try:
            dp_num = float(dp_val)
            if dp_num >= 20:
                verbal_parts.append(
                    f"The driving pressure is {dp_val} centimetres of water — "
                    f"dangerously elevated. Reduce tidal volume immediately."
                )
            elif dp_num >= 15:
                verbal_parts.append(
                    f"The driving pressure is {dp_val} centimetres of water, "
                    f"elevated above the 15 centimetre limit. "
                    f"Reduce tidal volume now."
                )
            elif dp_num >= 13:
                verbal_parts.append(
                    f"The driving pressure is {dp_val} centimetres of water — "
                    f"within target below 15 centimetres. "
                    f"Monitor after every ventilator change as it is approaching the limit."
                )
            else:
                verbal_parts.append(
                    f"The driving pressure is {dp_val} centimetres of water, "
                    f"within the safe target below 15."
                )
        except ValueError:
            pass

    # TV per kg alert
    if tv_pkg:
        try:
            tv_num = float(tv_pkg.split(" ")[0])
            if tv_num > 8:
                verbal_parts.append(
                    f"Tidal volume is {tv_num} mL per kg IBW, "
                    f"exceeding the 8 mL per kg maximum."
                )
        except ValueError:
            pass

    # Immediate action — strip urgent prefixes for natural speech
    if nextstep:
        clean_step = _re.sub(
            r"^(URGENT|CRITICAL|IMMEDIATE|ABSOLUTE LIMIT|DANGER)\s*[:\-]\s*",
            "", nextstep, flags=_re.IGNORECASE
        ).strip()
        if len(clean_step) > 10:
            verbal_parts.append(f"Immediate action: {clean_step}.")

    # Monitoring — first sentence only
    if monitor and len(monitor) > 10:
        verbal_parts.append(f"After the change, {monitor.lower().strip()}.")

    # Escalation — only for serious status
    if status in ["Critical", "Worsening"] and escalate and len(escalate) > 10:
        esc_first = escalate.split("->")[0].strip()
        if len(esc_first) > 10:
            verbal_parts.append(
                f"Escalate if {esc_first.lower().rstrip('.')}."
            )

    return " ".join(verbal_parts)

async def run_sccm_then_speak(ws_send_fn, case_id, case, doctor_input, msg_queue, inject_fn=None):
    """
    Primary live pipeline: SCCM runs first, then Gemini speaks the result.
    Triggered by: WebSocket text_input message.
    Voice: includes doctor input context in Gemini message.
    """

    try:
        print(f"[SCCM-First] Starting for: {doctor_input[:60]}")

        # Steps 1-13: shared clinical pipeline
        result = await _run_clinical_pipeline(case_id, case, doctor_input)
        if len(result) == 6:
            # Question path
            narrative, safety_flags, extracted, rec, trend, injection_text = result
            await ws_send_fn(json.dumps({
                "type":          "question_answer",
                "injection_text": injection_text,
            }))
            verbal_script = injection_text
            # Q&A: suppress Gemini readback — bubble already shown
            is_qa = True
        else:
            narrative, safety_flags, extracted, rec, trend = result
            verbal_script = _build_verbal_script(rec, doctor_input)
            await ws_send_fn(json.dumps({
                "type":                "assessment",
                "gemini_narrative":    narrative,
                "safety_flags":        safety_flags,
                "sccm_recommendation": rec,
                "extracted_data":      extracted,
                "trend":               trend,
                "verbal_script":       verbal_script
            }))
            is_qa = False

        # Step 16: Send to Gemini Live
        if inject_fn is not None:
            if is_qa:
                print(f"[SCCM-First] Q&A voice: {verbal_script[:100]}...")
            else:
                print(f"[SCCM-First] Injecting: {rec.get('ventilation_status','?')}")
                print(f"[SCCM-First] Script: {verbal_script[:100]}...")
            await inject_fn(verbal_script, is_qa)
        elif msg_queue is not None:
            print(f"[SCCM-First] Fallback via queue: {verbal_script[:60]}...")
            await msg_queue.put({
                "type":    "speech_inject",
                "text":    verbal_script,
                "is_qa":   is_qa
            })

    except asyncio.CancelledError:
        print(f"[SCCM-First] Task cancelled cleanly")
    except Exception as e:
        print(f"[SCCM-First] Error: {e}")
        traceback.print_exc()
        try:
            await ws_send_fn(json.dumps({
                "type": "error",
                "message": f"Analysis failed: {str(e)}"
            }))
        except Exception:
            pass

async def run_sccm_analysis(ws_send_fn, case_id, case, doctor_input, msg_queue=None, inject_fn=None):
    """
    Voice turn pipeline: SCCM runs after voice turn_complete signal.
    Triggered by: Gemini Live voice turn completion.
    Voice: assessment only in Gemini message (no doctor input echo).
    """
    try:
        print(f"[SCCM] Analyzing: {doctor_input[:60]}...")

        # Steps 1-13: shared clinical pipeline
        result = await _run_clinical_pipeline(case_id, case, doctor_input)
        if len(result) == 6:
            # Question path
            narrative, safety_flags, extracted, rec, trend, injection_text = result
            await ws_send_fn(json.dumps({
                "type":          "question_answer",
                "injection_text": injection_text,
            }))
            verbal_script = injection_text
            is_qa = True
        else:
            narrative, safety_flags, extracted, rec, trend = result
            verbal_script = _build_verbal_script(rec)
            await ws_send_fn(json.dumps({
                "type":                "assessment",
                "gemini_narrative":    narrative,
                "safety_flags":        safety_flags,
                "sccm_recommendation": rec,
                "extracted_data":      extracted,
                "trend":               trend,
                "verbal_script":       verbal_script
            }))
            is_qa = False

        # Step 16: Send to Gemini Live
        if inject_fn is not None:
            if is_qa:
                print(f"[SCCM] Q&A voice: {verbal_script[:100]}...")
            else:
                print(f"[SCCM] Injecting: {rec.get('ventilation_status','?')} - {verbal_script[:100]}...")
            await inject_fn(verbal_script, is_qa)
        elif msg_queue is not None:
            print(f"[SCCM] Fallback via queue: {verbal_script[:60]}...")
            await msg_queue.put({
                "type":  "speech_inject",
                "text":  verbal_script,
                "is_qa": is_qa
            })
        else:
            print("[SCCM] No queue and no inject_fn — injection skipped")

        print(f"[SCCM] Done: {'Q&A' if len(result) == 6 else rec.get('ventilation_status','?')}")

        print(f"[SCCM] Done: {'Q&A' if len(result) == 6 else rec.get('ventilation_status','?')}")

    except asyncio.CancelledError:
        print(f"[SCCM] Task cancelled cleanly")
    except Exception as e:
        print(f"[SCCM] Error: {e}")
        traceback.print_exc()
        try:
            await ws_send_fn(json.dumps({
                "type": "error",
                "message": f"Analysis failed: {str(e)}"
            }))
        except Exception:
            pass

