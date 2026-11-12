"""Vehicle profiles: researched normal values, known problems and extra data
codes for one car, in a JSON file the app can import.

A profile is usually written by Claude after researching a car (in the app's
"Research a vehicle" window, or in a normal Claude chat using the prompt from
research_prompt()), then imported. Everything in it is checked by validate()
before the app uses it, because it comes from outside the app.

Format (schema "fiero-aldl.vehicle-profile/1"):

{
  "schema": "fiero-aldl.vehicle-profile/1",
  "vehicle": {"name": "...", "years": "2002", "make": "...", "model": "...",
              "engine": "...", "engine_code": "...", "notes": "..."},
  "connection": {"type": "obd2" | "aldl", "protocol": "...", "notes": "..."},
  "normals": {
    "<key>": {"label": "...", "signal": "<signal id>", "unit": "...",
              "low": number|null, "high": number|null, "condition": "...",
              "short": "...", "limits": {"name": number, ...},
              "confidence": "gm|gm_general|oem|oem_general|owners|app",
              "note": "...", "sources": ["https://...", ...]}
  },
  "known_issues": [{"title": "...", "symptoms": "...", "what_data_shows": "...",
                    "checks": "...", "sources": ["https://..."]}],
  "enhanced_pids": [{"name": "...", "mode": "22", "pid": "1234", "bytes": 1|2,
                     "formula": "A*0.5-40", "unit": "...", "confidence": "...",
                     "sources": ["https://..."]}],
  "research": {"by": "...", "date": "YYYY-MM-DD", "notes": "..."}
}
"""

import ast
import json
import os
import re

SCHEMA = "fiero-aldl.vehicle-profile/1"
HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(HERE, "profiles")

CONFIDENCE = {
    "gm": "GM spec",
    "gm_general": "GM, this era",
    "oem": "Factory spec",
    "oem_general": "Maker, this era",
    "owners": "Owners",
    "app": "App estimate",
}

# Standard signals for OBD-II cars (ids used by "signal" in normals).
SIGNALS = {
    "rpm": ("Engine RPM", "rpm"),
    "speed": ("Vehicle speed", "mph"),
    "coolant_temp": ("Coolant temp", "F"),
    "intake_temp": ("Intake air temp", "F"),
    "maf": ("Mass air flow", "g/s"),
    "map": ("Manifold pressure (MAP)", "kPa"),
    "tps": ("Throttle position", "%"),
    "engine_load": ("Calculated load", "%"),
    "timing_advance": ("Timing advance", "deg"),
    "stft_b1": ("Short-term fuel trim, bank 1", "%"),
    "ltft_b1": ("Long-term fuel trim, bank 1", "%"),
    "stft_b2": ("Short-term fuel trim, bank 2", "%"),
    "ltft_b2": ("Long-term fuel trim, bank 2", "%"),
    "o2_b1s1": ("O2 bank 1 sensor 1 (upstream)", "V"),
    "o2_b1s2": ("O2 bank 1 sensor 2 (downstream)", "V"),
    "o2_b2s1": ("O2 bank 2 sensor 1 (upstream)", "V"),
    "battery": ("Battery / system voltage", "V"),
    "fuel_pressure": ("Fuel pressure (gauge)", "psi"),
}

_URL = re.compile(r"^https?://[^\s]+$")
_FORMULA_OK = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)


class ProfileError(ValueError):
    pass


def _num_or_none(x):
    return x is None or (isinstance(x, (int, float)) and not isinstance(x, bool))


def check_formula(formula):
    """Formulas may only use A-D (data bytes), numbers and + - * /."""
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ProfileError(f"formula {formula!r} isn't valid: {e.msg}")
    for node in ast.walk(tree):
        if not isinstance(node, _FORMULA_OK):
            raise ProfileError(f"formula {formula!r} uses something not allowed "
                               f"({type(node).__name__}); only A-D, numbers and + - * /")
        if isinstance(node, ast.Name) and node.id not in ("A", "B", "C", "D"):
            raise ProfileError(f"formula {formula!r} uses {node.id!r}; only A, B, C, D allowed")
        if isinstance(node, ast.Constant) and not _num_or_none(node.value):
            raise ProfileError(f"formula {formula!r} contains a non-number")
    return compile(tree, "<formula>", "eval")


def eval_formula(code, data_bytes):
    env = {k: (data_bytes[i] if i < len(data_bytes) else 0) for i, k in enumerate("ABCD")}
    return eval(code, {"__builtins__": {}}, env)   # noqa: S307 - checked by check_formula


