"""Ask Claude about the engine data (the app's "Ask Claude" tab).

Builds a compact text summary of the readings (per-value stats, a time series,
trouble codes, ECM status flags and the rule-based Analysis findings), sends it
to the Claude API and streams the answer back. Follow-up questions continue
the same conversation.

Needs an Anthropic API key (console.anthropic.com). Usage is billed to that
key; nothing is sent until the user clicks a button in the app.
"""

import statistics

import analysis

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_ROWS = 240                       # time-series rows sent per request
PRICE_IN, PRICE_OUT = 4.00, 20.00    # USD per million tokens for MODEL
KEYRING_SERVICE, KEYRING_USER = "Fiero ALDL", "anthropic-api-key"

SYSTEM_PROMPT = """\
You are helping the owner of a 1985-88 Pontiac Fiero with the 2.8L V6 (L44, VIN 9, \
multi-port injection, ECM 1227170) diagnose their engine from live ALDL data, read \
through their own app. They are a hands-on owner, not a professional technician.

The data is decoded with GM's Data Stream A29 specification (160 baud, pin E). \
Notes on the values:
- MAP is the sensor voltage, not kPa. TPS is volts. O2 is millivolts.
- BLM (block learn, long-term) and INT (integrator, short-term) are fuel corrections \
where 128 means no correction; above 128 the ECM is adding fuel (it sees lean), below \
128 it is removing fuel.
- IAC position is the idle air control motor's step count.
- Status flags come from the ECM: closed loop, rich/lean, Park/Neutral, A/C, and \
whether it is in ALDL (10k) or diagnostic (A-B shorted) mode. Diagnostic mode changes \
how the ECM runs the engine.
- Trouble codes are the ECM's malfunction flags (codes 12-55).
- "Rule findings" come from the app's simple threshold checks (typical GM ranges of \
the era, not factory Fiero specs). Treat them as hints, not conclusions.

How to answer:
- Base your reasoning on the numbers given, and quote the specific readings that \
support each point. Say plainly when the data can't settle something or a value \
looks implausible (for example a possible decoding or sensor fault).
- Rank likely causes, then give checks in a sensible order (cheap and quick first), \
saying what result would confirm or rule out each cause.
- Mention safety where it matters (fuel, hot or moving parts, jacking the car).
- Be concise and use short headings or lists. The reader is on a laptop in the garage.
"""


def _fmt(v, dec=2):
    if v is None:
        return ""
    return f"{v:.{dec}f}".rstrip("0").rstrip(".") if dec else f"{v:.0f}"


