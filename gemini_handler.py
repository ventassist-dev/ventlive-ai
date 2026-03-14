
import os, json, re
import asyncio
from google import genai
from google.genai import types
from google.genai.types import (
    LiveConnectConfig, SpeechConfig, VoiceConfig, PrebuiltVoiceConfig,
    AudioTranscriptionConfig, RealtimeInputConfig, AutomaticActivityDetection,
    TurnCoverage, StartSensitivity, EndSensitivity
)

_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_VERTEX_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east4")

# ── Startup validation ────────────────────────────────────
def _validate_credentials():
    errors = []
    if not _VERTEX_PROJECT:
        errors.append("GOOGLE_CLOUD_PROJECT is not set")
    if not _GEMINI_API_KEY and not _VERTEX_PROJECT:
        errors.append("Neither GEMINI_API_KEY nor GOOGLE_CLOUD_PROJECT is set — no credentials available")
    if errors:
        for e in errors:
            print(f"[gemini_handler] ❌ STARTUP ERROR: {e}")
        raise EnvironmentError(
            "VentLive AI cannot start — missing credentials:\n" +
            "\n".join(f"  • {e}" for e in errors)
        )
    print(f"[gemini_handler] ✅ Credentials validated — project={_VERTEX_PROJECT or 'API key mode'}")

_validate_credentials()

client = genai.Client(api_key=_GEMINI_API_KEY if _GEMINI_API_KEY else None)


SYSTEM_PROMPT = """You are VentLive AI, an expert ICU mechanical ventilation assistant.
All recommendations strictly follow SCCM Mechanical Ventilation Guidelines.

RESPOND IN THIS EXACT FORMAT:

CLINICAL NARRATIVE:
[2-4 sentences on what is happening physiologically]

SAFETY FLAGS:
[Immediate safety concerns, or write: None identified]

DATA EXTRACTION:
DATA EXTRACTION:
<data>
{
  "mode": null, "tv": null, "peep": null, "fio2": null,
  "rr": null, "pplat": null, "ppeak": null,
  "ph": null, "paco2": null, "pao2": null,
  "hco3": null, "spo2": null,
  "bp": null, "hr": null, "map": null, "auto_peep": null,
  "pf_ratio_stated": null, "insp_flow": null,
  "sbt_status": null, "gcs": null, "cough_strength": null,
  "vasopressor_dose": null, "post_extubation": null,
  "baseline_paco2": null, "prior_paco2": null
}
</data>

EXTRACTION RULES:
- tv: always in mL
- fio2: always as decimal (0.6 not 60%)
- bp: systolic/diastolic string e.g. 110/70
- Leave null if not mentioned
- In ventilator reports, numbers typically appear in this order:
  TV (mL) → PEEP (cmH2O) → FiO2 (decimal) → RR (/min) → Pplat (cmH2O)
  So if a doctor says "520 8 0.7 18 28" → tv=520, peep=8, fio2=0.7, rr=18, pplat=28
- pplat is ALWAYS the last pressure value mentioned in a vent settings report
- ppeak and pplat are different: pplat requires an inspiratory hold maneuver
- If only one pressure value is given at the end of a vent report, extract it as pplat
- pf_ratio_stated: extract ONLY if doctor states P/F ratio directly
  (e.g. "P/F 85", "P/F ratio is 120"). Do NOT calculate — leave null
  if only PaO2 and FiO2 are given separately.
- insp_flow: inspiratory flow rate in L/min. Extract from phrases like
  "flow 80", "inspiratory flow 60", "flow rate 80", "peak flow 80",
  "insp flow 60", "flow of 80 litres". Always in L/min — leave null if not stated.
- sbt_status: extract from phrases like "SBT passed", "passed the trial",
  "tolerated SBT", "SBT failed", "failed the trial", "SBT in progress",
  "doing the trial now", "not started SBT yet", "extubated".
  Values: "passing" / "failing" / "completed" / "not started" — leave null if not mentioned.
- gcs: Glasgow Coma Scale integer. Extract from "GCS 14", "GCS is 13",
  "Glasgow 12", "score of 14". Always integer 3–15 — leave null if not stated.
- cough_strength: extract from "strong cough", "good cough", "weak cough",
  "poor cough", "no cough", "cannot cough", "cough adequate".
  Values: "strong" / "weak" / "absent" — leave null if not mentioned.
- vasopressor_dose: norepinephrine equivalent in mcg/min (not mcg/kg/min).
  Extract from "norepi 8", "norepinephrine at 10", "vasopressor 5 mcg".
  Always numeric — leave null if not stated.
- post_extubation: true if patient is already extubated and off the ventilator.
  Extract from "just extubated", "extubated 1 hour ago", "off the vent",
  "breathing on their own now". Values: true / false — leave null if not mentioned.
- baseline_paco2: patient's known chronic baseline PaCO2 in mmHg (COPD patients).
  Extract from "baseline paco2 62", "baseline_paco2 62", "chronic co2 55",
  "their usual co2 is 65", "baseline co2 of 60". Always numeric — leave null if not stated.
- prior_paco2: most recent previous PaCO2 value before current reading, in mmHg.
  Extract from "prior paco2 82", "prior_paco2 82", "previous co2 was 80",
  "last co2 82", "co2 was 78 yesterday". Always numeric — leave null if not stated.
"""

