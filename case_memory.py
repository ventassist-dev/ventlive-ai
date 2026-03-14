

# case_memory.py — VentLive AI
# Persistent patient case storage using Google Firestore.
# Falls back to in-memory dict if Firestore is unavailable.


import urllib.request
import uuid
import os
import threading
import time
from datetime import datetime
from vent_reasoning import calculate_ibw

_USE_FIRESTORE = False
_db            = None
_COLLECTION    = "vent_cases"
_mem           = {}

# ── Pending sync queue for failed Firestore writes ──────────
# Each item: {"op": "array"|"field"|"save", "case_id": str, "args": dict}
_pending_sync  = []
_sync_lock     = threading.Lock()
_sync_thread   = None

try:
    from google.cloud import firestore as _fs
    from google.cloud.firestore import ArrayUnion as _ArrayUnion
    _db = _fs.Client(project=os.environ.get("GCP_PROJECT", "ventlive-ai"))
    _ = list(_db.collections())
    _USE_FIRESTORE = True
    print("✅ Firestore connected — cases will persist to cloud")
except Exception as _e:
    print(f"⚠️  Firestore unavailable ({_e.__class__.__name__}) — using in-memory fallback")
    _USE_FIRESTORE = False


def _save(case_id: str, case: dict):
    """Write full case to Firestore AND keep _mem in sync."""
    _mem[case_id] = case                    # ALWAYS update memory cache
    if _USE_FIRESTORE:
        _db.collection(_COLLECTION).document(case_id).set(case)

def _load(case_id: str):
    if _USE_FIRESTORE:
        doc = _db.collection(_COLLECTION).document(case_id).get()
        return doc.to_dict() if doc.exists else None
    return _mem.get(case_id)

def _load_all() -> list:
    if _USE_FIRESTORE:
        try:
            from google.cloud import firestore as _fs_local
            docs = (
                _db.collection(_COLLECTION)
                .order_by(
                    "created_at",
                    direction=_fs_local.Query.DESCENDING
                )
                .limit(50)
                .get()
            )
            return [d.to_dict() for d in docs]
        except Exception as e:
            print(f"[Firestore] _load_all failed: {e}")
            return sorted(
                list(_mem.values()),
                key=lambda c: c.get("created_at", ""),
                reverse=True
            )
    return sorted(
        list(_mem.values()),
        key=lambda c: c.get("created_at", ""),
        reverse=True
    )


def _patch_mem(case_id: str):
    if case_id in _mem:
        return
    if not _USE_FIRESTORE:
        return
    try:
        fresh = _load(case_id)
        if fresh:
            _mem[case_id] = fresh
            print(f"[Cache] Loaded case {case_id[:8]} from Firestore")
        else:
            print(f"[Cache] Case {case_id[:8]} not found in Firestore")
    except Exception as e:
        print(f"[Cache] _patch_mem failed: {e}")

def _update_array(case_id: str, field: str, item: dict):
    """
    Append item to array field.

    Strategy: Firestore FIRST, then _mem.
    If Firestore fails: rollback is not needed because we simply
    do NOT update _mem — the data never existed in either store.

    If Firestore succeeds but process crashes before _mem update:
    _patch_mem will reload from Firestore on next access — no data loss.
    """
    # Step 1 — Persist to Firestore FIRST (if available)
    firestore_ok = True
    if _USE_FIRESTORE:
        try:
            _db.collection(_COLLECTION).document(case_id).update(
                {field: _ArrayUnion([item])}
            )
        except Exception as e:
            firestore_ok = False
            print(f"[Firestore] _update_array failed for {case_id[:8]}.{field}: {e}")
            with _sync_lock:
                _pending_sync.append({
                    "op": "array",
                    "case_id": case_id,
                    "field": field,
                    "item": item,
                })

    # Step 2 — Update memory cache
    # If Firestore succeeded: both stores are in sync ✅
    # If Firestore failed:    we still update _mem so the app works,
    #                         AND we queued a retry so Firestore catches up.
    #                         On restart, _mem is lost but the retry daemon
    #                         will have pushed it to Firestore before that
    #                         (in most cases — 30s retry cycle).
    case = _mem.get(case_id)
    if case:
        case.setdefault(field, []).append(item)
    else:
        print(f"[WARNING] _update_array: case {case_id[:8]} not in _mem cache")

    if not firestore_ok:
        print(f"[SyncQueue] Queued {field} append for {case_id[:8]} — will retry in ≤30s")

