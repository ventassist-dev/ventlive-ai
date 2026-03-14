
# ── Helpers ───────────────────────────────────────────────

def _f(v):
    try:   return float(v) if v is not None else None
    except (ValueError, TypeError): return None

def _bp_sys(bp):
    try:
        if bp and "/" in str(bp): return float(str(bp).split("/")[0])
    except (ValueError, TypeError): pass
    return None

def _bp_dia(bp):
    try:
        if bp and "/" in str(bp): return float(str(bp).split("/")[1])
    except (ValueError, TypeError): pass
    return None

def calculate_ibw(height_cm, sex="male"):
    """
    Devine formula IBW for lung-protective ventilation.
    Minimum floor: 50 kg male / 45.5 kg female — ARDSNet standard for short patients.
    Source: ARDSNet protocol, ecampusontario.pressbooks.pub/mcvresource
    """
    if not height_cm: return None
    h = height_cm / 2.54  # cm to inches
    if sex.lower() == "female":
        ibw = 45.5 + 2.3 * (h - 60)
        return round(max(ibw, 45.5), 1)  # floor at 45.5 kg
    else:
        ibw = 50.0 + 2.3 * (h - 60)
        return round(max(ibw, 50.0), 1)  # floor at 50.0 kg

def calculate_driving_pressure(pplat, peep):
    p, e = _f(pplat), _f(peep)
    return round(p - e, 1) if p is not None and e is not None else None

def calculate_pf_ratio(pao2, fio2):
    p, f = _f(pao2), _f(fio2)
    if not (p and f): return None
    # PaO2 valid range: 20–700 mmHg | FiO2 valid range: 0.21–1.0
    if not (20 <= p <= 700): return None
    if not (0.21 <= f <= 1.0): return None
    result = round(p / f, 0)
    # P/F < 50 is incompatible with life | P/F > 500 is physiologically impossible
    return result if 50 <= result <= 500 else None

def calculate_rsbi(rr, tv_liters):
    r, t = _f(rr), _f(tv_liters)
    if not (r and t and t > 0):
        return None
    # Unit guard: TV > 2L is almost certainly stored in mL — convert to litres
    # Max physiologic TV even in large patients is ~2L (Yang & Tobin 1991)
    if t > 2:
        t = t / 1000
    result = round(r / t, 1)
    # Physiologic RSBI range: min = RR 8 / TV 2L = 4
    # Max = RR 35 (ARDSNet ceiling) / TV 0.08L = 437
    # Anything outside 1–450 is a calculation error, not a clinical value
    return result if 1 <= result <= 450 else None

def calculate_map(bp_sys, bp_dia):
    s, d = _f(bp_sys), _f(bp_dia)
    return round((s + 2*d)/3, 1) if s and d else None

def calculate_tv_per_kg(tv_ml, ibw_kg):
    t, i = _f(tv_ml), _f(ibw_kg)
    return round(t/i, 2) if t and i and i > 0 else None
def calculate_sf_ratio(spo2, fio2):
    """
    S/F ratio = SpO2 / FiO2 — surrogate for P/F when ABG unavailable.
    Validated thresholds (Rice 2007):
      S/F < 235  ≈  P/F < 200  (moderate ARDS)
      S/F < 150  ≈  P/F < 150  (prone threshold)
      S/F < 89   ≈  P/F < 100  (severe ARDS)
    Only valid when SpO2 <= 97% (above 97% the curve is flat
    and S/F overestimates P/F — unreliable).
    fio2: decimal (0.21–1.0)
    spo2: percentage (88–97)
    """
    s, f = _f(spo2), _f(fio2)
    if not (s and f and f > 0):
        return None
    # Reliability gate — SpO2 > 97% makes S/F unreliable
    if s > 97:
        return None
    # SpO2 must be physiologic
    if s < 60 or s > 100:
        return None
    return round(s / f, 0)
def lookup_ardsnet_peep(fio2, severity_tag="moderate"):
    """
    ARDSNet PEEP/FiO2 tables — Lower and Higher PEEP strategies.
    ATS 2024: Higher PEEP for moderate-to-severe ARDS.
              Lower PEEP for mild ARDS.
    ESICM 2023: No preference — individualize.

    fio2: decimal (0.21 to 1.0)
    severity_tag: "mild" | "moderate" | "severe" | "unclassified"

    Returns: dict with lower_peep, higher_peep, recommended_peep, rationale
    """

    # ARDSNet Lower PEEP / Higher FiO2 table (step-function, no overlaps)
    # Source: ARDSNet ARMA trial protocol — ATS 2024 / ESICM 2023
    # Each row: (fio2_min_inclusive, peep) — highest matching threshold wins
    LOWER_PEEP_TABLE = [
        (0.21, 5),
        (0.50, 8),
        (0.60, 10),
        (0.80, 14),
        (0.90, 16),
        (1.00, 18),
    ]

    # ARDSNet Higher PEEP / Lower FiO2 table (step-function, no overlaps)
    # Source: ARDSNet 2008 protocol card — minimum valid PEEP per FiO2 breakpoint
    # Higher PEEP strategy for moderate-to-severe ARDS (ATS 2024)
    HIGHER_PEEP_TABLE = [
        (0.21, 5),   # FiO2 < 0.30 — minimum PEEP
        (0.30, 10),  # FiO2 0.30: valid range 5-14, use 10 (middle)
        (0.40, 14),  # FiO2 0.40: valid range 14-16, minimum is 14
        (0.50, 18),  # FiO2 0.50: valid range 16-20, use 18 (middle)
        (0.60, 20),  # FiO2 0.60-0.80: protocol specifies 20
        (0.80, 22),  # FiO2 0.80-0.90: protocol specifies 22
        (0.90, 22),  # FiO2 0.90: protocol specifies 22
        (1.00, 24),  # FiO2 1.00: protocol specifies 22-24, use 24
    ]

    def _lookup(table, fio2_val):
        # Step-function: take the highest threshold that fio2_val meets
        result = table[0][1]  # default to lowest PEEP
        for fio2_min, peep in table:
            if fio2_val >= fio2_min:
                result = peep
            else:
                break
        return result

    f = _f(fio2)
    if f is None:
        return None

    # Clamp to valid range
    f = max(0.21, min(1.0, f))

    lower_peep  = _lookup(LOWER_PEEP_TABLE,  f)
    higher_peep = _lookup(HIGHER_PEEP_TABLE, f)

    # Recommendation logic based on severity
    if severity_tag in ["moderate", "severe"]:
        recommended        = higher_peep
        recommended_table  = "Higher PEEP"
        rationale = (
            "ATS 2024 recommends Higher PEEP table for " +
            severity_tag + " ARDS. "
            "ESICM 2023 found insufficient evidence — "
            "use clinical judgment and monitor response."
        )
    else:
        recommended        = lower_peep
        recommended_table  = "Lower PEEP"
        rationale = (
            "ATS 2024 recommends Lower PEEP table for mild ARDS. "
            "Avoid overdistension — monitor driving pressure after change."
        )

    return {
        "fio2":             round(f, 2),
        "lower_peep":       lower_peep,
        "higher_peep":      higher_peep,
        "recommended_peep": recommended,
        "recommended_table": recommended_table,
        "rationale":        rationale,
    }
# ── Global status assessment ───────────────────────────────

def assess_ventilation_status(data, trend=None):
    ph        = _f(data.get("ph"))
    paco2     = _f(data.get("paco2"))
    pao2      = _f(data.get("pao2"))
    fio2      = _f(data.get("fio2")) or 1.0
    bp_sys    = _f(data.get("bp_sys")) or _bp_sys(data.get("bp"))
    auto_peep = _f(data.get("auto_peep"))
    pplat     = _f(data.get("pplat"))
    peep      = _f(data.get("peep"))
    spo2      = _f(data.get("spo2"))
    hr        = _f(data.get("hr"))
    dp        = calculate_driving_pressure(pplat, peep)
    pf        = calculate_pf_ratio(pao2, fio2)
    if pf is None:
        pf_stated = _f(data.get("pf_ratio_stated"))
        if pf_stated and 50 <= pf_stated <= 500:
            pf = pf_stated
    if pf is None and spo2 and fio2:
        sf = calculate_sf_ratio(spo2, fio2)
        if sf is not None:
            if sf < 89:    pf = 85
            elif sf < 150: pf = 130
            elif sf < 235: pf = 180

    if any([
        ph and ph < 7.20,
        ph and ph > 7.60,
        paco2 and paco2 > 80,
        bp_sys and bp_sys < 80,
        auto_peep and auto_peep >= 10,
        dp and dp >= 20,
        spo2 and spo2 < 85,
        hr and hr > 150,
    ]):
        return "Critical"

    # Three-tier pH: 7.20–7.25 danger zone triggers Worsening
    # (not Critical — doctor has time to act but must act now)
    if ph and 7.20 <= ph < 7.25:
        return "Worsening"

    if pf and pf < 100:
        if any([bp_sys and bp_sys < 90, spo2 and spo2 < 88, ph and ph < 7.25]):
            return "Critical"
        return "Worsening"

    # Get TV per kg if available
    tv      = _f(data.get("tv"))
    ibw_kg  = _f(data.get("ibw_kg"))
    tv_pkg  = calculate_tv_per_kg(tv, ibw_kg) if tv and ibw_kg else None

    worsening = any([
        ph and (ph < 7.30 or ph > 7.55),
        pf and pf < 200,
        paco2 and paco2 > 60,
        bp_sys and bp_sys < 90,
        auto_peep and auto_peep >= 5,
        dp and dp >= 15,        # ← changed from 14 to 15
        spo2 and spo2 < 90,
        pplat and pplat > 30,
        tv_pkg and tv_pkg > 7.0,          # TV > 7 mL/kg IBW is worsening risk
        fio2 and fio2 >= 0.65,            # high FiO2 without ABG = worsening
    ])
    if not worsening and trend:
        worsening = any([
            trend.get("pao2_trend") == "worsening",
            trend.get("ph_trend")   == "worsening",
            trend.get("map_trend")  == "worsening",
        ])
    return "Worsening" if worsening else "Stable"


# ════════════════════════════════════════════════════════════
# WEANING HELPER — called from generate_sccm_recommendation
# Sources: ATS/ACCP 2017, AARC 2024, ESICM 2023
# ════════════════════════════════════════════════════════════

def _is_negated(text, keyword, window=50):
    """
    Returns True if `keyword` appears in `text` AND is preceded
    by a negation word within `window` characters before it.

    Clinical negation patterns covered:
      "has NOT failed", "did not fail", "patient never failed",
      "not distressed", "no accessory muscle use",
      "hasn't passed", "has not passed", "did not pass"

    Window of 50 chars catches:
      "patient has not [50 chars] failed" — standard clinical phrasing
    """
    idx = text.find(keyword)
    if idx < 0:
        return False   # keyword not present at all

    # Look at the text in the window immediately before the keyword
    start   = max(0, idx - window)
    pre_txt = text[start:idx]

    negation_words = [
        "not ", "n't ", "no ", "never ", "without ",
        "has not", "have not", "had not", "did not",
        "does not", "do not", "is not", "was not",
        "hasn't", "haven't", "hadn't", "didn't",
        "doesn't", "don't", "isn't", "wasn't",
        "denies", "denying", "ruled out", "negative for",
        "no evidence of", "no sign of",
    ]
    return any(neg in pre_txt for neg in negation_words)