# ── Clinical fields that indicate new data was provided ──
_CLINICAL_FIELDS = {
    "vent":  ["mode", "tv", "peep", "fio2", "rr", "pplat", "ppeak"],
    "abg":   ["ph", "paco2", "pao2", "hco3", "spo2"],
    "hemo":  ["bp", "hr", "map", "bp_sys", "bp_dia"],
    "other": ["auto_peep", "gcs", "insp_flow", "pf_ratio_stated"],
}
_ALL_CLINICAL = [f for group in _CLINICAL_FIELDS.values() for f in group]

def has_new_clinical_data(extracted: dict) -> bool:
    """
    Returns True if extracted dict contains at least one real clinical value.
    Ignores internal flags (_stale_*, _raw_input, baseline_paco2).
    """
    for field in _ALL_CLINICAL:
        val = extracted.get(field)
        if val is not None:
            return True
    return False

# Question patterns — inputs that are clearly questions not data
_QUESTION_PATTERNS = [
    r'\bwhat\s+is\b',
    r'\bwhat\'s\b',
    r'\bwhat are\b',
    r'\bshould\s+i\b',
    r'\bcan\s+i\b',
    r'\bdo\s+i\b',
    r'\bhow\s+(much|many|do|should|is|are)\b',
    r'\bwhy\s+is\b',
    r'\bwhen\s+(should|do|can)\b',
    r'\bis\s+(it|the|this|that)\b',
    r'\bare\s+we\b',
    r'\bexplain\b',
    r'\btell\s+me\b',
    r'\bgive\s+me\b',
    r'\bcalculate\b',
    r'\bcheck\b',
    r'\bshow\s+me\b',
    r'\bwhat\s+do\s+you\b',
]

def classify_input(text: str, extracted: dict, last_extracted: dict = None) -> str:
    """
    Returns:
      "data"     — new clinical values provided → run full pipeline
      "question" — question about existing data → answer from last assessment

    Uses three-layer detection:
    1. Numeric check — real clinical updates always contain numbers
    2. Question pattern match
    3. Context bleed check — if extracted values match last known, it's bleed not new data
    """
    t = text.lower().strip()

    # Layer 1 — no numbers in input = cannot be new clinical data
    import re as _re_local
    has_numbers = bool(_re_local.search(r'\d', text))
    if not has_numbers:
        return "question"

    # Layer 2 — question pattern match overrides even if numbers present
    # e.g. "should i increase PEEP to 10?" — has number but is a question
    for pattern in _QUESTION_PATTERNS:
        if re.search(pattern, t):
            return "question"

    # Layer 3 — context bleed check
    # If ALL extracted values match last known values exactly → context bleed
    if has_new_clinical_data(extracted) and last_extracted:
        new_fields = {k: extracted[k] for k in _ALL_CLINICAL if k in extracted}
        old_fields = {k: last_extracted.get(k) for k in new_fields}
        if new_fields == old_fields:
            return "question"  # identical values — came from context not new input

    if has_new_clinical_data(extracted):
        return "data"

    # Short inputs with no data are likely conversational
    if len(t.split()) <= 8:
        return "question"

    return "data"  # default — run pipeline if unsure