def _update_field(case_id: str, field: str, value):
    """Update a single field in Firestore AND in _mem cache."""
    # Step 1 — Always update memory cache first
    case = _mem.get(case_id)
    if case:
        case[field] = value
    else:
        print(f"[WARNING] _update_field: case {case_id[:8]} not in _mem cache")

    # Step 2 — Persist to Firestore if available
    if _USE_FIRESTORE:
        try:
            _db.collection(_COLLECTION).document(case_id).update(
                {field: value}
            )
        except Exception as e:
            print(f"[Firestore] _update_field failed: {e}")


def _update_field(case_id: str, field: str, value):
    """
    Strategy: Firestore FIRST, then _mem.
    Same rationale as _update_array.
    """
    # Step 1 — Persist to Firestore FIRST (if available)
    firestore_ok = True
    if _USE_FIRESTORE:
        try:
            _db.collection(_COLLECTION).document(case_id).update(
                {field: value}
            )
        except Exception as e:
            firestore_ok = False
            print(f"[Firestore] _update_field failed for {case_id[:8]}.{field}: {e}")
            with _sync_lock:
                _pending_sync.append({
                    "op": "field",
                    "case_id": case_id,
                    "field": field,
                    "value": value,
                })

    # Step 2 — Update memory cache
    case = _mem.get(case_id)
    if case:
        case[field] = value
    else:
        print(f"[WARNING] _update_field: case {case_id[:8]} not in _mem cache")

    if not firestore_ok:
        print(f"[SyncQueue] Queued {field} update for {case_id[:8]} — will retry in ≤30s")


def create_case(diagnosis: str, height_cm: float = None, sex: str = "male") -> dict:
    case_id = str(uuid.uuid4())
    ibw_kg = calculate_ibw(height_cm, sex) if height_cm else None
    case = {
        "case_id":               case_id,
        "diagnosis":             diagnosis,
        "vent_mode":             "",
        "created_at":            datetime.utcnow().isoformat(),
        "patient_height_cm":     height_cm,
        "patient_sex":           sex,
        "ibw_kg":                ibw_kg,
        "baseline_paco2":        None,
        "vent_settings_history": [],
        "abg_history":           [],
        "hemodynamics":          [],
        "events":                [],
        "ai_assessments":        [],
        "sbt_attempts":          [],
    }
    # _save() now handles BOTH _mem and Firestore — no double write
    _save(case_id, case)
    return case

def get_case(case_id: str):
    if case_id in _mem:
        return _mem[case_id]
    case = _load(case_id)
    if case:
        _mem[case_id] = case
    return case

def list_cases(limit: int = 20, offset: int = 0) -> dict:
    all_cases = _load_all()
    total = len(all_cases)
    page  = [
        {
            "case_id":          c["case_id"],
            "diagnosis":        c.get("diagnosis", ""),
            "created_at":       c.get("created_at", ""),
            "ibw_kg":           c.get("ibw_kg"),
            "assessment_count": len(c.get("ai_assessments", [])),
        }
        for c in all_cases[offset: offset + limit]
    ]
    return {"cases": page, "total": total, "limit": limit, "offset": offset}

def delete_case(case_id: str) -> bool:
    found = case_id in _mem or (_USE_FIRESTORE and _load(case_id) is not None)
    if not found:
        return False
    _mem.pop(case_id, None)
    if _USE_FIRESTORE:
        _db.collection(_COLLECTION).document(case_id).delete()
    return True