def build_data_summary(history, params, title, findings=None):
    """Plain-text summary of a history [(t, {name: value, "_codes", "_flags"})]."""
    names = [n for n, p in params.items()
             if p.get("offset", 0) is not None
             and any(v.get(n) is not None for _, v in history)]
    lines = [f"# {title}"]
    if not history:
        return "\n".join(lines + ["(no data)"])
    t0, t1 = history[0][0], history[-1][0]
    lines.append(f"{len(history)} frames over {t1 - t0:.0f} s.")

    lines.append("\n## Summary per value (min / median / max, units)")
    for n in names:
        vals = [v[n] for _, v in history if v.get(n) is not None]
        dec = params[n].get("decimals", 1)
        lines.append(f"- {n}: {_fmt(min(vals), dec)} / {_fmt(statistics.median(vals), dec)} / "
                     f"{_fmt(max(vals), dec)} {params[n].get('units', '')}".rstrip())

    codes = {}
    for _, v in history:
        for c, text in v.get("_codes") or []:
            codes[c] = text
    flag_rows = [v["_flags"] for _, v in history if v.get("_flags")]
    if flag_rows or codes:
        lines.append("\n## ECM trouble codes")
        lines.append(", ".join(f"{c} ({t})" for c, t in sorted(codes.items())) or "none")
    if flag_rows:
        lines.append("\n## ECM status flags (share of frames where on)")
        for name in flag_rows[-1]:
            share = sum(1 for f in flag_rows if f.get(name)) / len(flag_rows)
            lines.append(f"- {name}: {share:.0%}")

    if findings:
        lines.append("\n## Rule findings from the app")
        for f in findings:
            lines.append(f"- [{f.severity}] {f.title}. {f.detail}".rstrip())

    step = max(1, -(-len(history) // MAX_ROWS))
    lines.append(f"\n## Time series (every {step} frame{'s' if step > 1 else ''}; "
                 "t = seconds; trouble codes and flags omitted)")
    lines.append(",".join(["t"] + names))
    for t, v in history[::step]:
        lines.append(",".join([f"{t - t0:.1f}"]
                              + [_fmt(v.get(n), params[n].get("decimals", 1)) for n in names]))
    return "\n".join(lines)


def first_question(summary):
    return (f"{summary}\n\n"
            "My Fiero's idle hunts (it drops toward stalling, recovers, and sometimes dies), "
            "cold and warm. Based on this data, what do you see, what are the most likely "
            "causes, and what should I check first?")


def estimate_cost(usage):
    """Rough USD cost of one response, from its usage numbers."""
    if usage is None:
        return 0.0
    inp = (getattr(usage, "input_tokens", 0) or 0) \
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25 \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    out = getattr(usage, "output_tokens", 0) or 0
    return inp * PRICE_IN / 1e6 + out * PRICE_OUT / 1e6


# ---------------------------------------------------------------- API key

def load_api_key():
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        return None


def save_api_key(key):
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def delete_api_key():
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


# ---------------------------------------------------------- conversation

class Conversation:
    """One chat with Claude about the data. Not thread-safe: one ask() at a time."""

    def __init__(self, api_key=None, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client
        self.messages = []
        self.total_cost = 0.0

    def ask(self, text, on_text):
        """Send `text`, call on_text(chunk) as the answer streams in.

        Returns (answer_text, stop_reason, cost_usd). Raises the SDK's errors.
        """
        self.messages.append({"role": "user", "content": text})
        try:
            with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=self.messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                # If a safety classifier declines, retry on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for chunk in stream.text_stream:
                    on_text(chunk)
                message = stream.get_final_message()
        except Exception:
            self.messages.pop()   # keep the history valid for the next try
            raise
        # Keep the full content (including thinking blocks) for follow-ups.
        self.messages.append({"role": "assistant", "content": message.content})
        cost = estimate_cost(message.usage)
        self.total_cost += cost
        answer = "".join(b.text for b in message.content if b.type == "text")
        return answer, message.stop_reason, cost


def describe_error(exc):
    """Friendly one-line message for SDK errors (no API key in the text)."""
    try:
        import anthropic
    except ImportError:
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, anthropic.AuthenticationError):
        return "The API key was rejected. Check it under 'API key…'."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key isn't allowed to use the model."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the API. Wait a minute and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The request was rejected: {getattr(exc, 'message', exc)}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Anthropic's API had a server error ({exc.status_code}). Try again shortly."
        return f"API error ({exc.status_code}): {getattr(exc, 'message', exc)}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check the internet connection."
    return f"{type(exc).__name__}: {exc}"


def summary_for_live(history, params):
    findings = analysis.analyze(list(history), params)
    return build_data_summary(list(history), params, "Live data (last ~60 s)", findings)


def summary_for_log(path, fallback_params):
    hist, params = analysis.load_log_params(path, fallback=fallback_params)
    findings = analysis.analyze(hist, params, window=None)
    import os
    return build_data_summary(hist, params, f"Recorded log {os.path.basename(path)}", findings)


def offline_check():
    """Stream one reply through the real SDK against a local mock server.

    Used by the app's self-test so a packaged app proves the whole Claude
    path works (SDK modules bundled, request built, stream parsed) without a
    key or network. Returns the streamed text.
    """
    import json

    import anthropic
    import httpx2

    def handler(request):
        events = [
            {"type": "message_start", "message": {
                "id": "msg_check", "type": "message", "role": "assistant", "model": MODEL,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "offline check ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=body.encode())

    client = anthropic.Anthropic(
        api_key="offline-check", base_url="https://offline.invalid",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    answer, _, _ = Conversation(client=client).ask("check", lambda s: None)
    return answer