def extract_auto_peep_fallback(text, extracted):
    if extracted.get("auto_peep"):
        return extracted
    patterns = [
        r'auto[-\s]?peep\s+(?:measured\s+at\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'intrinsic\s+peep\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'ipeep\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 0 < val <= 25:
                extracted["auto_peep"] = val
            else:
                print(f"[Extract] auto_peep out of range ignored: {val} cmH2O")
            break
    return extracted

def extract_insp_flow_fallback(text, extracted):
    if extracted.get("insp_flow"):
        return extracted
    patterns = [
        r'inspir\w*\s+flow\s+(?:of\s+|=\s*|at\s+)?(\d+(?:\.\d+)?)',
        r'insp\s+flow\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'flow\s+rate\s+(?:of\s+|=\s*|at\s+)?(\d+(?:\.\d+)?)',
        r'peak\s+flow\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'\bflow\s+(?:of\s+|=\s*|at\s+)?(\d+(?:\.\d+)?)\s*(?:l/min|lpm|litres|liters)?',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 20 <= val <= 150:   # physiologic inspiratory flow range
                extracted["insp_flow"] = val
                print(f"[Extract] insp_flow fallback: {val} L/min")
                break
    return extracted

def extract_bp_fallback(text, extracted):
    if extracted.get("bp"):
        return extracted
    patterns = [
        r'bp\s+(?:is\s+)?(\d{2,3})[/\\](\d{2,3})',
        r'blood\s+pressure\s+(?:is\s+)?(\d{2,3})[/\\](\d{2,3})',
        r'(\d{2,3})\s+over\s+(\d{2,3})',
        r'bp\s+(?:is\s+)?(\d{2,3})\s+(\d{2,3})',      # "bp 85 60" without slash
        r'pressure\s+(\d{2,3})[/\\](\d{2,3})',
        r'(\d{2,3})[/\\](\d{2,3})\s+(?:mmhg|mm\s+hg)', # "85/60 mmHg"
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            sys_val = float(match.group(1))
            dia_val = float(match.group(2))
            # Sanity check — systolic 60-250, diastolic 30-150
            if 60 <= sys_val <= 250 and 30 <= dia_val <= 150:
                extracted["bp"] = f"{int(sys_val)}/{int(dia_val)}"
                print(f"[Extract] bp fallback: {extracted['bp']}")
                break
    return extracted

def extract_hr_fallback(text, extracted):
    if extracted.get("hr"):
        return extracted
    patterns = [
        r'hr\s+(?:is\s+|of\s+)?(\d{2,3})',
        r'heart\s+rate\s+(?:is\s+|of\s+)?(\d{2,3})',
        r'pulse\s+(?:is\s+|of\s+)?(\d{2,3})',
        r'(\d{2,3})\s+(?:bpm|beats)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 30 <= val <= 250:   # physiologic HR range
                extracted["hr"] = val
                print(f"[Extract] hr fallback: {val} bpm")
                break
    return extracted

def extract_pplat_fallback(text, extracted):
    if extracted.get("pplat"):
        return extracted

    # Strategy 1: explicit keyword match
    patterns = [
        r'p(?:lateau|lat)\s+(?:pressure\s+)?(?:of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
        r'plateau\s+(?:pressure\s+)?(\d+)',
        r'pplat\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'plat\s+(\d+)',
        r'(?:plateau|plat|pplat).*?(\d{2,3})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 10 <= val <= 50:
                extracted["pplat"] = val
                print(f"[Extract] pplat keyword match: {val} cmH2O")
                return extracted

    # Strategy 2: positional — last number in sentence
    # When we have TV + PEEP + FiO2 + RR already extracted,
    # the last number is very likely pplat
    has_tv   = extracted.get("tv")
    has_peep = extracted.get("peep")
    has_rr   = extracted.get("rr")

    if has_tv and has_peep and has_rr:
        # Find all numbers in the text
        all_numbers = re.findall(r'\b(\d{2,3})\b', text)
        if all_numbers:
            last_num = float(all_numbers[-1])
            # Pplat must be > PEEP and in physiologic range
            peep_val = float(has_peep)
            if peep_val < last_num <= 45:
                extracted["pplat"] = last_num
                print(f"[Extract] pplat positional fallback: {last_num} cmH2O")
                return extracted

    return extracted

def extract_peep_fallback(text, extracted):
    if extracted.get("peep"):
        return extracted
    patterns = [
        r'peep\s+(?:of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
        r'positive\s+end\s+expiratory\s+(?:pressure\s+)?(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 0 <= val <= 30:   # sanity check
                extracted["peep"] = val
                break
    return extracted

def extract_tv_fallback(text, extracted):
    if extracted.get("tv"):
        return extracted

    # Strategy 1: explicit keyword match
    patterns = [
        r'tidal\s+volume\s+(?:of\s+|=\s*|is\s+)?(\d+)\s*(?:ml|millilitre|mL)?',
        r'tv\s+(?:of\s+|=\s*)?(\d+)\s*(?:ml|mL)?',
        r'(\d{3,4})\s*(?:ml|mL)\s+tidal',
        r'volume\s+(?:of\s+|=\s*)?(\d{3,4})',
        r'(\d{3,4})\s*(?:ml|mL)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 200 <= val <= 900:   # physiologic TV range
                extracted["tv"] = val
                print(f"[Extract] tv fallback: {val} mL")
                return extracted

    # Strategy 2: positional — first 3-4 digit number is likely TV
    # when we have PEEP already (PEEP is always 2 digits)
    has_peep = extracted.get("peep")
    if has_peep:
        all_numbers = re.findall(r'\b(\d{3,4})\b', text)
        for num_str in all_numbers:
            val = float(num_str)
            if 200 <= val <= 900:
                extracted["tv"] = val
                print(f"[Extract] tv positional fallback: {val} mL")
                return extracted

    return extracted

def extract_fio2_fallback(text, extracted):
    if extracted.get("fio2"):
        return extracted
    patterns = [
        r'fi\s*o\s*2\s+(?:of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
        r'fio2\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s+fi\s*o\s*2',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if val > 1:
                val = val / 100   # convert 70 → 0.70
            if 0.21 <= val <= 1.0:   # sanity check
                extracted["fio2"] = val
                break
    return extracted

def extract_ph_fallback(text, extracted):
    if extracted.get("ph"):
        return extracted
    patterns = [
        r'\bph\s+(?:is\s+|of\s+|=\s*)?(\d\.\d{1,2})',
        r'\bph\s*[:=]\s*(\d\.\d{1,2})',
        r'(?:arterial\s+)?ph\s+(?:was\s+|measured\s+)?(\d\.\d{1,2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 6.80 <= val <= 7.80:   # physiologic pH range
                extracted["ph"] = val
                print(f"[Extract] ph fallback: {val}")
                break
    return extracted

def extract_paco2_fallback(text, extracted):
    if extracted.get("paco2"):
        return extracted
    patterns = [
        r'pa\s*co\s*2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'paco2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'co2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'pco2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'carbon\s+dioxide\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 15 <= val <= 150:   # physiologic PaCO2 range
                extracted["paco2"] = val
                print(f"[Extract] paco2 fallback: {val} mmHg")
                break
    return extracted

def extract_pao2_fallback(text, extracted):
    if extracted.get("pao2"):
        return extracted
    patterns = [
        r'pa\s*o\s*2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'pao2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'po2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'oxygen\s+(?:tension\s+)?(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)\s*mmhg',
        r'p\s*o2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 20 <= val <= 600:   # physiologic PaO2 range
                extracted["pao2"] = val
                print(f"[Extract] pao2 fallback: {val} mmHg")
                break
    return extracted

def extract_hco3_fallback(text, extracted):
    if extracted.get("hco3"):
        return extracted
    patterns = [
        r'hco\s*3\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'bicarb(?:onate)?\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'bicarbonate\s+(?:level\s+)?(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 5 <= val <= 60:   # physiologic HCO3 range
                extracted["hco3"] = val
                print(f"[Extract] hco3 fallback: {val} mEq/L")
                break
    return extracted

def extract_spo2_fallback(text, extracted):
    if extracted.get("spo2"):
        return extracted
    patterns = [
        r'spo\s*2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'sp\s*o2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'o2\s+sat(?:uration)?\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'sat(?:uration)?\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)\s*%',
        r'(?:oxygen\s+)?sat\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'satting\s+(?:at\s+)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 60 <= val <= 100:   # physiologic SpO2 range
                extracted["spo2"] = val
                print(f"[Extract] spo2 fallback: {val}%")
                break
    return extracted

def extract_rr_fallback(text, extracted):
    if extracted.get("rr"):
        return extracted
    patterns = [
        r'\brr\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'resp(?:iratory)?\s+rate\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'rate\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)\s*(?:/min|breaths)',
        r'(\d+(?:\.\d+)?)\s*breaths?\s*/?\s*min',
        r'breathing\s+(?:at\s+)?(\d+(?:\.\d+)?)\s*(?:/min)?',
        r'set\s+rate\s+(?:of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 4 <= val <= 60:   # physiologic RR range
                extracted["rr"] = val
                print(f"[Extract] rr fallback: {val} /min")
                break
    return extracted

def extract_mode_fallback(text, extracted):
    if extracted.get("mode"):
        return extracted
    # Order matters — more specific patterns first
    mode_patterns = [
        (r'\bvc[-\s]?ac\b',         "VC-AC"),
        (r'\bac[-\s]?vc\b',         "VC-AC"),
        (r'\bpc[-\s]?ac\b',         "PC-AC"),
        (r'\bac[-\s]?pc\b',         "PC-AC"),
        (r'\bprvc\b',               "PRVC"),
        (r'\bpressure\s+regulated\s+volume\s+control\b', "PRVC"),
        (r'\bvc\b',                 "VC-AC"),
        (r'\bpc\b(?!\s*o2|\s*o\b)', "PC-AC"),   # exclude "pco2", "pco"
        (r'\bpsv\b',                "PSV"),
        (r'\bpressure\s+support\b', "PSV"),
        (r'\bcpap\b',               "CPAP"),
        (r'\bsimv\b',               "SIMV"),
        (r'\bassist\s+control\b',   "VC-AC"),
        (r'\bvolume\s+control\b',   "VC-AC"),
        (r'\bpressure\s+control\b', "PC-AC"),
        (r'\bairway\s+pressure\s+release\b', "APRV"),
        (r'\baprv\b',               "APRV"),
    ]
    text_lower = text.lower()
    for pattern, mode_value in mode_patterns:
        if re.search(pattern, text_lower):
            extracted["mode"] = mode_value
            print(f"[Extract] mode fallback: {mode_value}")
            break
    return extracted

def extract_map_fallback(text, extracted):
    if extracted.get("map"):
        return extracted
    patterns = [
        r'\bmap\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'mean\s+arterial\s+(?:pressure\s+)?(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'mean\s+pressure\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 30 <= val <= 180:   # physiologic MAP range
                extracted["map"] = val
                print(f"[Extract] map fallback: {val} mmHg")
                break
    return extracted

def extract_gcs_fallback(text, extracted):
    if extracted.get("gcs"):
        return extracted
    patterns = [
        r'\bgcs\s+(?:is\s+|of\s+|=\s*)?(\d{1,2})',
        r'glasgow\s+(?:coma\s+)?(?:scale\s+)?(?:is\s+|of\s+|=\s*|score\s+)?(\d{1,2})',
        r'gcs\s+score\s+(?:is\s+|of\s+|=\s*)?(\d{1,2})',
        r'coma\s+scale\s+(?:is\s+|of\s+|=\s*)?(\d{1,2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = int(match.group(1))
            if 3 <= val <= 15:   # GCS range
                extracted["gcs"] = val
                print(f"[Extract] gcs fallback: {val}")
                break
    return extracted

def extract_sbt_status_fallback(text, extracted):
    if extracted.get("sbt_status"):
        return extracted
    text_lower = text.lower()

    # ── COMPLETED / PASSED ─────────────────────────────────
    if any(p in text_lower for p in [
        "passed sbt", "sbt passed", "passed the trial", "tolerated sbt",
        "tolerating the trial", "sbt tolerated", "completed sbt",
        "completed the trial", "trial passed", "trial completed",
        # Natural ICU speech
        "he tolerated", "she tolerated", "patient tolerated",
        "tolerated it well", "tolerated well", "trial went well",
        "breathing trial passed", "breathing trial completed",
        "extubation trial done", "extubation trial passed",
        "30 minute trial", "2 hour trial", "two hour trial",
        "spontaneous trial passed", "spontaneous trial completed",
        "doing well on the trial", "tolerating the sbt",
        "ready to extubate", "ready for extubation",
    ]):
        extracted["sbt_status"] = "completed"
        print("[Extract] sbt_status fallback: completed")

    # ── FAILING / FAILED ───────────────────────────────────
    elif any(p in text_lower for p in [
        "failed sbt", "sbt failed", "failed the trial", "trial failed",
        "could not tolerate", "not tolerating", "failing sbt",
        # Natural ICU speech
        "did not tolerate", "couldn't tolerate", "unable to tolerate",
        "failed the breathing trial", "failed breathing trial",
        "trial unsuccessful", "sbt unsuccessful",
        "pulled off the trial", "taken off the trial",
        "put back on the vent", "back on full support",
        "had to stop the trial", "stopped the trial",
        "desaturated during", "distressed during",
        "tachypneic on the trial", "rr climbed during",
    ]):
        extracted["sbt_status"] = "failing"
        print("[Extract] sbt_status fallback: failing")

    # ── IN PROGRESS ────────────────────────────────────────
    elif any(p in text_lower for p in [
        "sbt in progress", "doing the trial", "on sbt now",
        "currently on sbt", "trial in progress", "doing sbt",
        # Natural ICU speech
        "on the trial now", "currently on the trial",
        "we started the sbt", "trial started", "sbt started",
        "on spontaneous trial", "on breathing trial",
        "we tried the sbt", "trialling now",
    ]):
        extracted["sbt_status"] = "passing"
        print("[Extract] sbt_status fallback: passing (in progress)")

    # ── NOT STARTED ────────────────────────────────────────
    elif any(p in text_lower for p in [
        "not started sbt", "sbt not started", "haven't done sbt",
        "no sbt yet", "sbt not done",
        # Natural ICU speech
        "haven't trialled", "not trialled yet",
        "no trial yet", "trial not attempted",
        "planning to do sbt", "about to start sbt",
        "sbt planned", "trial planned for today",
    ]):
        extracted["sbt_status"] = "not started"
        print("[Extract] sbt_status fallback: not started")

    return extracted

def extract_cough_strength_fallback(text, extracted):
    if extracted.get("cough_strength"):
        return extracted
    text_lower = text.lower()

    # ── STRONG ─────────────────────────────────────────────
    if any(p in text_lower for p in [
        "strong cough", "good cough", "cough adequate",
        "cough is strong", "cough is good", "coughing well",
        "pcef > 60", "pcef>60",
        # Natural ICU speech
        "cough reflex intact", "cough reflex present",
        "cough on command", "can cough on command",
        "coughs on command", "coughing on command",
        "cough is present and strong", "cough present",
        "productive cough", "effective cough",
        "cough is effective", "cough force good",
        "able to cough", "cough effort good",
    ]):
        extracted["cough_strength"] = "strong"
        print("[Extract] cough_strength fallback: strong")

    # ── WEAK ───────────────────────────────────────────────
    elif any(p in text_lower for p in [
        "weak cough", "poor cough", "cough weak", "cough is weak",
        "cough is poor", "minimal cough", "pcef < 60", "pcef<60",
        # Natural ICU speech
        "cough is ineffective", "ineffective cough",
        "barely coughing", "cough barely", "cough is minimal",
        "cough effort weak", "cough force weak",
        "cough is present but weak", "feeble cough",
        "cough reflex weak", "cough is diminished",
    ]):
        extracted["cough_strength"] = "weak"
        print("[Extract] cough_strength fallback: weak")

    # ── ABSENT ─────────────────────────────────────────────
    elif any(p in text_lower for p in [
        "no cough", "absent cough", "cannot cough",
        "unable to cough", "no cough reflex",
        # Natural ICU speech
        "cough reflex absent", "no cough reflex",
        "does not cough", "doesn't cough",
        "no cough effort", "cough absent",
        "can't cough", "no spontaneous cough",
    ]):
        extracted["cough_strength"] = "absent"
        print("[Extract] cough_strength fallback: absent")

    return extracted

def extract_vasopressor_dose_fallback(text, extracted):
    if extracted.get("vasopressor_dose"):
        return extracted
    text_lower = text.lower()

    # ── mcg/kg/min → convert to mcg/min using 70 kg standard ──
    # Detects "0.1 mcg/kg/min" format and converts to ~7 mcg/min
    per_kg_patterns = [
        r'(?:norepi(?:nephrine)?|noradrenaline?|na|levophed)\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)\s*(?:mcg|ug)/kg',
        r'vasopressor\s+(?:at\s+)?(\d+(?:\.\d+)?)\s*(?:mcg|ug)/kg',
    ]
    for pattern in per_kg_patterns:
        match = re.search(pattern, text_lower)
        if match:
            val_per_kg = float(match.group(1))
            # Convert using 70 kg standard body weight
            val = round(val_per_kg * 70, 1)
            if 0 < val <= 500:
                extracted["vasopressor_dose"] = val
                print(f"[Extract] vasopressor_dose fallback (per-kg converted): {val} mcg/min")
                return extracted

    # ── Direct mcg/min patterns ────────────────────────────
    patterns = [
        # Norepinephrine — US and international names
        r'norepi(?:nephrine)?\s+(?:at\s+|=\s*|of\s+)?(\d+(?:\.\d+)?)\s*(?:mcg|ug|mic)',
        r'noradrenalin(?:e)?\s+(?:at\s+|=\s*|of\s+)?(\d+(?:\.\d+)?)',
        r'\bna\s+(?:at\s+|=\s*|of\s+)?(\d+(?:\.\d+)?)\s*(?:mcg|ug)',
        r'levophed\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)',
        # Vasopressor generic
        r'vasopressor\s+(?:at\s+|dose\s+|=\s*)?(\d+(?:\.\d+)?)\s*(?:mcg|ug)',
        # Other common vasopressors
        r'phenylephrine\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'epinephrine\s+(?:at\s+|=\s*|adrenaline\s+)?(\d+(?:\.\d+)?)',
        r'adrenalin(?:e)?\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'dopamine\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'vasopressin\s+(?:at\s+|=\s*)?(\d+(?:\.\d+)?)',
        # Bare norepi with number (no unit)
        r'norepi\s+(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            val = float(match.group(1))
            if 0 < val <= 500:   # mcg/min range
                extracted["vasopressor_dose"] = val
                print(f"[Extract] vasopressor_dose fallback: {val} mcg/min")
                break

    # ── Qualitative — no dose number given ────────────────
    # Flags vasopressor presence without a numeric dose
    # Stored as -1 sentinel so vent_reasoning knows vasopressors are running
    if not extracted.get("vasopressor_dose"):
        if any(p in text_lower for p in [
            "on vasopressor", "vasopressors running", "on pressors",
            "pressor dependent", "vasopressor support",
            "on noradrenaline", "on norepi", "on levophed",
            "low dose vasopressor", "minimal vasopressor",
            "vasopressor weaning", "weaning pressors",
        ]):
            extracted["vasopressor_dose"] = -1   # sentinel: present, dose unknown
            print("[Extract] vasopressor_dose fallback: -1 (present, dose unknown)")

    return extracted

def extract_baseline_paco2_fallback(text, extracted):
    if extracted.get("baseline_paco2"):
        return extracted
    patterns = [
        # Structured / typed format (most reliable — check first)
        r'baseline[_\s]paco\s*2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        r'baseline[_\s]co\s*2\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        # "usual/chronic/known/normal CO2 is X"
        r'(?:their\s+|his\s+|her\s+|the\s+)?(?:usual|chronic|baseline|known|normal)\s+(?:paco2|co2|pco2)\s+(?:is\s+|of\s+|=\s*|was\s+|around\s+)?(\d+(?:\.\d+)?)',
        # "CO2 usually/normally/chronically X"
        r'(?:paco2|co2|pco2)\s+(?:baseline|usually|normally|chronically|typically)\s+(?:is\s+|of\s+|=\s*|around\s+|runs?\s+(?:at\s+)?)?(\d+(?:\.\d+)?)',
        # "CO2 runs at X" / "CO2 runs around X"
        r'(?:paco2|co2|pco2)\s+runs?\s+(?:at\s+|around\s+)?(\d+(?:\.\d+)?)',
        # "his/her CO2 is usually X" / "their CO2 is normally X"
        r'(?:his|her|their|the\s+patient.s)\s+(?:paco2|co2|pco2)\s+(?:is\s+)?(?:usually|normally|typically|chronically|around|about)\s+(\d+(?:\.\d+)?)',
        # "normal CO2 is X" / "normal CO2 of X"
        r'normal\s+(?:paco2|co2|pco2)\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        # "COPD baseline X" — bare context
        r'copd\s+baseline\s+(?:paco2|co2)?\s+(?:is\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        # "CO2 usually around 60" / "CO2 is around 65"
        r'(?:paco2|co2|pco2)\s+(?:is\s+)?(?:usually\s+)?around\s+(\d+(?:\.\d+)?)',
        # "baseline is 62" — only when near COPD context keywords
        r'baseline\s+(?:is\s+|=\s*|of\s+)?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 35 <= val <= 120:   # COPD baseline PaCO2 range
                extracted["baseline_paco2"] = val
                print(f"[Extract] baseline_paco2 fallback: {val} mmHg")
                break
    return extracted

def extract_prior_paco2_fallback(text, extracted):
    if extracted.get("prior_paco2"):
        return extracted
    patterns = [
        # Structured / typed format (most reliable — check first)
        r'prior[_\s]paco\s*2\s+(?:is\s+|of\s+|=\s*|was\s+)?(\d+(?:\.\d+)?)',
        r'prior[_\s]co\s*2\s+(?:is\s+|of\s+|=\s*|was\s+)?(\d+(?:\.\d+)?)',
        # "previous/last/prior PaCO2/CO2 was X"
        r'(?:previous|last|prior|earlier|old)\s+(?:paco2|co2|pco2)\s+(?:was\s+|of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
        # "CO2 was X yesterday/before/earlier/on admission/this morning"
        r'(?:paco2|co2|pco2)\s+was\s+(\d+(?:\.\d+)?)\s+(?:yesterday|before|earlier|last|on\s+admission|this\s+morning|initially|at\s+admission)',
        # "CO2 on admission was X" / "initial CO2 was X"
        r'(?:initial|admission|morning|first|presenting)\s+(?:paco2|co2|pco2)\s+(?:was\s+|of\s+|=\s*|is\s+)?(\d+(?:\.\d+)?)',
        # "CO2 came back at X" / "CO2 came back as X"
        r'(?:paco2|co2|pco2)\s+came\s+back\s+(?:at\s+|as\s+)?(\d+(?:\.\d+)?)',
        # "first ABG showed CO2 of X" / "ABG showed CO2 X"
        r'(?:first\s+)?abg\s+showed\s+(?:paco2|co2|pco2)\s+(?:of\s+|=\s*)?(\d+(?:\.\d+)?)',
        # "the old CO2 was X"
        r'(?:the\s+)?old\s+(?:paco2|co2|pco2)\s+(?:was\s+|of\s+|=\s*)?(\d+(?:\.\d+)?)',
        # "CO2 started at X" / "CO2 was X when admitted"
        r'(?:paco2|co2|pco2)\s+(?:started\s+at|was\s+when\s+admitted)\s+(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            val = float(match.group(1))
            if 15 <= val <= 150:   # physiologic PaCO2 range
                extracted["prior_paco2"] = val
                print(f"[Extract] prior_paco2 fallback: {val} mmHg")
                break
    return extracted

def extract_data(text):
    try:
        s = text.find("<data>") + 6
        e = text.find("</data>")
        if s > 5 and e > 0:
            parsed = json.loads(text[s:e].strip())
            return {k: v for k, v in parsed.items() if v is not None}
    except Exception as ex:
        print("[extract_data] error:", ex)
    return {}

def extract_narrative(text):
    try:
        s = text.find("CLINICAL NARRATIVE:") + len("CLINICAL NARRATIVE:")
        e = text.find("SAFETY FLAGS:")
        if s > 0 and e > 0:
            return text[s:e].strip()
    except Exception:
        pass
    return ""

def extract_safety_flags(text):
    try:
        s = text.find("SAFETY FLAGS:") + len("SAFETY FLAGS:")
        e = text.find("DATA EXTRACTION:")
        if s > 0 and e > 0:
            flags = text[s:e].strip()
            return "" if flags.lower() == "none identified" else flags
    except Exception:
        pass
    return ""

# ══════════════════════════════════════════════════════════════════
# GEMINI API TIMEOUT + RETRY WRAPPER
# 3 attempts with exponential backoff: 1s → 2s → 4s
# Per-attempt timeout: 20s (covers typical 2.5-flash latency)
# On total failure: returns None so caller can degrade gracefully
# ══════════════════════════════════════════════════════════════════

async def _call_gemini_with_retry(context: str, max_attempts: int = 3):
    """
    Wraps client.aio.models.generate_content with:
      - Per-call asyncio timeout (20 s)
      - Exponential backoff on timeout or API error (1s / 2s / 4s)
      - Returns response object on success, None on total failure

    Caller is responsible for graceful degradation when None is returned.
    """
    delays = [1, 2, 4]   # seconds between retries

    for attempt in range(1, max_attempts + 1):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=context,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.15
                    )
                ),
                timeout=20.0   # seconds — aborts if Gemini stalls
            )
            return response   # ✅ success

        except asyncio.TimeoutError:
            print(f"[Gemini] Attempt {attempt}/{max_attempts} TIMED OUT (20s)")
        except Exception as e:
            # Catches: google.api_core.exceptions.ServiceUnavailable (503),
            #          ResourceExhausted (429), InternalServerError (500),
            #          and any unexpected SDK errors
            print(f"[Gemini] Attempt {attempt}/{max_attempts} ERROR: {type(e).__name__}: {e}")

        if attempt < max_attempts:
            wait = delays[attempt - 1]
            print(f"[Gemini] Retrying in {wait}s...")
            await asyncio.sleep(wait)

    print("[Gemini] All 3 attempts failed — returning None for graceful degradation")
    return None

async def process_text_input(case_context, user_input):
    recent_vent   = case_context.get("vent_settings_history", [])[-3:]
    recent_abg    = case_context.get("abg_history", [])[-3:]
    recent_hemo   = case_context.get("hemodynamics", [])[-3:]
    recent_events = case_context.get("events", [])[-5:]
    recent_ai     = case_context.get("ai_assessments", [])[-2:]
    ibw_kg        = case_context.get("ibw_kg")

    prev = []
    for a in recent_ai:
        rec = a.get("sccm_recommendation", {})
        if rec:
            prev.append(
                "[" + a.get("timestamp", "")[:16] + "] "
                "Status: " + rec.get("ventilation_status", "?") + " | "
                "Action: " + rec.get("immediate_next_step", "")[:80] + "..."
            )

    context = (
        "=== PATIENT CONTEXT ===\n"
        "Diagnosis: " + case_context.get("diagnosis", "Unknown") + "\n"
        "IBW: " + (str(ibw_kg) + " kg" if ibw_kg else "Not provided") + "\n\n"
        "Recent Vent Settings:\n" + json.dumps(recent_vent, indent=2) + "\n\n"
        "Recent ABG:\n"           + json.dumps(recent_abg,  indent=2) + "\n\n"
        "Recent Hemodynamics:\n"  + json.dumps(recent_hemo, indent=2) + "\n\n"
        "Previous Assessments:\n" + "\n".join(prev) + "\n\n"
        "=== TRANSCRIPTION CORRECTION ===\n"
        "The input below may be a voice transcript with speech recognition errors, "
        "mixed Arabic/English words, noise tags like <noise>, or garbled medical terms.\n"
        "Silently correct ALL errors using ICU medical context before extracting.\n"
        "Examples: 'Batient on ACVC' → 'Patient on AC/VC'\n"
        "          'بيبت اس 0.7' → 'PEEP 0.7'\n"
        "          'tidal 5 2 0' → 'tidal volume 520'\n"
        "          '<noise>' → ignore\n"
        "          'fio two' → 'FiO2'\n"
        "Do NOT mention the correction in your output. Just extract the corrected values.\n\n"
        "=== DOCTOR INPUT ===\n" + user_input
    )

    response = await _call_gemini_with_retry(context)

    # ── Graceful degradation on total Gemini failure ──────────────
    # All 3 attempts failed (timeout or API error).
    # Regex fallbacks still run below — return a yellow warning
    # in the narrative so the doctor knows AI extraction was partial.
    if response is None:
        print("[Process] Gemini unavailable — regex-only fallback active")
        narrative    = (
            "⚠️ AI extraction temporarily unavailable "
            "(Gemini API timeout after 3 attempts). "
            "Ventilation reasoning is based on structured field "
            "fallbacks only. Please re-submit or type values explicitly."
        )
        safety_flags = ["Gemini API unavailable — regex fallback mode"]
        extracted    = {}
        # Still run all regex fallbacks so numeric fields are captured
        extracted = extract_auto_peep_fallback(user_input, extracted)
        extracted = extract_insp_flow_fallback(user_input, extracted)
        extracted = extract_bp_fallback(user_input, extracted)
        extracted = extract_hr_fallback(user_input, extracted)
        extracted = extract_pplat_fallback(user_input, extracted)
        extracted = extract_peep_fallback(user_input, extracted)
        extracted = extract_tv_fallback(user_input, extracted)
        extracted = extract_fio2_fallback(user_input, extracted)
        extracted = extract_ph_fallback(user_input, extracted)
        extracted = extract_paco2_fallback(user_input, extracted)
        extracted = extract_pao2_fallback(user_input, extracted)
        extracted = extract_hco3_fallback(user_input, extracted)
        extracted = extract_spo2_fallback(user_input, extracted)
        extracted = extract_rr_fallback(user_input, extracted)
        extracted = extract_mode_fallback(user_input, extracted)
        extracted = extract_map_fallback(user_input, extracted)
        extracted = extract_gcs_fallback(user_input, extracted)
        extracted = extract_sbt_status_fallback(user_input, extracted)
        extracted = extract_cough_strength_fallback(user_input, extracted)
        extracted = extract_vasopressor_dose_fallback(user_input, extracted)
        extracted = extract_baseline_paco2_fallback(user_input, extracted)
        extracted = extract_prior_paco2_fallback(user_input, extracted)
        return narrative, safety_flags, extracted
    # ── Normal path continues ─────────────────────────────────────

    text         = response.text
    narrative    = extract_narrative(text)
    safety_flags = extract_safety_flags(text)
    extracted    = extract_data(text)
    extracted    = extract_auto_peep_fallback(user_input, extracted)
    extracted    = extract_insp_flow_fallback(user_input, extracted)
    extracted    = extract_bp_fallback(user_input, extracted)
    extracted    = extract_hr_fallback(user_input, extracted)
    extracted    = extract_pplat_fallback(user_input, extracted)
    extracted    = extract_peep_fallback(user_input, extracted)
    extracted    = extract_tv_fallback(user_input, extracted)
    extracted    = extract_fio2_fallback(user_input, extracted)
    extracted    = extract_ph_fallback(user_input, extracted)
    extracted    = extract_paco2_fallback(user_input, extracted)
    extracted    = extract_pao2_fallback(user_input, extracted)
    extracted    = extract_hco3_fallback(user_input, extracted)
    extracted    = extract_spo2_fallback(user_input, extracted)
    extracted    = extract_rr_fallback(user_input, extracted)
    extracted    = extract_mode_fallback(user_input, extracted)
    extracted    = extract_map_fallback(user_input, extracted)
    extracted    = extract_gcs_fallback(user_input, extracted)
    extracted    = extract_sbt_status_fallback(user_input, extracted)
    extracted    = extract_cough_strength_fallback(user_input, extracted)
    extracted    = extract_vasopressor_dose_fallback(user_input, extracted)
    extracted    = extract_baseline_paco2_fallback(user_input, extracted)
    extracted    = extract_prior_paco2_fallback(user_input, extracted)
    return narrative, safety_flags, extracted