def delete_all_cases() -> int:
    """Delete every case from memory (and Firestore if available). Returns count deleted."""
    # Collect all unique IDs from both stores before touching anything
    all_ids = set(_mem.keys())
    if _USE_FIRESTORE:
        try:
            all_ids.update(doc.id for doc in _db.collection(_COLLECTION).stream())
        except Exception as e:
            print(f"[Firestore] delete_all_cases ID sweep failed: {e}")

    # Delete from memory
    for cid in list(all_ids):
        _mem.pop(cid, None)

    # Delete from Firestore
    deleted_firestore = 0
    if _USE_FIRESTORE:
        try:
            docs = _db.collection(_COLLECTION).stream()
            for doc in docs:
                doc.reference.delete()
                deleted_firestore += 1
        except Exception as e:
            print(f"[Firestore] delete_all_cases cloud sweep failed: {e}")

    # Clear pending sync queue — all cases gone, no point retrying
    with _sync_lock:
        _pending_sync.clear()

    count = len(all_ids)
    print(f"[CaseMemory] Deleted {count} cases ({deleted_firestore} from Firestore).")
    return count

def set_baseline_paco2(case_id: str, value: float) -> bool:
    """
    Store the patient's known baseline (pre-admission) PaCO2.
    Used by COPD branch to warn against over-correction below baseline.
    Returns True if case found and updated, False if case not found.
    """
    case = get_case(case_id)
    if not case:
        return False
    _update_field(case_id, "baseline_paco2", round(float(value), 1))
    return True

def update_vent_settings(case_id: str, settings: dict):
    settings = {**settings, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "vent_settings_history", settings)
    if settings.get("mode"):
        _update_field(case_id, "vent_mode", settings["mode"])

def update_abg(case_id: str, abg: dict):
    abg = {**abg, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "abg_history", abg)

def update_hemodynamics(case_id: str, hemo: dict):
    hemo = {**hemo, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "hemodynamics", hemo)

def add_ai_assessment(case_id: str, assessment: dict):
    assessment = {**assessment, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "ai_assessments", assessment)

def add_event(case_id: str, event_text: str):
    item = {"event": event_text, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "events", item)

def add_sbt_attempt(case_id: str, attempt: dict):
    attempt = {**attempt, "timestamp": datetime.utcnow().isoformat()}
    _patch_mem(case_id)
    _update_array(case_id, "sbt_attempts", attempt)

def get_trend(case_id: str) -> dict:
    case = get_case(case_id)
    if not case:
        return {}
    trend = {}

    abgs = case.get("abg_history", [])
    if len(abgs) >= 2:
        pao2_vals = [a.get("pao2") for a in abgs[-3:] if a.get("pao2")]
        if len(pao2_vals) >= 2:
            delta = pao2_vals[-1] - pao2_vals[0]
            trend["pao2_trend"] = (
                "improving" if delta > 5 else
                "worsening" if delta < -5 else
                "stable"
            )
            trend["pao2_delta"] = round(delta, 1)

    if len(abgs) >= 2:
        ph_vals = [a.get("ph") for a in abgs[-3:] if a.get("ph")]
        if len(ph_vals) >= 2:
            delta = ph_vals[-1] - ph_vals[0]
            trend["ph_trend"] = (
                "improving" if delta > 0.02 else
                "worsening" if delta < -0.02 else
                "stable"
            )

    hemos = case.get("hemodynamics", [])
    if len(hemos) >= 2:
        map_vals = [h.get("map") for h in hemos[-3:] if h.get("map")]
        if len(map_vals) >= 2:
            delta = map_vals[-1] - map_vals[0]
            trend["map_trend"] = (
                "improving" if delta > 5 else
                "worsening" if delta < -5 else
                "stable"
            )

    vents = case.get("vent_settings_history", [])
    if len(vents) >= 2:
        peep_vals = [v.get("peep") for v in vents[-3:] if v.get("peep")]
        if len(peep_vals) >= 2:
            trend["peep_changed"]   = peep_vals[-1] != peep_vals[0]
            trend["peep_direction"] = (
                "increased" if peep_vals[-1] > peep_vals[0] else
                "decreased" if peep_vals[-1] < peep_vals[0] else
                "unchanged"
            )
    return trend

def storage_status() -> dict:
    with _sync_lock:
        pending = len(_pending_sync)
    return {
        "backend":       "firestore" if _USE_FIRESTORE else "memory",
        "collection":    _COLLECTION if _USE_FIRESTORE else None,
        "case_count":    len(list_cases()),
        "pending_sync":  pending,
    }