def validate(p):
    """Raise ProfileError listing every problem; return the profile if it's fine."""
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    if not isinstance(p, dict):
        raise ProfileError("the file isn't a JSON object")
    need(p.get("schema") == SCHEMA, f'"schema" must be "{SCHEMA}"')
    v = p.get("vehicle")
    need(isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip(),
         '"vehicle.name" is required')
    c = p.get("connection")
    need(isinstance(c, dict) and c.get("type") in ("obd2", "aldl"),
         '"connection.type" must be "obd2" or "aldl"')
    normals = p.get("normals")
    need(isinstance(normals, dict) and normals, '"normals" must be a non-empty object')
    for key, n in (normals or {}).items() if isinstance(normals, dict) else []:
        where = f'normals["{key}"]'
        if not isinstance(n, dict):
            errs.append(f"{where} must be an object")
            continue
        for f in ("label", "unit", "condition", "confidence", "note"):
            need(isinstance(n.get(f), str), f'{where}.{f} must be text')
        need(n.get("confidence") in CONFIDENCE,
             f'{where}.confidence must be one of {", ".join(CONFIDENCE)}')
        need(_num_or_none(n.get("low")) and _num_or_none(n.get("high")),
             f"{where}.low/high must be numbers or null")
        if isinstance(n.get("low"), (int, float)) and isinstance(n.get("high"), (int, float)):
            need(n["low"] <= n["high"], f"{where}: low is above high")
        lim = n.get("limits", {})
        need(isinstance(lim, dict) and all(_num_or_none(x) and x is not None for x in lim.values()),
             f"{where}.limits must map names to numbers")
        src = n.get("sources", [])
        need(isinstance(src, list) and all(isinstance(s, str) and _URL.match(s) for s in src),
             f"{where}.sources must be a list of http(s) links")
        if n.get("confidence") not in ("app",) and isinstance(src, list):
            need(len(src) > 0, f"{where}: only an \"app\" estimate may have no sources")
        sig = n.get("signal")
        if c and c.get("type") == "obd2" and sig is not None:
            need(sig in SIGNALS, f'{where}.signal "{sig}" isn\'t a known signal')
    for i, k in enumerate(p.get("known_issues", []) or []):
        need(isinstance(k, dict) and isinstance(k.get("title"), str),
             f"known_issues[{i}] needs a title")
        if isinstance(k, dict):
            src = k.get("sources", [])
            need(isinstance(src, list) and all(isinstance(s, str) and _URL.match(s) for s in src),
                 f"known_issues[{i}].sources must be a list of http(s) links")
    for i, e in enumerate(p.get("enhanced_pids", []) or []):
        where = f"enhanced_pids[{i}]"
        if not isinstance(e, dict):
            errs.append(f"{where} must be an object")
            continue
        need(isinstance(e.get("name"), str), f"{where}.name is required")
        need(str(e.get("mode")) in ("22",), f'{where}.mode must be "22"')
        need(isinstance(e.get("pid"), str) and re.fullmatch(r"[0-9A-Fa-f]{4}", e.get("pid", "")),
             f"{where}.pid must be 4 hex digits")
        need(e.get("bytes") in (1, 2, 3, 4), f"{where}.bytes must be 1-4")
        try:
            check_formula(str(e.get("formula", "")))
        except ProfileError as ex:
            errs.append(f"{where}: {ex}")
    if errs:
        raise ProfileError("; ".join(errs))
    return p


def load(path):
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as e:
            raise ProfileError(f"not valid JSON: {e}")
    return validate(data)


def list_profiles(user_dir=None):
    """[(path, profile)] for built-in profiles then imported ones (valid only).

    An imported profile with the same vehicle name as a built-in one replaces
    it (so an edited copy, e.g. with PIDs added by the PID explorer, wins).
    """
    out = []
    for folder in (BUILTIN_DIR, user_dir):
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                path = os.path.join(folder, name)
                try:
                    out.append((path, load(path)))
                except (OSError, ProfileError):
                    continue
    seen = {}
    for path, prof in out:
        seen[prof["vehicle"]["name"]] = (path, prof)
    order = []
    for path, prof in out:
        if seen[prof["vehicle"]["name"]] not in order:
            order.append(seen[prof["vehicle"]["name"]])
    return order


def safe_filename(profile):
    name = profile["vehicle"]["name"].lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:60] + ".json"


def import_profile(src_path, user_dir):
    """Validate a profile file and copy it into the user's profile folder."""
    prof = load(src_path)
    os.makedirs(user_dir, exist_ok=True)
    dest = os.path.join(user_dir, safe_filename(prof))
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=2)
    return dest, prof


def extract_json(text):
    """Pull the profile JSON out of a reply (a ```json block, or the outermost object)."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    if not raw.strip():
        raise ProfileError("no JSON found in the reply")
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ProfileError(f"the reply's JSON isn't valid: {e}")


def research_prompt(year, make, model, engine="", extra=""):
    """Instructions for Claude to research a car and write a profile.

    Used by the app's research window, and shown so it can be pasted into a
    normal Claude chat with web search on.
    """
    car = " ".join(x for x in (year, make, model, engine) if x).strip()
    signals = "\n".join(f"  - {k}: {lbl} ({unit})" for k, (lbl, unit) in SIGNALS.items())
    return f"""Research diagnostic baselines for this car and write a vehicle profile for the \