def _weaning_branch(diagnosis, data, ibw_kg, trend, dp_label):

    fio2    = _f(data.get("fio2"))
    peep    = _f(data.get("peep"))
    ph      = _f(data.get("ph"))
    pao2    = _f(data.get("pao2"))
    paco2   = _f(data.get("paco2"))
    spo2    = _f(data.get("spo2"))
    rr      = _f(data.get("rr"))
    hr      = _f(data.get("hr"))
    tv      = _f(data.get("tv"))
    pplat   = _f(data.get("pplat"))
    map_val = _f(data.get("map"))
    gcs     = _f(data.get("gcs"))
    bp      = data.get("bp", "")
    bp_sys  = _f(data.get("bp_sys")) or _bp_sys(bp)
    bp_dia  = _f(data.get("bp_dia")) or _bp_dia(bp)
    raw     = data.get("_raw_input", "").lower()
    diag    = diagnosis.lower()

    if not map_val and bp_sys and bp_dia:
        map_val = calculate_map(bp_sys, bp_dia)

    dp = calculate_driving_pressure(pplat, peep)
    pf = calculate_pf_ratio(pao2, fio2)

    tv_per_kg = calculate_tv_per_kg(tv, ibw_kg) if tv and ibw_kg else None

    rsbi = None
    if rr and tv:
        # TV > 100 → stored in mL → convert to litres for RSBI
        # TV <= 100 → already in litres (physiologic range 0.3–0.8 L)
        tv_l = tv / 1000 if tv > 100 else tv
        rsbi = calculate_rsbi(rr, tv_l)

    # ── Post-extubation support: auto-flag HFNC vs NIV ────
    high_risk = any(k in diag for k in [
        "copd", "obstruct", "heart fail", "cardiac", "chf", "obes", "hypercapn"
    ])
    if any(p in raw for p in ["age > 65", "elderly", "hypercapnia during", "co2 rose"]):
        high_risk = True
    if paco2 and paco2 > 45:
        high_risk = True

    post_ext = (
        "Prophylactic NIV immediately post-extubation x24-48h "
        "(high-risk: COPD/HF/obesity/hypercapnia/age>65 — ATS/ACCP 2017, ESICM 2023). "
        "Settings: IPAP 12-16 / EPAP 4-8, FiO2 to maintain SpO2 92-96%"
        if high_risk else
        "HFNC 40-60 L/min post-extubation — low-to-moderate risk "
        "(ATS/ACCP 2017, ESICM 2023). FiO2 same as pre-extubation."
    )

    # ── Sub-scenario detection ─────────────────────────────
    # ── SBT failure keyword detection — negation-aware ────
    # Each keyword is checked individually so negation context
    # is assessed per-phrase, not across the whole raw input.
    # "patient has NOT failed the SBT" must NOT trigger sbt_fail_kw.
    _fail_keywords = [
        "failed sbt", "failed trial", "failed spontaneous",
        "sbt failed", "trial failed", "could not tolerate",
        "rr climbed", "rr increased", "desaturated",
        "spo2 dropped", "distress", "diaphoretic", "diaphoresis",
        "accessory muscle", "paradoxical", "agitated", "agitation",
        "altered mental", "increased work",
    ]
    sbt_fail_kw = any(
        p in raw and not _is_negated(raw, p)
        for p in _fail_keywords
    )

    # ── Full-support guard — prevents numeric thresholds from ──
    # firing as SBT failure when patient is not on an active trial
    _on_full_support = (
        (peep is not None and peep > 8) or
        (fio2 is not None and fio2 > 0.50)
    )
    sbt_fail_num = (not _on_full_support) and any([
        rr and rr > 35,
        spo2 and spo2 < 90,
        hr and hr > 140,
        ph and ph < 7.32,
        bp_sys and (bp_sys > 180 or bp_sys < 90),
    ])
    sbt_failed = sbt_fail_kw or sbt_fail_num

    # ── SBT passed keyword detection — negation-aware ─────
    # "has NOT passed the trial" must NOT trigger sbt_passed.
    _pass_keywords = [
        "passed sbt", "passed trial", "tolerated sbt", "tolerating",
        "sbt passed", "trial passed", "ready to extubate",
        "extubation ready", "completed sbt", "completed trial",
    ]
    # Also trust the structured sbt_status field extracted by gemini_handler.
    # This catches natural phrases like "tolerated the SBT well" that
    # keyword matching misses due to intervening words.
    _extracted_sbt = data.get("sbt_status", "")
    sbt_passed = (
        any(
            p in raw and not _is_negated(raw, p)
            for p in _pass_keywords
        ) or
        _extracted_sbt in ["completed", "passing"]
    )

    # Extracted "failing" status also reinforces keyword detection
    if _extracted_sbt == "failing" and not sbt_failed:
        sbt_failed = True

    # ── SUB-SCENARIO B — SBT FAILED ───────────────────────
    if sbt_failed:
        if (spo2 and spo2 < 88) or (ph and ph < 7.25):
            status = "Critical"
        else:
            status = "Worsening"

        abort = []
        if rr and rr > 35:
            abort.append("RR " + str(rr) + " > 35/min for > 5 min")
        elif rr and rr > 30:
            abort.append("RR " + str(rr) + " > 30/min (borderline — watch > 5 min)")
        if spo2 and spo2 < 90:
            abort.append("SpO2 " + str(spo2) + "% < 90%")
        if hr and hr > 140:
            abort.append("HR " + str(hr) + " > 140 bpm")
        if bp_sys and bp_sys > 180:
            abort.append("SBP " + str(bp_sys) + " > 180 mmHg")
        if bp_sys and bp_sys < 90:
            abort.append("SBP " + str(bp_sys) + " < 90 mmHg")
        if ph and ph < 7.32:
            abort.append("pH " + str(ph) + " < 7.32")
        if any(w in raw for w in ["accessory", "diaphoretic", "diaphoresis"]):
            abort.append("Accessory muscle use / diaphoresis")
        if any(w in raw for w in ["agitat", "altered mental"]):
            abort.append("Agitation / altered mental status")
        if "paradox" in raw:
            abort.append("Paradoxical breathing")
        if sbt_fail_kw and not abort:
            abort.append("Clinical signs reported by team")

        abort_str = " | ".join(abort) if abort else "Clinical deterioration during trial"
        physio = (
            "SBT FAILURE — Abort criteria met: " + abort_str + ". "
            "Return to full ventilatory support immediately (ATS/ACCP 2017)."
        )
        causes = (
            "Investigate failure cause: "
            "(1) Secretion burden — suction, assess cough; "
            "(2) Bronchospasm — bronchodilator; "
            "(3) Fluid overload — diuresis if CVP/BNP elevated; "
            "(4) Respiratory muscle weakness — nutrition, electrolytes; "
            "(5) Cardiac dysfunction — echo if new haemodynamic change; "
            "(6) Unresolved primary insult"
        )
        next_step = " | ".join([
            "IMMEDIATE: Return to full support — AC/VC or PSV 12-15 cmH2O + PEEP " + (str(peep) if peep else "5") + " cmH2O",
            "Allow minimum 24h recovery before next SBT (ATS/ACCP 2017)",
            causes,
            "Optimise: electrolytes (K >= 3.5, Mg >= 0.8, PO4 >= 0.8), nutrition 25-30 kcal/kg/day, upright 30-45 degrees",
            "Implement ABC bundle: daily SAT + SBT, early mobility, delirium screening (CAM-ICU)",
            "Repeat RSBI tomorrow — target < 105 before next SBT attempt",
        ])
        monitoring = (
            "ABG 30 min after returning to full support. SpO2 continuous — target 92-96%. "
            "Daily: RSBI, NIF (target < -25 cmH2O), cough assessment, secretion burden. "
            "Electrolytes q12h until optimised. Reassess SBT eligibility in 24h."
        )
        escalation = (
            "Failed SBT x2 without reversible cause -> tracheostomy discussion (ATS/ACCP 2017). "
            "RSBI > 105 persistently -> neuromuscular assessment (NIF, phrenic nerve studies). "
            "Refractory hypercapnia during SBT -> COPD pathway / ECCO2R referral. "
            "New haemodynamic instability -> echo, cardiology consult."
        )
        return {
            "ventilation_status":         status,
            "physiologic_interpretation": physio,
            "immediate_next_step":        next_step,
            "monitoring_and_safety":      monitoring,
            "escalation_criteria":        escalation,
            "driving_pressure":           dp_label,
            "pf_ratio":                   str(int(pf)) if pf else None,
            "rsbi":                       str(rsbi) if rsbi else None,
            "rsbi_relevant":              True,
            "tv_per_kg_ibw":              str(tv_per_kg) + " mL/kg IBW" if tv_per_kg else None,
            "map":                        str(map_val) if map_val else None,
            "trend_summary":              "SBT failed — " + abort_str[:60],
        }

    # ── SUB-SCENARIO C — SBT PASSED → Extubation ──────────
    elif sbt_passed:
        contra = []
        if gcs and gcs < 13:
            contra.append("GCS " + str(int(gcs)) + " < 13 — neurologic insufficiency")
        if fio2 and fio2 >= 0.60:
            contra.append("FiO2 " + str(fio2) + " >= 0.60")
        if peep and peep >= 10:
            contra.append("PEEP " + str(peep) + " >= 10 cmH2O")
        if bp_sys and (bp_sys < 90 or bp_sys > 160):
            contra.append("SBP " + str(bp_sys) + " outside 90-160 mmHg")
        if any(p in raw for p in ["residual paralysis", "block not reversed"]):
            contra.append("Residual NMB — confirm TOF > 90%")
        if any(p in raw for p in ["escalating vasopressor", "increasing norepi", "vasopressor up"]):
            contra.append("Vasopressor escalation — haemodynamic instability")

        needs_cuff_leak = any(p in raw for p in [
            "intubated > 6", "6 days", "7 days", "8 days",
            "long intubation", "stridor risk", "previous stridor",
        ])
        weak_cough = any(p in raw for p in [
            "weak cough", "poor cough", "no cough", "unable to cough", "pcef < 60",
        ])
        good_cough = any(p in raw for p in [
            "strong cough", "good cough", "cough adequate", "pcef > 60",
        ])
        high_secretions = any(p in raw for p in [
            "suctioning q1h", "suctioning q2h", "frequent suction",
            "thick secretions", "heavy secretions",
        ])

        if contra:
            status     = "Worsening"
            contra_str = " | ".join(contra)
            physio     = (
                "SBT PASSED — but extubation CONTRAINDICATED: " + contra_str + ". "
                "Do not extubate until contraindications resolved (AARC 2024)."
            )
            next_step  = " | ".join([
                "Hold extubation — resolve: " + contra_str,
                "Continue ventilatory support at current settings",
                "Reassess daily — address each contraindication",
                "Implement ABC bundle to prevent deconditioning",
            ])
            escalation = (
                "Senior review if contraindications persist > 24h. "
                "GCS < 13 persisting -> neurology consult. "
                "Haemodynamic instability -> echo + vasopressor optimisation."
            )
        else:
            status      = "Stable"
            phys_parts  = ["SBT PASSED — extubation criteria review (ATS/ACCP 2017, AARC 2024)"]
            if rsbi:
                phys_parts.append("RSBI = " + str(rsbi) + (" — favourable" if rsbi < 105 else " — elevated, review carefully"))
            if gcs:
                phys_parts.append("GCS = " + str(int(gcs)) + (" — adequate" if gcs >= 13 else " — BORDERLINE"))
            if weak_cough:
                phys_parts.append("Weak cough — extubation failure risk increased (PCEF target > 60 L/min)")
            elif good_cough:
                phys_parts.append("Adequate cough strength — favourable")
            if high_secretions:
                phys_parts.append("High secretion burden — suctioning > q2h is relative contraindication")
            if needs_cuff_leak:
                phys_parts.append("Intubated > 6 days — cuff leak test required (AARC 2024)")
            physio = ". ".join(phys_parts)

            steps = [
                "Proceed with extubation — criteria met",
                "Pre-extubation checklist: airway equipment at bedside, team present, patient upright 45 degrees",
            ]
            if needs_cuff_leak:
                steps.append(
                    "CUFF LEAK TEST required: deflate cuff — adequate if leak > 110 mL or > 10-12% of TV. "
                    "Postpone if no leak — post-extubation stridor risk"
                )
            if weak_cough or high_secretions:
                steps.append("HIGH FAILURE RISK: weak cough or high secretions — experienced reintubation team at bedside")
            steps.append("Immediately post-extubation: " + post_ext)
            if high_risk:
                steps.append("Monitor 1h post-extubation before reducing surveillance — NIV available at bedside")
            steps.append("Reintubation threshold: SpO2 < 90%, RR > 35, GCS drop > 2, haemodynamic instability")
            next_step  = " | ".join(steps)
            escalation = (
                "Reintubate if: SpO2 < 90% despite HFNC/NIV, RR > 35 sustained, "
                "GCS drop, haemodynamic instability, stridor with respiratory distress. "
                "Post-extubation stridor -> nebulised epinephrine + IV dexamethasone; reintubate if unresponsive. "
                "Failed extubation x1 -> reassess for tracheostomy (ATS/ACCP 2017)."
            )

        monitoring = (
            "q5 min for first 30 min post-extubation: RR, SpO2, HR, BP, WOB, phonation. "
            "q15 min for next 2h. ABG at 1h if high-risk. "
            "SpO2 target 92-96% (88-92% if COPD). "
            "Daily: swallowing assessment, NIV weaning if applicable."
        )
        return {
            "ventilation_status":         status,
            "physiologic_interpretation": physio,
            "immediate_next_step":        next_step,
            "monitoring_and_safety":      monitoring,
            "escalation_criteria":        escalation,
            "driving_pressure":           dp_label,
            "pf_ratio":                   str(int(pf)) if pf else None,
            "rsbi":                       str(rsbi) if rsbi else None,
            "rsbi_relevant":              True,
            "tv_per_kg_ibw":              str(tv_per_kg) + " mL/kg IBW" if tv_per_kg else None,
            "map":                        str(map_val) if map_val else None,
            "trend_summary":              "SBT passed — " + ("high-risk" if high_risk else "standard-risk") + " extubation",
        }

    # ── SUB-SCENARIO A — Readiness Screening ──────────────
    else:
        met, failed, warn = [], [], []

        if fio2 is not None:
            if fio2 <= 0.40:   met.append("FiO2 " + str(fio2) + " <= 0.40")
            elif fio2 <= 0.50: warn.append("FiO2 " + str(fio2) + " = 0.41-0.50 (borderline — acceptable AARC 2024)")
            else:              failed.append("FiO2 " + str(fio2) + " > 0.50")

        if peep is not None:
            if peep <= 5:   met.append("PEEP " + str(peep) + " <= 5 cmH2O")
            elif peep <= 8: warn.append("PEEP " + str(peep) + " = 6-8 cmH2O (borderline — acceptable AARC 2024)")
            else:           failed.append("PEEP " + str(peep) + " > 8 cmH2O")

        if ph is not None:
            if 7.35 <= ph <= 7.50:    met.append("pH " + str(ph) + " acceptable")
            elif 7.30 <= ph < 7.35:   warn.append("pH " + str(ph) + " mildly acidotic — monitor closely")
            else:                     failed.append("pH " + str(ph) + " outside acceptable range")

        if pao2 is not None:
            if pao2 >= 60: met.append("PaO2 " + str(pao2) + " >= 60 mmHg")
            else:          failed.append("PaO2 " + str(pao2) + " < 60 mmHg")

        if bp_sys is not None:
            if 90 <= bp_sys <= 160: met.append("SBP " + str(bp_sys) + " within 90-160 mmHg")
            else:                   failed.append("SBP " + str(bp_sys) + " outside 90-160 mmHg")

        if hr is not None:
            if hr <= 140: met.append("HR " + str(hr) + " <= 140 bpm")
            else:         failed.append("HR " + str(hr) + " > 140 bpm")

        if gcs is not None:
            if gcs >= 13: met.append("GCS " + str(int(gcs)) + " >= 13 — follows commands")
            else:         failed.append("GCS " + str(int(gcs)) + " < 13 — neurologic insufficiency")

        if rsbi is not None:
            if rsbi < 80:    met.append("RSBI " + str(rsbi) + " < 80 — strongly favourable")
            elif rsbi < 105: warn.append("RSBI " + str(rsbi) + " = 80-104 — favourable but borderline")
            else:            failed.append("RSBI " + str(rsbi) + " >= 105 — predicts SBT failure")

        if any(p in raw for p in ["tof > 90", "tof 90", "reversed", "sugammadex given"]):
            met.append("NMB reversed — TOF > 90%")
        elif any(p in raw for p in ["nmb", "paralysed", "tof", "neuromuscular block"]):
            warn.append("NMB mentioned — confirm TOF > 90% before SBT (AARC 2024)")

        if any(p in raw for p in ["norepi < 8", "low dose norepi", "minimal vasopressor", "vasopressor weaned"]):
            met.append("Vasopressors minimal — acceptable")
        elif any(p in raw for p in ["vasopressor", "norepinephrine", "norepi", "vasopressin", "phenylephrine"]):
            warn.append("Vasopressors in use — verify dose < 8 mcg/min norepinephrine equivalent (ATS/ACCP 2017)")

        core_ready = (
            (fio2 is None or fio2 <= 0.50) and
            (peep is None or peep <= 8) and
            (ph is None or ph >= 7.30) and
            (pao2 is None or pao2 >= 60) and
            (bp_sys is None or 90 <= bp_sys <= 160) and
            (hr is None or hr <= 140) and
            (gcs is None or gcs >= 13) and
            (rsbi is None or rsbi < 105) and
            not failed
        )

        phys_parts = ["Weaning Readiness Screening (ATS/ACCP 2017, AARC 2024)"]
        if met:    phys_parts.append("Met: " + " | ".join(met))
        if warn:   phys_parts.append("Borderline: " + " | ".join(warn))
        if failed: phys_parts.append("Not met: " + " | ".join(failed))
        physio = ". ".join(phys_parts)

        if core_ready:
            if dp is not None and dp >= 15:
                status = "Worsening"
                steps = [
                    "dP = " + str(dp) + " cmH2O >= 15 — REDUCE TV by 1 mL/kg IBW steps before proceeding. "
                    "Do NOT start SBT until dP < 15 cmH2O (ATS 2024 / ESICM 2023).",
                    "SBT prerequisites otherwise met — correct dP and reassess in 2-4h",
                    "Once dP < 15: proceed with SBT — PSV 5-8 cmH2O + PEEP 5 cmH2O",
                ]
            else:
                status = "Stable"
                steps = [                                 # ← only runs when dP is safe
                    "Patient MEETS SBT prerequisites — proceed with SBT now (ATS/ACCP 2017)",
                    "SBT method: PSV 5-8 cmH2O + PEEP 5 cmH2O (preferred) OR T-piece OR CPAP 0-5 cmH2O",
                    "Duration: 30-120 min — do NOT increase FiO2 during trial",
                    "Monitor q5 min: RR, SpO2, HR, BP, work of breathing",
                    "Abort if: RR > 35/min for > 5 min, SpO2 < 90%, HR > 140 or +20% sustained, SBP > 180 or < 90, pH < 7.32, agitation or diaphoresis",
                    "If tolerated: assess extubation — RSBI, cough strength (PCEF > 60 L/min), secretions < q2h, GCS >= 13",
                ]
            if warn:
                steps.insert(1, "Note borderline criteria: " + " | ".join(warn) + " — proceed with caution")
        else:
            status      = "Worsening"
            failed_str  = " | ".join(failed) if failed else "borderline criteria unresolved"
            steps       = [
                "NOT ready for SBT — unmet criteria: " + failed_str,
                "Implement ABC bundle: daily SAT + SBT coordination, early mobility, delirium screening (CAM-ICU)",
                "Optimise nutrition: 25-30 kcal/kg/day, protein 1.2-2.0 g/kg/day",
                "Correct electrolytes: K >= 3.5, Mg >= 0.8, PO4 >= 0.8 mEq/L",
                "Minimise sedation — target RASS -1 to 0",
                "Reassess readiness every 24h",
            ]
            if gcs and gcs < 13:
                steps.append("GCS < 13 — neurology review; assess airway protection capacity")
            if rsbi and rsbi >= 105:
                steps.append("RSBI >= 105 — check NIF (target < -25 cmH2O), diaphragm ultrasound for muscle weakness")

        next_step  = " | ".join(steps)
        monitoring = (
            "Daily readiness screen: FiO2, PEEP, pH, SpO2, HR, BP, GCS, RSBI, NIF. "
            "During SBT: q5 min — RR, SpO2, HR, BP, WOB. "
            "If SBT aborted: ABG within 30 min, return to full support, reassess in 24h. "
            "SpO2 target 92-96% (88-92% if COPD). "
            "Secretion burden: document suction frequency daily."
        )
        escalation = (
            "RSBI > 105 persistently -> neuromuscular assessment. "
            "FiO2 > 0.50 persisting -> investigate (infection, fluid, pneumothorax). "
            "Failed SBT x2 -> senior review + tracheostomy discussion. "
            "GCS declining -> neurology consult. "
            "Vasopressors escalating -> haemodynamic optimisation before weaning attempt."
        )
        return {
            "ventilation_status":         status,
            "physiologic_interpretation": physio,
            "immediate_next_step":        next_step,
            "monitoring_and_safety":      monitoring,
            "escalation_criteria":        escalation,
            "driving_pressure":           dp_label,
            "pf_ratio":                   str(int(pf)) if pf else None,
            "rsbi":                       str(rsbi) if rsbi else None,
            "rsbi_relevant":              True,
            "tv_per_kg_ibw":              str(tv_per_kg) + " mL/kg IBW" if tv_per_kg else None,
            "map":                        str(map_val) if map_val else None,
            "trend_summary":              "Weaning readiness — " + ("ready for SBT" if core_ready else "criteria not met"),
        }


# ════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════
# COPD + ARDS OVERLAP BRANCH
# Primary framework: ARDS LPV (ATS 2024 / ESICM 2023)
# Mandatory COPD constraints layered on top (GOLD 2024):
#   - SpO2 target 88-92% (Haldane effect still applies)
#   - RR ceiling 12/min (auto-PEEP risk)
#   - Auto-PEEP monitoring mandatory
#   - Baseline PaCO2 protection
#   - No rapid PaCO2 correction (> 20 mmHg or > 50% in 24h)
# ════════════════════════════════════════════════════════════

def _copd_ards_overlap_branch(diagnosis, data, ibw_kg, trend, dp_label,
                               pf, pf_from_sf, sf_label, dp,
                               tv_per_kg, rsbi, map_val, status):
    fio2           = _f(data.get("fio2"))
    peep           = _f(data.get("peep"))
    ph             = _f(data.get("ph"))
    paco2          = _f(data.get("paco2"))
    spo2           = _f(data.get("spo2"))
    rr             = _f(data.get("rr"))
    tv             = _f(data.get("tv"))
    pplat          = _f(data.get("pplat"))
    auto_peep      = _f(data.get("auto_peep"))
    bp             = data.get("bp", "")
    bp_sys         = _f(data.get("bp_sys")) or _bp_sys(bp)
    bp_dia         = _f(data.get("bp_dia")) or _bp_dia(bp)
    baseline_paco2 = _f(data.get("baseline_paco2"))
    prior_paco2    = _f(data.get("prior_paco2"))
    tv_target_ml   = round(6 * ibw_kg, 0) if ibw_kg else None

    # ── ARDS severity classification ───────────────────────
    peep_ok = peep and peep >= 5
    if pf and peep_ok:
        if pf <= 100:   severity, severity_tag = "Severe (P/F <= 100, PEEP >= 5)", "severe"
        elif pf <= 200: severity, severity_tag = "Moderate (100 < P/F <= 200, PEEP >= 5)", "moderate"
        elif pf <= 300: severity, severity_tag = "Mild (200 < P/F <= 300, PEEP >= 5)", "mild"
        else:           severity, severity_tag = "Outside Berlin criteria (P/F > 300)", "none"
    elif pf and not peep_ok:
        severity, severity_tag = "P/F = " + str(int(pf)) + " but PEEP < 5 — Berlin criteria NOT met", "unclassified"
    else:
        severity, severity_tag = "Unclassified — P/F not calculable", "unclassified"

    # ── COPD constraint 1: FiO2 titration to SpO2 88-92% ──
    if spo2 and fio2:
        if spo2 > 92:
            if fio2 <= 0.21:
                fio2_step = (
                    "COPD+ARDS FiO2 CONSTRAINT: SpO2 " + str(spo2) +
                    "% > 92% ceiling — FiO2 at 0.21 (cannot reduce further). "
                    "Verify SpO2 probe. Target SpO2 88-92% (GOLD 2024)."
                )
            else:
                new_fio2 = round(max(0.21, fio2 - 0.05), 2)
                fio2_step = (
                    "COPD+ARDS FiO2 CONSTRAINT: SpO2 " + str(spo2) +
                    "% EXCEEDS 92% ceiling — REDUCE FiO2 from " +
                    str(fio2) + " to " + str(new_fio2) + " now. "
                    "Target SpO2 88-92% (NOT 95% — Haldane effect and "
                    "hypoxic drive suppression still apply in COPD+ARDS). "
                    "Reduce by 0.05 every 15 min. Recheck ABG after each step (GOLD 2024)."
                )
        elif spo2 < 88:
            if fio2 >= 1.0:
                fio2_step = (
                    "COPD+ARDS FiO2 CONSTRAINT: SpO2 " + str(spo2) +
                    "% < 88% floor — FiO2 at 1.0 (maximum). "
                    "ABG stat. Senior review NOW. "
                    "Investigate: ETT malposition, secretions, bronchospasm, "
                    "auto-PEEP worsening (GOLD 2024)."
                )
            else:
                new_fio2 = round(min(1.0, fio2 + 0.05), 2)
                fio2_step = (
                    "COPD+ARDS FiO2 CONSTRAINT: SpO2 " + str(spo2) +
                    "% < 88% floor — INCREASE FiO2 from " +
                    str(fio2) + " to " + str(new_fio2) + " as bridge. "
                    "STOP increasing once SpO2 reaches 88% — do NOT target 95%. "
                    "Haldane effect still applies in COPD+ARDS (GOLD 2024)."
                )
        else:
            fio2_step = (
                "COPD+ARDS FiO2 CONSTRAINT: SpO2 " + str(spo2) +
                "% within 88-92% target. Hold FiO2 " + str(fio2) +
                ". Do NOT increase further — Haldane effect applies (GOLD 2024)."
            )
    else:
        fio2_step = (
            "COPD+ARDS FiO2 CONSTRAINT: SpO2 and/or FiO2 not reported. "
            "Attach pulse oximetry. Target SpO2 88-92% (NOT 95%) in COPD+ARDS (GOLD 2024)."
        )

    # ── COPD constraint 2: RR ceiling 12/min ──────────────
    rr_warning = None
    if rr and rr > 12:
        rr_warning = (
            "COPD+ARDS RR CONSTRAINT: RR " + str(int(rr)) +
            "/min EXCEEDS 12/min ceiling — REDUCE to 12/min immediately. "
            "In COPD+ARDS, RR > 12/min shortens expiratory time, "
            "worsening auto-PEEP and dynamic hyperinflation even without "
            "measured auto-PEEP. Permissive hypercapnia is accepted — "
            "do NOT increase RR above 12/min to correct PaCO2 "
            "(GOLD 2024 / Expert Consensus)."
        )

    # ── COPD constraint 3: Auto-PEEP management ───────────
    auto_peep_warning = None
    if auto_peep and auto_peep >= 5:
        rec_ext_peep = min(round(auto_peep * 0.75, 1), auto_peep)
        auto_peep_warning = (
            "COPD+ARDS AUTO-PEEP ALERT: auto-PEEP = " + str(auto_peep) +
            " cmH2O detected. Reduce RR to 12/min (COPD ceiling). "
            "Set external PEEP to " + str(rec_ext_peep) + " cmH2O "
            "(= 75% of measured auto-PEEP — HARD RULE: external PEEP must "
            "NEVER exceed measured auto-PEEP, GOLD 2024). "
            "Expiratory hold after every RR change. "
            "ARDSNet PEEP targets MUST be balanced against auto-PEEP risk — "
            "do NOT blindly apply Higher PEEP table while auto-PEEP is present."
        )

    # ── COPD constraint 4: Baseline PaCO2 protection ──────
    baseline_paco2_warning = None
    if baseline_paco2 and paco2:
        if paco2 < (baseline_paco2 - 10):
            drop = round(baseline_paco2 - paco2, 1)
            baseline_paco2_warning = (
                "COPD+ARDS BASELINE PaCO2 WARNING: "
                "Current PaCO2 = " + str(paco2) + " mmHg — "
                "BELOW patient baseline of " + str(baseline_paco2) + " mmHg "
                "(drop of " + str(drop) + " mmHg). "
                "Post-hypercapnic metabolic alkalosis risk. "
                "Target PaCO2 >= " + str(baseline_paco2) + " mmHg. "
                "Reduce MV: decrease RR by 2/min (stay at or below 12/min ceiling) "
                "or reduce TV by 1 mL/kg step. Recheck ABG in 30 min (GOLD 2024)."
            )
        elif paco2 < baseline_paco2:
            baseline_paco2_warning = (
                "COPD+ARDS BASELINE PaCO2 ADVISORY: "
                "PaCO2 " + str(paco2) + " mmHg — approaching baseline floor of " +
                str(baseline_paco2) + " mmHg. "
                "Do NOT reduce PaCO2 further below baseline (GOLD 2024)."
            )

    # ── COPD constraint 5: PaCO2 rapid-correction guard ───
    paco2_delta_warning = None
    if prior_paco2 and paco2:
        paco2_drop     = round(prior_paco2 - paco2, 1)
        paco2_drop_pct = round((paco2_drop / prior_paco2) * 100, 1) if prior_paco2 > 0 else 0
        if paco2_drop > 0:
            if paco2_drop > 20 and paco2_drop_pct > 50:
                paco2_delta_warning = (
                    "COPD+ARDS DANGER — PaCO2 RAPID CORRECTION: "
                    "PaCO2 dropped " + str(paco2_drop) + " mmHg "
                    "(" + str(paco2_drop_pct) + "%) from " +
                    str(prior_paco2) + " to " + str(paco2) + " mmHg. "
                    "BOTH thresholds breached (> 20 mmHg AND > 50%). "
                    "Risk of intracranial haemorrhage. "
                    "Reduce MV immediately. Neurology review if any neurologic change "
                    "(GOLD 2024 / Expert Consensus)."
                )
            elif paco2_drop > 20:
                paco2_delta_warning = (
                    "COPD+ARDS WARNING — PaCO2 dropped " + str(paco2_drop) +
                    " mmHg from prior " + str(prior_paco2) + " mmHg. "
                    "Exceeds 20 mmHg safe limit within 24h. "
                    "Slow correction rate (GOLD 2024)."
                )
            elif paco2_drop_pct > 50:
                paco2_delta_warning = (
                    "COPD+ARDS WARNING — PaCO2 dropped " + str(paco2_drop_pct) +
                    "% from prior " + str(prior_paco2) + " mmHg. "
                    "Exceeds 50% safe limit within 24h. "
                    "Slow correction rate (GOLD 2024)."
                )

    # ── Physio narrative ───────────────────────────────────
    parts = [
        "COPD + ARDS OVERLAP — Berlin ARDS: " + severity +
        " | ARDS LPV framework WITH mandatory COPD constraints "
        "(ATS 2024 / ESICM 2023 / GOLD 2024)"
    ]
    if pf and not pf_from_sf:
        parts.append("P/F ratio = " + str(int(pf)))
    elif pf_from_sf and sf_label:
        parts.append(sf_label)
    if tv_per_kg:
        tv_label = ("optimal (6 mL/kg target)" if tv_per_kg <= 6.0
                    else "acceptable (4-8 mL/kg range)" if tv_per_kg <= 8.0
                    else "EXCEEDS 8 mL/kg absolute maximum")
        parts.append("TV = " + str(tv_per_kg) + " mL/kg IBW (" + tv_label + ")")
    if dp is not None:
        if dp >= 20:   parts.append("DANGEROUS dP = " + str(dp) + " cmH2O — immediate action required")
        elif dp >= 15: parts.append("Elevated dP = " + str(dp) + " cmH2O (limit: < 15 cmH2O, ATS 2024)")
        else:          parts.append("dP = " + str(dp) + " cmH2O — within target (< 15 cmH2O)")
    if pplat and pplat > 30:
        parts.append("Pplat " + str(pplat) + " cmH2O EXCEEDS 30 cmH2O absolute limit")
    if auto_peep and auto_peep >= 5:
        parts.append(
            "Auto-PEEP = " + str(auto_peep) + " cmH2O — dynamic hyperinflation present. "
            "ARDSNet PEEP targets must be balanced against auto-PEEP risk"
        )
    if rr and rr > 12:
        parts.append(
            "RR " + str(int(rr)) + "/min EXCEEDS 12/min COPD ceiling — "
            "worsening air trapping risk even in ARDS context"
        )
    if ph and ph < 7.35 and paco2:
        parts.append(
            "Permissive hypercapnia in COPD+ARDS: pH " + str(ph) +
            " | PaCO2 " + str(paco2) + " mmHg. "
            "Target patient baseline PaCO2 — NOT normocapnia. "
            "pH >= 7.25 preferred minimum; >= 7.20 absolute floor. "
            "Do NOT increase RR above 12/min to correct PaCO2 (GOLD 2024 / ATS 2024)."
        )
    physio = ". ".join(parts)

    # ── Steps ──────────────────────────────────────────────
    steps = []

    # COPD safety constraints first — these override ARDS defaults
    if rr_warning:
        steps.append(rr_warning)
    if auto_peep_warning:
        steps.append(auto_peep_warning)

    # ARDS LPV framework
    if pplat and pplat > 30:
        steps.append(
            "ABSOLUTE LIMIT: Pplat " + str(pplat) +
            " > 30 cmH2O — reduce TV by 1 mL/kg steps until Pplat <= 30. "
            "Minimum TV = 4 mL/kg IBW (ATS 2024)"
        )
    if tv_per_kg and tv_per_kg > 8:
        steps.append(
            "URGENT: TV " + str(tv_per_kg) + " mL/kg EXCEEDS 8 mL/kg absolute maximum. "
            "Reduce to " + str(int(tv_target_ml)) + " mL immediately (6 mL/kg IBW)"
        )
    elif ibw_kg and tv_target_ml:
        steps.append(
            "TV target = " + str(int(tv_target_ml)) +
            " mL (6 mL/kg x " + str(ibw_kg) + " kg IBW). Range 4-8 mL/kg (ATS 2024)"
        )
    if dp and dp >= 15:
        steps.append(
            "dP = " + str(dp) + " cmH2O >= 15 — reduce TV by 1 mL/kg steps. "
            "Target dP < 15 cmH2O (ATS 2024 / ESICM 2023)"
        )

    # ARDSNet PEEP table with COPD auto-PEEP caveat
    if severity_tag in ["moderate", "severe", "mild"] and fio2:
        peep_lookup = lookup_ardsnet_peep(fio2, severity_tag)
        if peep_lookup:
            rec_peep  = peep_lookup["recommended_peep"]
            rec_table = peep_lookup["recommended_table"]
            rationale = peep_lookup["rationale"]
            if auto_peep and auto_peep >= 5:
                steps.append(
                    "ARDSNet PEEP TABLE (" + rec_table + "): recommended PEEP = " +
                    str(rec_peep) + " cmH2O for FiO2 = " + str(fio2) + ". "
                    "AUTO-PEEP OVERRIDE: auto-PEEP = " + str(auto_peep) +
                    " cmH2O present — do NOT increase PEEP per ARDSNet table until "
                    "auto-PEEP is controlled. Increasing PEEP with active auto-PEEP "
                    "worsens hyperinflation. Reduce RR to 12/min first, "
                    "recheck expiratory hold, then reassess PEEP titration (GOLD 2024)."
                )
            elif peep is not None and peep != rec_peep:
                steps.append(
                    "ARDSNet PEEP TABLE (" + rec_table + "): for FiO2 = " + str(fio2) +
                    " → recommended PEEP = " + str(rec_peep) + " cmH2O "
                    "(current = " + str(peep) + " cmH2O). " + rationale +
                    " After change: recheck dP, Pplat, hemodynamics within 15 min. "
                    "Perform expiratory hold to confirm no auto-PEEP developing "
                    "(mandatory COPD+ARDS constraint)."
                )
            else:
                steps.append(
                    "ARDSNet PEEP TABLE (" + rec_table + "): current PEEP = " +
                    str(peep) + " cmH2O matches recommended for FiO2 = " +
                    str(fio2) + ". " + rationale
                )

    # Prone positioning (ARDS threshold, unchanged)
    if pf and pf < 150:
        steps.append(
            "Prone positioning indicated — P/F = " + str(int(pf)) +
            " < 150 mmHg. Target >= 16h/day (ESICM 2023 / PROSEVA). "
            "Initiate within 36h of ARDS diagnosis (ATS 2024)."
        )

    # NMBA (ARDS threshold, unchanged)
    if pf and pf < 100:
        steps.append(
            "NMBA INDICATION: P/F = " + str(int(pf)) +
            " < 100 — short-term cisatracurium if within 48h of ARDS diagnosis. "
            "Maximum 48h. Confirm with senior (ATS 2024 / ESICM 2023)."
        )

    # COPD FiO2 constraint (replaces standard ARDS 88-95% target)
    steps.append(fio2_step)

    # pH management — COPD RR ceiling enforced
    if ph and ph < 7.20:
        steps.append(
            "CRITICAL pH " + str(ph) + " < 7.20 — ABSOLUTE FLOOR BREACHED. "
            "Increase RR to 12/min MAXIMUM (COPD ceiling — cannot exceed). "
            "If RR already at 12/min: IV sodium bicarbonate + "
            "ECCO2R/VV-ECMO referral immediately. "
            "Do NOT increase TV above 8 mL/kg IBW (ATS 2024 / GOLD 2024)."
        )
    elif ph and ph < 7.25:
        steps.append(
            "pH " + str(ph) + " in 7.20-7.25 danger zone. "
            "Increase RR to 12/min MAXIMUM (COPD ceiling). "
            "If already at 12/min: prepare sodium bicarbonate. "
            "Recheck ABG in 30 min. Do NOT increase TV (ATS 2024 / GOLD 2024)."
        )

    # Haemodynamic guard
    if bp_sys and bp_sys < 90:
        steps.append(
            "HAEMODYNAMIC ALERT: SBP " + str(bp_sys) +
            " < 90 — HIGH PEEP CONTRAINDICATED. "
            "Fluid 500 mL. Vasopressors if MAP < 65. "
            "If auto-PEEP suspected: disconnect ventilator 30-60s (GOLD 2024)."
        )

    if not steps:
        steps.append(
            "COPD+ARDS overlap: settings within targets — "
            "TV 6 mL/kg IBW, Pplat <= 30 cmH2O, dP < 15 cmH2O. "
            "SpO2 target 88-92% (NOT 95%). "
            "Expiratory hold q2h to monitor for auto-PEEP development "
            "(GOLD 2024 / ATS 2024)."
        )

    # Prepend COPD safety guards at top of step list
    if baseline_paco2_warning:
        steps.insert(0, baseline_paco2_warning)
    if paco2_delta_warning:
        steps.insert(0, paco2_delta_warning)

    next_step = " | ".join(steps)

    monitoring = (
        "ABG 30-60 min after ANY vent change. "
        "Expiratory hold q2h — mandatory for COPD+ARDS to detect auto-PEEP. "
        "SpO2 target 88-92% (NOT 95%). "
        "Pplat + dP after every TV adjustment. "
        "Hemodynamics q1h. Daily CXR. " +
        ("Prone monitoring: SpO2 continuous, ETT position q2h, "
         "pressure injury assessment q2h. " if pf and pf < 150 else "")
    )

    escalation = (
        "P/F < 150 despite LPV -> prone positioning (PROSEVA / ATS 2024). "
        "VV-ECMO/ECCO2R referral (EOLIA / GOLD 2024): "
        "P/F < 50 persistent > 3h, OR P/F < 80 persistent > 6h, OR "
        "pH < 7.25 + PaCO2 >= 60 persistent > 6h. "
        "Auto-PEEP > 10 with haemodynamic instability -> "
        "disconnect ventilator 30-60s. "
        "pH < 7.20 with RR at 12/min ceiling -> bicarbonate + ECCO2R/ECMO — "
        "RR cannot be increased above 12/min in COPD+ARDS. "
        "Do NOT reduce PaCO2 > 20 mmHg or > 50% within 24h "
        "(intracranial haemorrhage risk — GOLD 2024)."
    )

    return {
        "ventilation_status":         status,
        "physiologic_interpretation": physio,
        "immediate_next_step":        next_step,
        "monitoring_and_safety":      monitoring,
        "escalation_criteria":        escalation,
        "driving_pressure":           dp_label,
        "pf_ratio":                   str(int(pf)) if pf else None,
        "rsbi":                       str(rsbi) if rsbi else None,
        "rsbi_relevant":              False,
        "tv_per_kg_ibw":              str(tv_per_kg) + " mL/kg IBW" if tv_per_kg else None,
        "map":                        str(map_val) if map_val else None,
        "trend_summary":              "COPD+ARDS overlap — " + severity,
    }