Fiero ALDL app: {car}. {extra}

Use web search. Prefer factory service manual figures, GM/maker technical bulletins and \
diagnostic charts; then reputable references for this engine family; then consistent \
reports from owners of this exact car on enthusiast forums. Never invent a number: if you \
can't find a value, leave that entry out. Every value needs its source links and an honest \
confidence:
  gm / oem = factory spec for this car; gm_general / oem_general = maker figure for this \
engine family or era; owners = consistent owner reports; app = a general rule of thumb \
(no source needed, use sparingly).

Cover, where you can find them (warm engine unless stated):
- warm idle RPM; coolant: thermostat rating, fan on temperatures, overheating
- fuel trims (short and long term) healthy range and the trim where lean/rich codes set
- MAF (g/s) at warm idle if it has a MAF; MAP (kPa) at idle and at full boost if it has a MAP
- throttle position at closed throttle; timing advance at idle
- upstream O2 switching behaviour; downstream O2 behaviour with a healthy catalyst
- charging voltage; fuel pressure (key on engine off, and at idle)
- the OBD protocol this car uses (e.g. J1850 VPW, J1850 PWM, ISO 9141, KWP2000, CAN)
- known problems for this car/engine that affect idle, fuel trims or codes: symptoms, what \
the live data shows, and what to check
- optional: maker-specific "mode 22" data codes for this car (e.g. GM enhanced PIDs such as \
knock retard or IAC counts), only with a source that gives the PID and its formula.

Signal ids for "signal" (use these for OBD-II cars):
{signals}

Reply with ONLY one JSON object in a ```json block, in exactly this format:

```json
{{
  "schema": "{SCHEMA}",
  "vehicle": {{"name": "{car}", "years": "", "make": "", "model": "", "engine": "",
              "engine_code": "", "notes": ""}},
  "connection": {{"type": "obd2", "protocol": "", "notes": ""}},
  "normals": {{
    "rpm_warm_idle": {{"label": "Warm idle speed", "signal": "rpm", "unit": "rpm",
                      "low": 600, "high": 800, "condition": "warm idle in Park",
                      "short": "warm idle", "limits": {{}}, "confidence": "owners",
                      "note": "what the sources say", "sources": ["https://..."]}}
  }},
  "known_issues": [{{"title": "", "symptoms": "", "what_data_shows": "", "checks": "",
                    "sources": ["https://..."]}}],
  "enhanced_pids": [],
  "research": {{"by": "Claude", "date": "YYYY-MM-DD", "notes": "gaps and caveats"}}
}}
```

Rules: "low"/"high" are numbers or null; "limits" holds named numbers (e.g. \
"code_sets_above": 25); units are rpm, F, g/s, kPa, %, deg, V, psi, mph; temperatures in F; \
fuel trims in percent; enhanced PID formulas may only use A, B, C, D (data bytes), numbers \
and + - * /. Use the car's real name in "vehicle.name"."""


# --------------------------------------------------------- research in the app

RESEARCH_MODEL = "claude-opus-5-5"


def research_with_claude(client, year, make, model, engine="", extra="", on_progress=None,
                         max_continuations=6):
    """Have Claude research a car with web search and return a validated profile.

    on_progress(text) gets short status updates. Returns (profile, cost_usd).
    Raises ProfileError if Claude's answer can't be turned into a valid profile.
    """
    import claude_assistant

    def note(t):
        if on_progress:
            on_progress(t)

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 20}]
    messages = [{"role": "user", "content": research_prompt(year, make, model, engine, extra)}]
    cost, text = 0.0, ""
    for _ in range(max_continuations + 1):
        with client.beta.messages.stream(
            model=RESEARCH_MODEL, max_tokens=32000, messages=messages, tools=tools,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            for event in stream:
                if (event.type == "content_block_start"
                        and getattr(event.content_block, "type", "") == "server_tool_use"):
                    note("Searching the web…")
            msg = stream.get_final_message()
        cost += claude_assistant.estimate_cost(msg.usage)
        text += "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "pause_turn":
            # Server-side search loop paused: send it back as-is to continue.
            messages = messages[:1] + [{"role": "assistant", "content": msg.content}]
            note("Still researching…")
            continue
        if msg.stop_reason == "refusal":
            raise ProfileError("Claude declined this research request.")
        break
    note("Checking the profile…")
    try:
        return validate(extract_json(text)), cost
    except ProfileError as first:
        note("Fixing the profile format…")
        fix = client.beta.messages.create(
            model=RESEARCH_MODEL, max_tokens=32000, output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content":
                       "This vehicle profile JSON has problems: " + str(first)
                       + "\n\nReturn the corrected JSON only, in a ```json block, keeping "
                         "every value and source that is valid and dropping entries you "
                         "can't fix.\n\n" + text}])
        cost += claude_assistant.estimate_cost(fix.usage)
        fixed = "".join(b.text for b in fix.content if b.type == "text")
        return validate(extract_json(fixed)), cost