def generate_sccm_recommendation(diagnosis, data, ibw_kg=None, trend=None):
    if trend is None:
        trend = {}

    peep      = _f(data.get("peep"))
    pplat     = _f(data.get("pplat"))
    tv        = _f(data.get("tv"))
    fio2      = _f(data.get("fio2"))
    rr        = _f(data.get("rr"))
    ph        = _f(data.get("ph"))
    paco2     = _f(data.get("paco2"))
    pao2      = _f(data.get("pao2"))
    bp        = data.get("bp", "")
    hr        = _f(data.get("hr"))
    map_val   = _f(data.get("map"))
    auto_peep = _f(data.get("auto_peep"))
    ppeak     = _f(data.get("ppeak"))
    spo2      = _f(data.get("spo2"))
    insp_flow = _f(data.get("insp_flow"))


    bp_sys = _f(data.get("bp_sys")) or _bp_sys(bp)
    bp_dia = _f(data.get("bp_dia")) or _bp_dia(bp)

    dp = calculate_driving_pressure(pplat, peep)
    pf = calculate_pf_ratio(pao2, fio2)
    # If PaO2/FiO2 not available but doctor stated P/F directly — use it
    if pf is None:
        pf_stated = _f(data.get("pf_ratio_stated"))
        if pf_stated and 50 <= pf_stated <= 500:
            pf = pf_stated

    # S/F ratio surrogate — only when P/F still not available
    sf             = None
    sf_label       = None
    pf_from_sf     = False
    if pf is None and spo2 and fio2:
        sf = calculate_sf_ratio(spo2, fio2)
        if sf is not None:
            pf_from_sf = True
            if sf < 89:
                sf_label = (
                    "S/F = " + str(int(sf)) +
                    " < 89 — surrogate for P/F < 100 (severe ARDS range). "
                    "ABG required to confirm."
                )
                pf = 85    # conservative midpoint estimate for severe range
            elif sf < 150:
                sf_label = (
                    "S/F = " + str(int(sf)) +
                    " < 150 — surrogate for P/F < 150 (prone threshold range). "
                    "ABG required to confirm."
                )
                pf = 130   # conservative midpoint estimate
            elif sf < 235:
                sf_label = (
                    "S/F = " + str(int(sf)) +
                    " < 235 — surrogate for P/F < 200 (moderate ARDS range). "
                    "ABG required to confirm."
                )
                pf = 180   # conservative midpoint estimate

    tv_per_kg    = None
    tv_target_ml = None
    if tv and ibw_kg:
        tv_per_kg    = calculate_tv_per_kg(tv, ibw_kg)
        tv_target_ml = round(6 * ibw_kg, 0)

    rsbi = None
    if rr and tv:
        tv_l = tv / 1000 if tv > 100 else tv
        rsbi = calculate_rsbi(rr, tv_l)

    if not map_val and bp_sys and bp_dia:
        map_val = calculate_map(bp_sys, bp_dia)

    status = assess_ventilation_status({**data, "bp_sys": bp_sys}, trend)
    diag = diagnosis.lower()

    # Strip patient name prefix before branch matching
    # Format is either "Name — DIAGNOSIS" or just "DIAGNOSIS"
    # Using only the segment after " — " prevents patient names
    # from accidentally matching clinical branch keywords
    _diag_seg = diag.split(" — ")[-1].strip() if " — " in diag else diag.strip()

    is_overlap = "copd+ards" in _diag_seg or (
        ("copd" in _diag_seg or "obstruct" in _diag_seg or "hypercapn" in _diag_seg) and
        ("ards" in _diag_seg or "hypoxem" in _diag_seg)
    )


    # ── Trend summary ──────────────────────────────────────
    trend_notes = []
    if trend.get("pao2_trend") == "worsening":
        trend_notes.append("PaO2 falling " + str(abs(trend.get("pao2_delta", 0))) + " mmHg")
    elif trend.get("pao2_trend") == "improving":
        trend_notes.append("PaO2 improving +" + str(trend.get("pao2_delta", 0)) + " mmHg")
    if trend.get("ph_trend") == "worsening":
        trend_notes.append("pH trending down")
    elif trend.get("ph_trend") == "improving":
        trend_notes.append("pH trending up")
    if trend.get("peep_changed"):
        trend_notes.append("PEEP recently " + trend.get("peep_direction", "changed"))
    trend_str = " | ".join(trend_notes)

    # ── Driving pressure label ─────────────────────────────
    if dp is not None:
        if dp >= 20:   dp_label = str(dp) + " cmH2O (DANGEROUS — reduce immediately)"
        elif dp >= 15: dp_label = str(dp) + " cmH2O (ELEVATED — target < 15 cmH2O, ATS 2024 / ESICM 2023)"
        elif dp >= 13: dp_label = str(dp) + " cmH2O (within target < 15 cmH2O — monitor after every vent change)"
        else:          dp_label = str(dp) + " cmH2O (within target < 15 cmH2O)"
    else:
        dp_label = "Not calculable — provide Pplat and PEEP"

    # ════════════════════════════════════════════════════════
    # BRANCH 0 — COPD + ARDS OVERLAP
    # Sources: ATS 2024, ESICM 2023, GOLD 2024
    # ════════════════════════════════════════════════════════
    if is_overlap:
        return _copd_ards_overlap_branch(
            diagnosis, data, ibw_kg, trend, dp_label,
            pf, pf_from_sf, sf_label, dp,
            tv_per_kg, rsbi, map_val, status
        )

    # ════════════════════════════════════════════════════════
    # BRANCH 1 — ARDS / Hypoxemic Respiratory Failure
    # Sources: ATS 2024, ESICM 2023, EOLIA
    # ════════════════════════════════════════════════════════
    if "ards" in _diag_seg or "hypoxem" in _diag_seg:

        peep_ok = peep and peep >= 5
        if pf and peep_ok:
            if pf <= 100:   severity, severity_tag = "Severe (P/F <= 100, PEEP >= 5)", "severe"
            elif pf <= 200: severity, severity_tag = "Moderate (100 < P/F <= 200, PEEP >= 5)", "moderate"
            elif pf <= 300: severity, severity_tag = "Mild (200 < P/F <= 300, PEEP >= 5)", "mild"
            else:           severity, severity_tag = "Outside Berlin criteria (P/F > 300)", "none"
        elif pf and not peep_ok:
            severity, severity_tag = "P/F = " + str(int(pf)) + " but PEEP < 5 — Berlin criteria NOT met", "unclassified"
        else:
            raw_input = data.get("_raw_input", "").lower()
            if "severe" in raw_input:
                severity, severity_tag = "Severe (doctor-stated — obtain ABG to confirm P/F)", "severe"
            elif "moderate" in raw_input:
                severity, severity_tag = "Moderate (doctor-stated — obtain ABG to confirm P/F)", "moderate"
            elif "mild" in raw_input:
                severity, severity_tag = "Mild (doctor-stated — obtain ABG to confirm P/F)", "mild"
            else:
                severity, severity_tag = "Unclassified — P/F not calculable", "unclassified"

        crs = None
        if tv and pplat and peep and (pplat - peep) > 0:
            crs = round((tv / 1000) / (pplat - peep) * 1000, 1)

        mv = round((tv * rr) / 1000, 1) if tv and rr else None

        acp_risk, acp_reasons = False, []
        if dp and dp >= 15:      acp_risk = True; acp_reasons.append("dP >= 15 cmH2O")
        if paco2 and paco2 > 48: acp_risk = True; acp_reasons.append("PaCO2 > 48 mmHg")

        ecmo_criteria = []
        if pf and pf < 50:  ecmo_criteria.append("P/F < 50 mmHg (refer if persistent > 3h)")
        elif pf and pf < 80: ecmo_criteria.append("P/F < 80 mmHg (refer if persistent > 6h)")
        if ph and ph < 7.25 and paco2 and paco2 >= 60:
            ecmo_criteria.append("pH < 7.25 + PaCO2 >= 60 mmHg (refer if persistent > 6h)")

        parts = ["Berlin ARDS: " + severity]
        if pf and not pf_from_sf:
            parts.append("P/F ratio = " + str(int(pf)))
        elif pf_from_sf and sf_label:
            parts.append(sf_label)
        if tv_per_kg:
            tv_label = ("optimal (6 mL/kg target)" if tv_per_kg <= 6.0
                        else "acceptable (4-8 mL/kg range)" if tv_per_kg <= 8.0
                        else "EXCEEDS 8 mL/kg absolute maximum")
            parts.append("TV = " + str(tv_per_kg) + " mL/kg IBW (" + tv_label + ")")
        if dp is not None:
            if dp >= 20:
                parts.append("DANGEROUS driving pressure dP = " + str(dp) + " cmH2O — immediate action required")
            elif dp >= 15:
                parts.append("Elevated dP = " + str(dp) + " cmH2O — exceeds 15 cmH2O limit (ATS 2024 / ESICM 2023)")
            elif dp >= 13:
                parts.append("dP = " + str(dp) + " cmH2O — within target < 15 cmH2O, monitor after every vent change")
            else:
                parts.append("dP = " + str(dp) + " cmH2O — within target (< 15 cmH2O)")
        if pplat and pplat > 30:
            parts.append("Pplat " + str(pplat) + " cmH2O EXCEEDS 30 cmH2O absolute limit")
        if crs:
            crs_label = "severely reduced" if crs < 20 else "reduced" if crs < 40 else "normal range"
            parts.append("Static compliance = " + str(crs) + " mL/cmH2O (" + crs_label + ")")
        if ph and ph < 7.35 and paco2:
            if ph >= 7.25:
                parts.append(
                   "Permissive hypercapnia: pH " + str(ph) +
                   " acceptable (above 7.25 preferred threshold). " +
                   ("PaCO2 = " + str(paco2) + " mmHg — " if paco2 else "") + "Monitor closely — obtain ABG (GOLD 2024)."
                )
            elif ph >= 7.20:
                parts.append(
                   "WARNING: pH " + str(ph) +
                   " in danger zone (7.20–7.25). " +
                   "Approaching absolute floor of 7.20 (ATS 2024 / ESICM 2023). " +
                   ("PaCO2 = " + str(paco2) + " mmHg — " if paco2 else "") + "Increase RR now — obtain ABG to confirm PaCO2."
                )
            else:
                parts.append(
                   "DANGER: pH " + str(ph) +
                   " BELOW absolute permissive limit of 7.20 " +
                   "(ATS 2024 / ESICM 2023). " +
                   ("PaCO2 = " + str(paco2) + " mmHg — " if paco2 else "") + "Act immediately — obtain ABG."
                )
        if fio2 and fio2 >= 0.6:
            parts.append("FiO2 " + str(fio2) + " — oxygen toxicity risk if sustained > 24h")
        if severity_tag in ["moderate", "severe"] and peep is not None:
            if peep < 12:
                parts.append(
                    "PEEP = " + str(peep) + " cmH2O — may be suboptimal for " +
                    severity_tag + " ARDS. "
                    "ATS 2024 recommends higher PEEP strategy for moderate-to-severe ARDS. "
                    "Note: ESICM 2023 found insufficient evidence to prefer either "
                    "higher or lower PEEP strategy — individualize based on "
                    "hemodynamic response and oxygenation."
                )
            else:
                parts.append(
                    "PEEP = " + str(peep) + " cmH2O — in higher PEEP range for " +
                    severity_tag + " ARDS. "
                    "Monitor hemodynamics and driving pressure closely after every PEEP change. "
                    "ATS 2024 supports higher PEEP for moderate-to-severe ARDS; "
                    "ESICM 2023 notes insufficient evidence — assess individual response."
                )
        elif severity_tag == "mild" and peep is not None and peep > 8:
           parts.append(
               "PEEP = " + str(peep) + " cmH2O — consider lower PEEP strategy for mild ARDS "
               "(ATS 2024). Higher PEEP not recommended for mild disease. "
               "Monitor for overdistension — check driving pressure."
           )
        if acp_risk:
            parts.append("ACP RISK: " + " + ".join(acp_reasons) + " — bedside echo for RV assessment")
        if mv:
            parts.append("Minute ventilation = " + str(mv) + " L/min")
        physio = ". ".join(parts)

        steps = []
        if ph and ph < 7.20:
            if rr and rr >= 35:
                rr_msg = (
                    "RR = " + str(int(rr)) + "/min — ALREADY AT CEILING. "
                    "Cannot increase RR further. "
                    "IV sodium bicarbonate NOW. ECMO referral if pH does not improve."
                )
            elif rr and rr >= 32:
                headroom = 35 - int(rr)
                rr_msg = (
                    "RR = " + str(int(rr)) + "/min — only " + str(headroom) +
                    " breath(s) below ceiling (35/min). "
                    "Increase RR to 35/min immediately. "
                    "Prepare bicarbonate — ceiling will be reached with this adjustment."
                )
            elif rr:
                rr_msg = (
                    "Increase RR from " + str(int(rr)) + " to 35/min immediately."
                )
            else:
                rr_msg = "Increase RR immediately (max 35/min — check current RR first)."
            steps.append(
                "CRITICAL: pH " + str(ph) +
                " < 7.20 — ABSOLUTE LIMIT BREACHED (ATS 2024 / ESICM 2023). " +
                rr_msg +
                " DO NOT increase TV above 8 mL/kg IBW to correct pH — "
                "worsens barotrauma without fixing acidosis."
            )
        elif ph and ph < 7.25:
           if rr and rr >= 35:
               rr_msg = (
                   "RR = " + str(int(rr)) + "/min — ALREADY AT CEILING (35/min). "
                   "Cannot increase RR. Prepare sodium bicarbonate NOW. "
                   "Do NOT increase TV."
               )
           elif rr and rr >= 32:
               headroom = 35 - int(rr)
               rr_msg = (
                   "RR = " + str(int(rr)) + "/min — only " + str(headroom) +
                   " breath(s) available before ceiling (35/min). "
                   "Increase RR by " + str(headroom) + " breaths/min only. "
                   "Recheck ABG in 30 min. "
                   "Prepare sodium bicarbonate — near ceiling. "
                   "Do NOT increase TV."
               )
           elif rr:
               rr_msg = (
                   "Current RR = " + str(int(rr)) + "/min — increase by 2–4 breaths/min "
                   "(max 35/min). "
                   "Recheck ABG in 30 min. "
                   "If pH does not improve: prepare sodium bicarbonate. "
                   "Do NOT increase TV."
               )
           else:
               rr_msg = (
                   "Increase RR by 2–4 breaths/min (max 35/min — check current RR first). "
                   "Recheck ABG in 30 min. "
                   "If pH does not improve: prepare sodium bicarbonate. "
                   "Do NOT increase TV."
               )
           steps.append(
               "WARNING: pH " + str(ph) +
               " in 7.20–7.25 danger zone — approaching absolute floor. " +
               rr_msg
           )
        if pplat and pplat > 30:
            steps.append("ABSOLUTE LIMIT: Pplat " + str(pplat) + " > 30 — reduce TV by 1 mL/kg steps until Pplat <= 30. Minimum TV = 4 mL/kg IBW")
        if tv_per_kg and tv_per_kg > 8:
            steps.append("URGENT: TV " + str(tv) + " mL = " + str(tv_per_kg) + " mL/kg EXCEEDS 8 mL/kg. Reduce to " + str(int(tv_target_ml)) + " mL immediately")
        if ibw_kg and tv_target_ml:
            steps.append("TV target = " + str(int(tv_target_ml)) + " mL (6 mL/kg x " + str(ibw_kg) + " kg IBW). Range 4-8 mL/kg (ATS 2024)")
        if dp and dp >= 15:
            steps.append(
                "dP = " + str(dp) + " cmH2O >= 15 — reduce TV by 1 mL/kg steps. "
                "If dP persists after TV reduction, cautiously lower PEEP by 2 cmH2O. "
                "Target dP < 15 cmH2O (ATS 2024 / ESICM 2023)."
            )
        elif dp and dp >= 13:
           steps.append(
               "dP = " + str(dp) + " cmH2O — within target < 15 cmH2O. "
               "Monitor after every vent change. Reduce TV if dP reaches 15 cmH2O."
            )
        if severity_tag in ["moderate", "severe", "mild"] and fio2:
            peep_lookup = lookup_ardsnet_peep(fio2, severity_tag)
            if peep_lookup:
                rec_peep    = peep_lookup["recommended_peep"]
                rec_table   = peep_lookup["recommended_table"]
                lower_peep  = peep_lookup["lower_peep"]
                higher_peep = peep_lookup["higher_peep"]
                rationale   = peep_lookup["rationale"]

                if peep is not None and peep != rec_peep:
                    gap       = round(rec_peep - peep, 1)
                    direction = "increase" if gap > 0 else "decrease"

                    # FiO2 reduction guidance — only relevant when increasing PEEP
                    fio2_guidance = ""
                    if gap > 0 and fio2 and fio2 > 0.6:
                        fio2_guidance = (
                            " GOAL: once SpO2 >= 92% is achieved after PEEP increase, "
                            "reduce FiO2 stepwise (by 0.05–0.10 every 15–30 min) "
                            "targeting FiO2 < 0.60 to avoid oxygen toxicity "
                            "(sustained FiO2 >= 0.60 for > 24h causes oxidative lung injury). "
                            "Do NOT reduce FiO2 before confirming stable oxygenation."
                        )
                    elif gap > 0 and fio2 and fio2 <= 0.6:
                        fio2_guidance = (
                            " FiO2 = " + str(fio2) +
                            " is already below 0.60 — "
                            "maintain current FiO2 after PEEP increase. "
                            "Reduce further only if SpO2 consistently > 95%."
                        )

                    steps.append(
                        "ARDSNet PEEP TABLE (" + rec_table + "): "
                        "For FiO2 = " + str(fio2) +
                        " → recommended PEEP = " + str(rec_peep) + " cmH2O. "
                        "Current PEEP = " + str(peep) + " cmH2O "
                        "(" + direction + " by " + str(abs(gap)) + " cmH2O). "
                        "Lower PEEP table: " + str(lower_peep) + " cmH2O / "
                        "Higher PEEP table: " + str(higher_peep) + " cmH2O. " +
                        rationale +
                        " After change: recheck dP (target < 15), "
                        "Pplat (target <= 30), hemodynamics within 15 min." +
                        fio2_guidance
                    )
                elif peep is not None and peep == rec_peep:
                    steps.append(
                        "ARDSNet PEEP TABLE (" + rec_table + "): "
                        "Current PEEP = " + str(peep) + " cmH2O matches "
                        "recommended value for FiO2 = " + str(fio2) + ". " +
                        rationale
                    )

        if pf and pf < 150:
            prone_note = (
                "Prone positioning indicated — P/F = " + str(int(pf)) +
                " < 150 mmHg. Target duration >= 16h/day (ESICM 2023); "
                "minimum >= 12h/day acceptable (ATS 2024). "
                "Initiate within 36h of ARDS diagnosis."
            )
            if pf <= 100:
                prone_note += (
                    " PROSEVA trial: NNT = 6 for severe ARDS "
                    "(mean session duration 17h — supports >= 16h target)."
                )
            steps.append(prone_note)
        if pf and pf < 100:
            steps.append(
                "NMBA INDICATION (ATS 2024 / ESICM 2023): "
                "P/F = " + str(int(pf)) + " < 100 in severe ARDS — "
                "short-term neuromuscular blockade is indicated if within 48h of diagnosis "
                "AND Berlin ARDS criteria confirmed on ABG + CXR/CT. "
                "Agent: cisatracurium (preferred — no active metabolite accumulation). "
                "Duration: maximum 48 hours — do NOT continue beyond 48h. "
                "Confirm with senior clinician before initiating. "
                "Discontinue if: P/F improves > 150, hemodynamic instability develops, "
                "or 48h limit reached."
            )
            steps.append(
                "CORTICOSTEROIDS (ATS 2024 / ESICM 2023): "
                "Dexamethasone indicated for early moderate-to-severe ARDS "
                "— only if Berlin ARDS criteria confirmed "
                "(bilateral infiltrates on CXR/CT, non-cardiac origin, "
                "PEEP >= 5 cmH2O, P/F <= 200 on ABG). "
                "Dosing: 20 mg IV once daily x 5 days, "
                "then 10 mg IV once daily x 5 days (total 10-day course). "
                "Start within 24h of meeting severe ARDS criteria. "
                "Monitor: blood glucose q4h (target < 10 mmol/L), "
                "infection surveillance daily."
            )
        if pf and pf < 150:
            steps.append(
                "LRM GUIDANCE (ATS 2024 / ESICM 2023): "
                "STRONGLY AGAINST prolonged high-pressure maneuvers "
                "(>= 35 cmH2O sustained for > 60 seconds) — "
                "associated with hypotension and cardiac arrest in ~10% of cases. "
                "EXCEPTION: Brief recruitment after accidental ventilator disconnection "
                "or suctioning is acceptable — "
                "CPAP 35 cmH2O for 45 seconds only, then immediately return to "
                "previous PEEP and FiO2 settings. "
                "Monitor SpO2 and hemodynamics continuously during and after."
            )
        if ecmo_criteria:
            steps.append("RESCUE — VV-ECMO criteria met: " + " | ".join(ecmo_criteria) + ". Contact ECMO centre NOW (EOLIA)")
        if acp_risk:
            steps.append("RV PROTECTION: " + " + ".join(acp_reasons) + ". Echo. Avoid PEEP increases. MAP >= 65 with vasopressors")
        if bp_sys and bp_sys < 90:
            steps.append("HAEMODYNAMIC ALERT: SBP " + str(bp_sys) + " < 90 — HIGH PEEP CONTRAINDICATED. Fluid 500 mL. Vasopressors if MAP < 65")
        if not steps:
            steps.append(
                "Settings within targets — maintain TV 6 mL/kg IBW, "
                "Pplat <= 30 cmH2O, dP < 15 cmH2O. "
                "Reassess in 4-6h (ATS 2024 / ESICM 2023)."
            )
            steps.append("Obtain ABG now — P/F ratio required for Berlin ARDS classification and severity-based management")
            steps.append("Measure Pplat via inspiratory hold — required for driving pressure calculation (dP = Pplat - PEEP)")
            if fio2 and fio2 >= 0.6:
                steps.append("FiO2 " + str(fio2) + " >= 0.6 — titrate PEEP upward using ARDSNet PEEP/FiO2 table to allow FiO2 reduction")
            steps.append("SpO2 target 88-95% (PaO2 55-80 mmHg) — avoid hyperoxia")
            steps.append("Daily CXR — assess for pneumothorax, ETT position, consolidation pattern")
        next_step = " | ".join(steps)

        mon_parts = [
            "ABG 30-60 min after ANY vent change",
            "SpO2 target 88-95% (PaO2 55-80 mmHg)",
            "Pplat + dP after every TV adjustment",
            "Hemodynamics q1h",
            "Daily CXR",
        ]
        if pf and pf < 150:
            mon_parts.append(
                "Prone monitoring: SpO2 continuous, ETT position q2h, "
                "pressure injury assessment q2h, abdominal compartment pressure awareness"
            )
        elif pf is None and fio2 and fio2 >= 0.6:
            mon_parts.append(
                "Prone monitoring on standby — ABG required to confirm P/F ratio. "
                "If P/F < 150 confirmed on ABG: initiate prone protocol immediately"
            )
        if acp_risk:
            mon_parts.append("Echo q12h for RV function")
        if severity_tag == "severe" and pf and pf < 100:
            mon_parts.append(
                "NMBA: TOF q4h if paralysed — target TOF 1-2/4. "
                "Reassess liberation from NMBA every 24h. "
                "Sedation: target RASS -3 to -4 while paralysed"
            )
        monitoring = ". ".join(mon_parts)

        escalation = (
            "P/F < 150 despite LPV -> prone positioning. "
            "ECMO REFERRAL (EOLIA) — time-dependent criteria: "
            "P/F < 50 mmHg PERSISTENT > 3 hours, OR "
            "P/F < 80 mmHg PERSISTENT > 6 hours, OR "
            "pH < 7.25 + PaCO2 >= 60 mmHg PERSISTENT > 6 hours "
            "despite optimized ventilation — contact ECMO centre immediately. "
            "dP >= 20 unresponsive to TV reduction -> senior review. "
            "Pplat > 30 refractory -> reduce TV to 4 mL/kg IBW. "
            "pH < 7.20 despite RR 35/min -> bicarbonate + ECMO. "
            "Haemodynamic instability after PEEP change -> reduce PEEP 2 cmH2O + vasopressors. "
            "RV failure on echo -> prone + inhaled NO/prostacyclin."
        )

    # ════════════════════════════════════════════════════════
    # BRANCH 2 — COPD / Hypercapnic Respiratory Failure
    # Source: GOLD 2024
    # ════════════════════════════════════════════════════════

    elif "copd" in _diag_seg or "hypercapn" in _diag_seg or "obstruct" in _diag_seg:

        mv            = round((tv * rr) / 1000, 1) if tv and rr else None
        pip_pplat_gap = round(ppeak - pplat, 1) if ppeak and pplat else None
        if auto_peep:
            # Formula-primary: 50-80% of measured auto-PEEP (GOLD 2024 / Expert Consensus)
            # The "typically 4-8 cmH2O" range is illustrative only — never apply as fixed target
            # Hard rule: external PEEP must NEVER exceed measured intrinsic PEEP (Section 7)
            _ext_peep_raw = round(auto_peep * 0.75, 1)   # midpoint of 50-80% range
            rec_ext_peep  = min(_ext_peep_raw, auto_peep) # safety cap — never exceed intrinsic PEEP
        else:
            rec_ext_peep  = None

        # ── Inspiratory flow assessment (GOLD 2024 / Expert Consensus) ──
        # Low insp flow in COPD shortens available expiratory time,
        # worsening air trapping and auto-PEEP directly
        if insp_flow is not None:
            if insp_flow < 60:
                insp_flow_warning = (
                    "LOW INSPIRATORY FLOW: " + str(insp_flow) + " L/min — "
                    "BELOW minimum 60 L/min for COPD. "
                    "Increase to 80-100 L/min immediately. "
                    "Low flow prolongs inspiratory time → shortens expiratory time → "
                    "worsens air trapping and auto-PEEP (GOLD 2024 / Expert Consensus). "
                    "Target I:E >= 1:3 — verify after flow increase."
                )
            elif insp_flow < 80:
                insp_flow_warning = (
                    "SUBOPTIMAL FLOW: " + str(insp_flow) + " L/min — "
                    "below preferred 80-100 L/min for COPD. "
                    "Increase to 80 L/min to lengthen expiratory time and reduce auto-PEEP risk "
                    "(GOLD 2024 / Expert Consensus)."
                )
            else:
                insp_flow_warning = None   # >= 80 L/min — within target, no action needed
        else:
            insp_flow_warning = None   # not provided — no warning generated

        # ── Baseline PaCO2 over-correction warning (GOLD 2024 / Expert Consensus) ──
        # COPD patients have chronically elevated PaCO2 — normalizing it causes
        # metabolic alkalosis, cerebral vasoconstriction, and neurologic injury.
        # Only fires when baseline_paco2 is stored AND current paco2 is known.
        baseline_paco2     = _f(data.get("baseline_paco2"))
        baseline_paco2_warning = None
        if baseline_paco2 and paco2:
            if paco2 < baseline_paco2:
                drop        = round(baseline_paco2 - paco2, 1)
                drop_pct    = round((drop / baseline_paco2) * 100, 1)
                if paco2 < (baseline_paco2 - 10):
                    baseline_paco2_warning = (
                        "WARNING — BELOW BASELINE PaCO2: "
                        "Current PaCO2 = " + str(paco2) + " mmHg — "
                        "BELOW patient baseline of " + str(baseline_paco2) + " mmHg "
                        "(drop of " + str(drop) + " mmHg, " + str(drop_pct) + "%). "
                        "In COPD, chronic CO2 retention is compensated by renal bicarbonate retention. "
                        "Correcting PaCO2 below baseline causes post-hypercapnic metabolic alkalosis, "
                        "cerebral vasoconstriction, and neurologic injury (GOLD 2024 / Expert Consensus). "
                        "Target PaCO2 >= " + str(baseline_paco2) + " mmHg (patient baseline). "
                        "Reduce minute ventilation: decrease RR by 2/min or reduce TV by 1 mL/kg step. "
                        "Recheck ABG in 30 min."
                    )
                else:
                    # Within 10 mmHg below baseline — advisory only, not urgent
                    baseline_paco2_warning = (
                        "ADVISORY — PaCO2 approaching baseline floor: "
                        "Current PaCO2 = " + str(paco2) + " mmHg — "
                        + str(drop) + " mmHg below patient baseline of "
                        + str(baseline_paco2) + " mmHg. "
                        "Monitor closely — do NOT reduce PaCO2 further below baseline. "
                        "Target PaCO2 >= " + str(baseline_paco2) + " mmHg (GOLD 2024)."
                    )
            else:
                baseline_paco2_warning = None   # at or above baseline — no warning

        # ── FiO2 titration step — SpO2-guided (GOLD 2024 / Expert Consensus) ──
        # Target SpO2 88-92% in COPD — avoid over-oxygenation (Haldane effect,
        # hypoxic drive suppression, oxygen-induced hypercapnia worsening)
        if spo2 and fio2:
            if spo2 > 92:
                if fio2 <= 0.21:
                    fio2_step = (
                        "FiO2 TITRATION: SpO2 " + str(spo2) + "% > 92% ceiling — "
                        "FiO2 already at 0.21 (room air — cannot reduce further). "
                        "Verify SpO2 probe accuracy. "
                        "If supplemental O2 device in use, reduce flow rate. "
                        "Target SpO2 88-92% in COPD — "
                        "sustained SpO2 > 92% causes Haldane effect "
                        "(O2 displaces CO2 from Hb → PaCO2 rises) and "
                        "suppresses hypoxic drive (GOLD 2024)."
                    )
                else:
                    new_fio2 = round(max(0.21, fio2 - 0.05), 2)
                    fio2_step = (
                        "FiO2 TITRATION: SpO2 " + str(spo2) + "% > 92% ceiling — "
                        "REDUCE FiO2 from " + str(fio2) + " to " + str(new_fio2) + " now. "
                        "Continue reducing by 0.05 every 15 min until SpO2 88-92%. "
                        "Minimum FiO2 = 0.21 (room air — never go lower). "
                        "RISK: sustained SpO2 > 92% in COPD causes Haldane effect "
                        "(O2 displaces CO2 from Hb → PaCO2 rises) and "
                        "suppresses hypoxic drive (GOLD 2024). "
                        "Recheck ABG after each FiO2 reduction step."
                    )
            elif spo2 < 88:
                if fio2 >= 1.0:
                    fio2_step = (
                        "FiO2 TITRATION: SpO2 " + str(spo2) + "% < 88% floor — "
                        "FiO2 already at 1.0 (maximum — cannot increase further). "
                        "ABG stat. Senior review NOW. "
                        "Investigate: ETT malposition, secretion burden, "
                        "bronchospasm, pneumothorax, auto-PEEP worsening (GOLD 2024)."
                    )
                elif fio2 <= 0.21:
                    fio2_step = (
                        "FiO2 TITRATION: SpO2 " + str(spo2) + "% < 88% floor — "
                        "FiO2 at 0.21 (room air — minimum). "
                        "Increase FiO2 cautiously to 0.26 as bridge while investigating. "
                        "Urgent investigation required: secretion burden, bronchospasm, "
                        "ETT obstruction, atelectasis, pneumothorax. "
                        "ABG stat. Senior review NOW. "
                        "COPD over-oxygenation risk persists even during correction — "
                        "target SpO2 88-92%, stop increasing FiO2 once SpO2 "
                        "reaches 88% (GOLD 2024)."
                    )
                else:
                    new_fio2 = round(min(1.0, fio2 + 0.05), 2)
                    fio2_step = (
                        "FiO2 TITRATION: SpO2 " + str(spo2) + "% < 88% floor — "
                        "INCREASE FiO2 from " + str(fio2) + " to " + str(new_fio2) + " now. "
                        "Recheck SpO2 in 5 min after each step. "
                        "Target SpO2 88-92% — STOP increasing FiO2 once SpO2 reaches 88%. "
                        "DO NOT overshoot above 92% (Haldane effect, hypoxic drive suppression). "
                        "If SpO2 < 88% persists after 2 steps: ABG stat, "
                        "check ETT patency, secretion burden, bronchospasm (GOLD 2024)."
                    )
            else:
                fio2_step = (
                    "FiO2 TITRATION: SpO2 " + str(spo2) + "% — within COPD target 88-92%. "
                    "Hold FiO2 " + str(fio2) + ". DO NOT increase further. "
                    "Target PaO2 55-75 mmHg. "
                    "Recheck SpO2 after every ventilator change (GOLD 2024)."
                )
        elif spo2 and not fio2:
            fio2_step = (
                "FiO2 TITRATION: SpO2 " + str(spo2) + "% reported — "
                "FiO2 not stated. Provide current FiO2 to enable titration guidance. "
                "Target SpO2 88-92% in COPD (GOLD 2024)."
            )
        elif fio2 and not spo2:
            fio2_step = (
                "FiO2 TITRATION: FiO2 = " + str(fio2) + " — "
                "SpO2 not reported. Attach pulse oximetry now. "
                "Target SpO2 88-92% in COPD (GOLD 2024)."
            )
        else:
            fio2_step = (
                "FiO2 TITRATION: SpO2 and FiO2 not reported. "
                "Attach pulse oximetry and state current FiO2. "
                "Target SpO2 88-92% (PaO2 55-75 mmHg) in COPD (GOLD 2024)."
            )

        has_pea_risk   = auto_peep and auto_peep >= 10 and bp_sys and bp_sys < 80
        has_auto_peep  = auto_peep and auto_peep >= 5
        has_high_press = (pplat and pplat > 28) or (dp and dp >= 15)
        has_resistance = pip_pplat_gap and pip_pplat_gap > 10

        # ── PaCO2 rapid-correction safety check (GOLD 2024 / Expert Consensus) ──
        # Section 7: do NOT reduce PaCO2 > 20 mmHg OR > 50% within first 24h
        # Risk: intracranial haemorrhage / neurologic injury from rapid CO2 drop
        paco2_delta_warning = None
        prior_paco2 = _f(data.get("prior_paco2"))
        if prior_paco2 and paco2:
            paco2_drop     = round(prior_paco2 - paco2, 1)
            paco2_drop_pct = round((paco2_drop / prior_paco2) * 100, 1)
            if paco2_drop > 0:   # only warn on drops, not rises
                if paco2_drop > 20 and paco2_drop_pct > 50:
                    paco2_delta_warning = (
                        "DANGER — PaCO2 RAPID CORRECTION: "
                        "PaCO2 dropped " + str(paco2_drop) + " mmHg "
                        "(" + str(paco2_drop_pct) + "%) from prior value of "
                        + str(prior_paco2) + " mmHg to current " + str(paco2) + " mmHg. "
                        "BOTH thresholds breached: > 20 mmHg AND > 50% drop. "
                        "Risk of intracranial haemorrhage and neurologic injury "
                        "(GOLD 2024 / Expert Consensus). "
                        "Reduce minute ventilation NOW — increase PEEP or reduce RR. "
                        "Urgent neurology review if any neurologic change."
                    )
                elif paco2_drop > 20:
                    paco2_delta_warning = (
                        "WARNING — PaCO2 RAPID CORRECTION: "
                        "PaCO2 dropped " + str(paco2_drop) + " mmHg "
                        "from prior " + str(prior_paco2) + " mmHg to " + str(paco2) + " mmHg. "
                        "Exceeds 20 mmHg safe limit within 24h "
                        "(GOLD 2024 / Expert Consensus). "
                        "Risk of neurologic injury — slow the correction rate. "
                        "Target PaCO2 reduction <= 20 mmHg per 24h."
                    )
                elif paco2_drop_pct > 50:
                    paco2_delta_warning = (
                        "WARNING — PaCO2 RAPID CORRECTION: "
                        "PaCO2 dropped " + str(paco2_drop_pct) + "% "
                        "from prior " + str(prior_paco2) + " mmHg to " + str(paco2) + " mmHg. "
                        "Exceeds 50% safe limit within 24h "
                        "(GOLD 2024 / Expert Consensus). "
                        "Risk of neurologic injury — slow the correction rate. "
                        "Target PaCO2 reduction <= 50% per 24h."
                    )

        if has_pea_risk:
            status = "Critical"
            physio = (
                "OBSTRUCTIVE SHOCK / PEA RISK FROM DYNAMIC HYPERINFLATION. "
                "Auto-PEEP " + str(auto_peep) + " cmH2O — impeded venous return — decreased CO. "
                "BP " + str(bp) + " mmHg. " + (trend_str if trend_str else "")
            )
            next_step = " | ".join([
                "IMMEDIATE: Disconnect ventilator 30-60s — passive exhalation to release trapped air (GOLD 2024)",
                "Reduce RR to 8-10/min IMMEDIATELY — most effective auto-PEEP treatment",
                "Reduce TV to 6 mL/kg IBW",
                (
                    insp_flow_warning if insp_flow_warning else
                    "Increase inspiratory flow to 80-100 L/min — shorten Ti, lengthen Te"
                ),
                "Target I:E 1:4 to 1:5",
                "DO NOT increase external PEEP — worsens air trapping",
                "If SBP < 80 persists: manually assist exhalation via chest wall pressure",
                "Norepinephrine 0.1-0.3 mcg/kg/min as bridge while reducing hyperinflation",
                "Adequate sedation to reduce respiratory drive",
                fio2_step,
            ])
            if baseline_paco2_warning:
                next_step = baseline_paco2_warning + " | " + next_step
            if paco2_delta_warning:
                next_step = paco2_delta_warning + " | " + next_step
            monitoring = (
                "Continuous arterial BP. Expiratory hold after every RR change. "
                "ECG continuous — watch for PEA. ABG 30 min. "
                "Auto-PEEP q15 min until < 5 cmH2O."
            )
            escalation = (
                "ESCALATE NOW — senior ICU at bedside. Prepare CPR if PEA. "
                "Echo — rule out tension PTX, PE, tamponade. "
                "Consider ECCO2R if refractory CO2 retention. "
                "Do NOT reduce PaCO2 > 20 mmHg or > 50% within 24h (haemorrhagic risk)."
            )

        elif has_auto_peep:
            _raw_status = assess_ventilation_status({**data, "bp_sys": bp_sys}, trend)
            if _raw_status == "Critical" and (not ph or ph >= 7.20) and (not bp_sys or bp_sys >= 70):
                # dP/pressure alone does not justify Critical in COPD auto-PEEP context
                # Critical only if pH < 7.20 or BP crisis
                status = "Worsening"
            elif _raw_status == "Stable":
                status = "Worsening"  # auto-PEEP always at least Worsening
            else:
                status = _raw_status
            parts  = ["Dynamic hyperinflation: auto-PEEP = " + str(auto_peep) + " cmH2O"]
            if pip_pplat_gap and pip_pplat_gap > 10:
                parts.append("PIP-Pplat gap = " + str(pip_pplat_gap) + " cmH2O — elevated airway resistance")
            if pplat and pplat > 30:
                parts.append("DANGER: Pplat " + str(pplat) + " cmH2O EXCEEDS absolute COPD limit of 30 cmH2O — immediate action required (GOLD 2024 / Expert Consensus)")
            elif pplat and pplat >= 28:
                parts.append("WARNING: Pplat " + str(pplat) + " cmH2O — approaching preferred COPD ceiling of 28 cmH2O. Monitor closely (GOLD 2024 / Expert Consensus)")
            if rr and rr > 15:
                parts.append(
                    "DANGER: RR " + str(int(rr)) + "/min EXCEEDS hard ceiling of 15/min — "
                    "severely shortened expiratory time, worsening auto-PEEP (GOLD 2024 / Expert Consensus). "
                    "Reduce RR immediately."
                )
            elif rr and rr == 15:
                parts.append(
                    "DANGER: RR " + str(int(rr)) + "/min AT hard ceiling of 15/min — "
                    "expiratory time critically shortened. "
                    "Reduce RR to 12/min (GOLD 2024 / Expert Consensus)."
                )
            elif rr and rr >= 13:
                parts.append(
                    "WARNING: RR " + str(int(rr)) + "/min approaching soft ceiling — "
                    "target 8-12/min in COPD. "
                    "RR >= 13 shortens expiratory time and risks worsening auto-PEEP. "
                    "Reduce RR to 12/min or below (GOLD 2024 / Expert Consensus)."
                )
            if mv: parts.append("Minute ventilation = " + str(mv) + " L/min")
            if ph and ph < 7.35:
                if ph >= 7.25:
                    parts.append(
                        "Permissive hypercapnia: pH " + str(ph) +
                        " acceptable (above 7.25 preferred threshold). " +
                        ("PaCO2 = " + str(paco2) + " mmHg — " if paco2 else "") + "Monitor closely — obtain ABG (GOLD 2024)."
                    )
                elif ph >= 7.20:
                    parts.append(
                        "WARNING: pH " + str(ph) +
                        " in danger zone (7.20–7.25) — approaching absolute floor. " +
                        ("PaCO2 = " + str(paco2) + " mmHg. " if paco2 else "PaCO2 unknown — obtain ABG. ") +
                        "Increase RR cautiously (max 12/min in COPD — watch auto-PEEP). "
                        "Recheck ABG in 30 min (GOLD 2024 / Expert Consensus)."
                    )
                else:
                    parts.append(
                        "DANGER: pH " + str(ph) +
                        " BELOW absolute floor of 7.20. " +
                        ("PaCO2 = " + str(paco2) + " mmHg. " if paco2 else "PaCO2 unknown — obtain ABG. ") +
                        "Senior review NOW. ECCO2R consideration (GOLD 2024)."
                    )
            physio = ". ".join(parts)

            if rr and rr > 15:
                rr_step = (
                    "DANGER: RR " + str(int(rr)) + "/min EXCEEDS hard ceiling of 15/min — "
                    "reduce RR IMMEDIATELY to 12/min. "
                    "Every breath above 12 shortens expiratory time and worsens air trapping (GOLD 2024)."
                )
            elif rr and rr == 15:
                rr_step = (
                    "DANGER: RR AT hard ceiling of 15/min — "
                    "reduce to 12/min immediately. "
                    "Hard ceiling must not be exceeded in COPD (GOLD 2024 / Expert Consensus)."
                )
            elif rr and rr >= 13:
                rr_step = (
                    "WARNING: RR " + str(int(rr)) + "/min at soft ceiling — "
                    "reduce to 12/min now. "
                    "RR 13-14 risks worsening auto-PEEP — "
                    "only acceptable if pH < 7.20 AND no auto-PEEP present (GOLD 2024)."
                )
            else:
                rr_step = (
                    "1st: Reduce RR to 8-12/min — "
                    "primary auto-PEEP treatment. "
                    "Lower RR lengthens expiratory time and allows trapped air to escape (GOLD 2024)."
                )

            steps  = [
                rr_step,
                (insp_flow_warning if insp_flow_warning else
                    "2nd: Increase inspiratory flow to 80-100 L/min — shorten Ti, lengthen Te"),
                "3rd: Reduce TV to 6-8 mL/kg IBW",
                "4th: Target I:E >= 1:3 (aim 1:4 to 1:5)",
                "5th: Set external PEEP to " + (
                    str(rec_ext_peep) + " cmH2O "
                    "(= 75% of measured auto-PEEP " + str(auto_peep) + " cmH2O). "
                    "Formula: 50-80% of measured auto-PEEP — offsets triggering threshold "
                    "without worsening hyperinflation. "
                    "HARD RULE: external PEEP must NEVER exceed measured auto-PEEP "
                    "(GOLD 2024 / Expert Consensus)."
                    if rec_ext_peep else
                    "50-80% of measured auto-PEEP — measure auto-PEEP via expiratory hold first. "
                    "HARD RULE: external PEEP must NEVER exceed measured auto-PEEP "
                    "(GOLD 2024 / Expert Consensus)."
                ),
                "6th: Expiratory hold — remeasure auto-PEEP after each RR change",
                fio2_step,
                "IF SBP drops during management: disconnect ventilator 30-60s — "
                "allows passive exhalation and release of trapped air pressure (GOLD 2024 / Expert Consensus). "
                "Manually assist exhalation via chest wall pressure if SBP does not recover. "
                "Reconnect only after BP stabilises.",
            ]
            if has_resistance:
                steps.append("HIGH RESISTANCE (gap " + str(pip_pplat_gap) + " cmH2O): suction ETT, bronchodilator, check circuit")
            if ph and ph < 7.20:
                steps.insert(0,
                    "CRITICAL: pH " + str(ph) +
                    " < 7.20 — ABSOLUTE FLOOR BREACHED (GOLD 2024). "
                    "Senior review NOW. "
                    "Increase RR by 1-2 breaths ONLY if current RR < 12 — "
                    "do NOT exceed 12/min in COPD (worsens auto-PEEP). "
                    "If RR already at 12: ECCO2R referral immediately. "
                    "Do NOT increase TV to correct pH."
                )
            elif ph and ph >= 7.20 and ph < 7.25:
                steps.insert(0,
                    "WARNING: pH " + str(ph) +
                    " in danger zone (7.20–7.25) — approaching absolute floor (GOLD 2024). "
                    "Increase RR by 1-2 breaths/min if current RR < 12 — "
                    "watch for worsening auto-PEEP after every increase. "
                    "Recheck expiratory hold after RR change. "
                    "Recheck ABG in 30 min. "
                    "If pH does not improve: senior review + ECCO2R consideration. "
                    "Do NOT increase TV to correct pH."
                )
            next_step  = " | ".join(steps)
            if baseline_paco2_warning:
                next_step = baseline_paco2_warning + " | " + next_step
            if paco2_delta_warning:
                next_step = paco2_delta_warning + " | " + next_step
            monitoring = (
                "Auto-PEEP q30 min via expiratory hold. Hemodynamics q15 min. "
                "ABG 1h after every change. SpO2 88-92%. PIP and Pplat after every change."
            )
            escalation = (
                "Escalate if: auto-PEEP > 15. SBP < 90. pH < 7.20. "
                "Consider ECCO2R if refractory hypercapnia. "
                "Do NOT reduce PaCO2 > 20 mmHg or > 50% in 24h (neurologic injury risk)."
            )

        elif has_high_press:
            status = assess_ventilation_status({**data, "bp_sys": bp_sys}, trend)
            if status == "Stable":
                status = "Worsening"  # elevated pressures always at least Worsening
            parts  = []
            if pplat and pplat > 30:
                parts.append("DANGER: Pplat " + str(pplat) + " cmH2O EXCEEDS absolute COPD limit of 30 cmH2O — immediate action required (GOLD 2024 / Expert Consensus)")
            elif pplat and pplat >= 28:
                parts.append("WARNING: Pplat " + str(pplat) + " cmH2O — approaching preferred COPD ceiling of 28 cmH2O. Monitor closely (GOLD 2024 / Expert Consensus)")
            if ph and ph < 7.20:
                parts.append(
                    "DANGER: pH " + str(ph) +
                    " BELOW absolute floor of 7.20. " +
                    ("PaCO2 = " + str(paco2) + " mmHg. " if paco2 else "PaCO2 unknown — obtain ABG. ") +
                    "Senior review NOW. ECCO2R consideration (GOLD 2024)."
                )
            elif ph and ph >= 7.20 and ph < 7.25:
                parts.append(
                    "WARNING: pH " + str(ph) +
                    " in danger zone (7.20–7.25) — approaching absolute floor. " +
                    ("PaCO2 = " + str(paco2) + " mmHg. " if paco2 else "PaCO2 unknown — obtain ABG. ") +
                    "Increase RR cautiously (max 12/min in COPD — watch auto-PEEP). " +
                    "Recheck ABG in 30 min (GOLD 2024 / Expert Consensus)."
                )
            elif ph and ph < 7.35:
                parts.append(
                    "Permissive hypercapnia: pH " + str(ph) +
                    " acceptable (above 7.25 preferred threshold). " +
                    ("PaCO2 = " + str(paco2) + " mmHg — " if paco2 else "") + "Monitor closely — obtain ABG (GOLD 2024)."
                )
            if dp and dp >= 15:      parts.append("Driving pressure " + str(dp) + " cmH2O at/above 15 limit")
            if has_resistance:       parts.append("Elevated PIP-Pplat gap " + str(pip_pplat_gap) + " cmH2O")
            physio = ". ".join(parts) if parts else "Elevated airway pressures in COPD"
            steps  = []
            if has_resistance:
                steps.append("HIGH PIP-Pplat GAP: suction ETT, bronchodilator, check for kinked circuit")
            if insp_flow_warning:
                steps.append(insp_flow_warning)
            if pplat and pplat > 30:
                steps.append(
                    "DANGER: Pplat " + str(pplat) + " cmH2O EXCEEDS absolute limit of 30 cmH2O — "
                    "reduce TV by 1 mL/kg steps IMMEDIATELY until Pplat <= 30 cmH2O. "
                    "Minimum TV = 4 mL/kg IBW. Recheck Pplat after every reduction (GOLD 2024)."
                )
            elif pplat and pplat >= 28:
                steps.append(
                    "WARNING: Pplat " + str(pplat) + " cmH2O — approaching preferred COPD ceiling of 28 cmH2O. "
                    "Reduce TV by 1 mL/kg step and recheck Pplat. "
                    "Target Pplat < 28 cmH2O (strictly preferred in COPD). "
                    "Absolute limit is 30 cmH2O — act before it is reached (GOLD 2024)."
                )
            if dp and dp >= 15:
                steps.append("dP >= 15 — reduce TV first. If persists, reduce external PEEP by 2 cmH2O")
            steps.append("Expiratory hold — measure auto-PEEP baseline")
            steps.append(fio2_step)
            if ph and ph < 7.20:
                steps.insert(0,
                    "CRITICAL: pH " + str(ph) +
                    " < 7.20 — ABSOLUTE FLOOR BREACHED (GOLD 2024). "
                    "Senior review NOW. "
                    "Increase RR by 1-2 breaths ONLY if current RR < 12 — "
                    "do NOT exceed 12/min in COPD (worsens auto-PEEP). "
                    "If RR already at 12: ECCO2R referral immediately. "
                    "Do NOT increase TV to correct pH."
                )
            elif ph and ph >= 7.20 and ph < 7.25:
                steps.insert(0,
                    "WARNING: pH " + str(ph) +
                    " in danger zone (7.20–7.25) — approaching absolute floor (GOLD 2024). "
                    "Increase RR by 1-2 breaths/min if current RR < 12 — "
                    "watch for worsening auto-PEEP after every RR increase. "
                    "Recheck expiratory hold after RR change. "
                    "Recheck ABG in 30 min. "
                    "If pH does not improve: senior review + ECCO2R consideration. "
                    "Do NOT increase TV to correct pH."
                )
            next_step  = " | ".join(steps)
            if baseline_paco2_warning:
                next_step = baseline_paco2_warning + " | " + next_step
            if paco2_delta_warning:
                next_step = paco2_delta_warning + " | " + next_step
            monitoring = "Pplat and dP after every vent change. ABG 1h. Auto-PEEP q1h. SpO2 88-92%."
            escalation = (
                "Pplat WARNING (28-30): escalate to senior if Pplat does not decrease after "
                "TV reduction by 2 mL/kg IBW. "
                "Pplat DANGER (>30): escalate to senior immediately — do not wait for response. "
                "Auto-PEEP > 5 developing -> reduce RR first. "
                "pH < 7.20 -> senior review + ECCO2R consideration. "
                "BP < 90 -> disconnect ventilator 30-60s + vasopressors."
            )

        else:
            parts = []
            if paco2 and ph:
                if ph < 7.35: parts.append("Acute hypercapnic RF: pH " + str(ph) + ", PaCO2 " + str(paco2) + " mmHg — target baseline PaCO2")
                else:         parts.append("Chronic CO2 retention compensated: PaCO2 " + str(paco2) + " mmHg, pH " + str(ph))
            if rr and rr > 12: parts.append("RR " + str(rr) + "/min above 8-12 target")
            if mv: parts.append("MV = " + str(mv) + " L/min")
            physio    = ". ".join(parts) if parts else "COPD on MV — monitor for air trapping"
            ph_step = ""
            if ph and ph < 7.20:
                ph_step = (
                    "CRITICAL: pH " + str(ph) +
                    " < 7.20 — ABSOLUTE FLOOR BREACHED (GOLD 2024). "
                    "Senior review NOW. "
                    "Increase RR by 1-2 breaths ONLY if current RR < 12 — "
                    "do NOT exceed 12/min in COPD (worsens auto-PEEP). "
                    "If RR already at 12: ECCO2R referral immediately. "
                    "Do NOT increase TV to correct pH."
                )
            elif ph and ph >= 7.20 and ph < 7.25:
                ph_step = (
                    "WARNING: pH " + str(ph) +
                    " in danger zone (7.20–7.25) — approaching absolute floor (GOLD 2024). "
                    "Increase RR by 1-2 breaths/min if current RR < 12 — "
                    "watch for worsening auto-PEEP after every RR increase. "
                    "Recheck expiratory hold after RR change. "
                    "Recheck ABG in 30 min. "
                    "If pH does not improve: senior review + ECCO2R consideration. "
                    "Do NOT increase TV to correct pH."
                )
            elif ph and ph >= 7.25 and ph < 7.35:
                ph_step = (
                    "Permissive hypercapnia: pH " + str(ph) +
                    " acceptable (above 7.25 preferred threshold). "
                    "Target patient baseline PaCO2 — do NOT attempt to normalize. "
                    "Monitor pH trend hourly (GOLD 2024)."
                )
            else:
                ph_step = (
                    "Permissive hypercapnia: target patient baseline PaCO2. "
                    "pH >= 7.20 acceptable minimum — pH >= 7.25 preferred (GOLD 2024)."
                )

            flow_steps = [
                "Preferred mode: VC-AC — ensures consistent MV (GOLD 2024)",
                "Initial settings: TV 6-8 mL/kg IBW, RR 8-12/min, Flow 60-100 L/min, I:E 1:3 to 1:5",
                "Expiratory hold NOW — establish auto-PEEP baseline",
                ph_step,
                fio2_step,
            ]
            if insp_flow_warning:
                flow_steps.append(insp_flow_warning)
            flow_steps.append(
                "NIV GUIDANCE (GOLD 2024 / Expert Consensus): "
                + (
                    "URGENT — pH " + str(ph) + " in 7.25–7.35 pre-NIV warning zone. "
                    "Initiate NIV NOW if not yet intubated: "
                    "IPAP 12-16 cmH2O / EPAP 4-8 cmH2O, FiO2 to maintain SpO2 88-92%. "
                    "Reassess pH and RR at 1h — if no improvement: escalate to invasive ventilation. "
                    "NIV failure criteria: pH worsening, RR > 30, SpO2 < 88%, GCS drop, haemodynamic instability."
                    if ph and 7.25 <= ph <= 7.35 else
                    "If pH <= 7.35 and not yet intubated: trial NIV before invasive ventilation. "
                    "Settings: IPAP 12-16 / EPAP 4-8, FiO2 to maintain SpO2 88-92%. "
                    "Reassess at 1h. Failure criteria: pH worsening, RR > 30, SpO2 < 88%, GCS drop."
                )
            )
            next_step = " | ".join(flow_steps)
            if baseline_paco2_warning:
                next_step = baseline_paco2_warning + " | " + next_step
            if paco2_delta_warning:
                next_step = paco2_delta_warning + " | " + next_step
            monitoring = "Expiratory hold q2h. ABG 1h after any change. SpO2 88-92%. Hemodynamics q1h."
            escalation = (
                "Auto-PEEP > 5 cmH2O developing -> reduce RR first. "
                "pH < 7.20 -> senior review + ECCO2R consideration immediately. "
                "BP < 90 -> disconnect ventilator 30-60s + vasopressors. "
                "NIV FAILURE — escalate to invasive ventilation if ANY of: "
                "pH worsening after 1h on NIV, RR > 30 sustained, "
                "SpO2 < 88% despite FiO2 titration, GCS drop >= 2, "
                "haemodynamic instability on NIV (GOLD 2024 / Expert Consensus). "
                "Do NOT reduce PaCO2 > 20 mmHg or > 50% within 24h "
                "(risk of intracranial haemorrhage — GOLD 2024)."
            )

    # ════════════════════════════════════════════════════════
    # BRANCH 3 — Weaning / SBT / Extubation / Post-op
    # Sources: ATS/ACCP 2017, AARC 2024, ESICM 2023
    # ════════════════════════════════════════════════════════
    elif any(k in _diag_seg for k in ["wean", "post", "extubat", "sbt", "liberat"]):
        return _weaning_branch(diagnosis, data, ibw_kg, trend, dp_label)

    # ════════════════════════════════════════════════════════
    # BRANCH 4 — Unknown / Unspecified
    # ════════════════════════════════════════════════════════
    else:
        physio     = ("pH " + str(ph) + " | PaCO2 " + str(paco2) + " | PaO2 " + str(pao2)
                      if any([ph, paco2, pao2]) else "Insufficient data — specify diagnosis")
        next_step  = "Diagnosis not recognized — select from: ARDS | COPD | COPD+ARDS | Weaning / SBT | Post-op Weaning"
        monitoring = "Monitor ABG, SpO2, hemodynamics, ventilator waveforms continuously."
        escalation = "Follow institutional escalation protocol."

    return {
        "ventilation_status":         status,
        "physiologic_interpretation": physio,
        "immediate_next_step":        next_step,
        "monitoring_and_safety":      monitoring,
        "escalation_criteria":        escalation,
        "driving_pressure":           dp_label,
        "pf_ratio":                   str(int(pf)) if pf else None,
        "rsbi":                       str(rsbi) if rsbi else None,
        "rsbi_relevant":              any(k in diag for k in ["wean","post","extubat","sbt","liberat"]),
        "tv_per_kg_ibw":              str(tv_per_kg) + " mL/kg IBW" if tv_per_kg else None,
        "map":                        str(map_val) if map_val else None,
        "trend_summary":              trend_str or "First assessment",
    }
