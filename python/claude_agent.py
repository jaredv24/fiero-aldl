"""Claude as a diagnostic partner: a conversation where Claude can look things
up in the app itself (live readings, codes, guided tests, checks, history)
instead of only reading one summary.

The tools are read-only or ask the owner to do something; there is no tool
to clear codes or change anything on the car.
"""

import json

import claude_assistant as ca

MAX_ROUNDS = 8           # tool round-trips per question, so a confused loop can't run up a bill

AGENT_PROMPT = """

You are working as the owner's diagnostic partner inside the app, with tools that read the
app's live data, trouble codes, guided tests, hands-on checks, leak hunt and vehicle history.
Use them to look before you conclude: check the live snapshot, the codes and the history
rather than guessing. When a guided test would settle a question, start it with start_test
and tell the owner exactly what to do; the test needs them at the car, so end your turn after
starting it and read its result with get_test_result when they come back. Never claim a test
ran or a value was read unless a tool returned it. Work toward the next concrete step, one or
two at a time, and keep safety first (hot parts, moving belts, fire risk from spray)."""

TOOLS = [
    {"name": "get_live_snapshot",
     "description": "Current readings and the last ~60 s summarized (min/avg/max per reading), "
                    "plus the app's automatic findings. Returns 'not connected' if no data.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "read_codes",
     "description": "Trouble codes. OBD-II: stored/pending codes, readiness, freeze frame "
                    "(takes a few seconds). Fiero: codes currently in the data stream.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_tests",
     "description": "Guided tests available for this car: id, name, what it checks, and the "
                    "latest result this session if it has been run.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "start_test",
     "description": "Start a guided test in the app's Tests tab. The owner must follow its steps "
                    "at the car, so after starting one, tell them what to do and end your turn.",
     "input_schema": {"type": "object",
                      "properties": {"test_id": {"type": "string", "description": "id from list_tests"}},
                      "required": ["test_id"], "additionalProperties": False}},
    {"name": "get_test_result",
     "description": "Result of a guided test run this session: status, headline, details, numbers.",
     "input_schema": {"type": "object",
                      "properties": {"test_id": {"type": "string"}},
                      "required": ["test_id"], "additionalProperties": False}},
    {"name": "get_history",
     "description": "This car's saved history: past guided tests, hands-on checks the owner "
                    "recorded (fuel pressure, compression, notes of repairs), trends.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_leak_hunt",
     "description": "Spray-test (leak hunt) results from this session and the before/after "
                    "log comparison if one was made.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def _validate(name, args):
    if name not in TOOL_NAMES:
        return f"Unknown tool {name!r}."
    if not isinstance(args, dict):
        return "Tool input must be an object."
    if name in ("start_test", "get_test_result"):
        tid = args.get("test_id")
        if not isinstance(tid, str) or not tid or len(tid) > 64:
            return "test_id must be a short string from list_tests."
    return None


class Agent(ca.Conversation):
    """Conversation that lets Claude call the app's tools.

    `handlers` maps tool name -> callable(args) -> str. They are called on the
    worker thread; the app wraps them to run on the GUI thread.
    """

    def __init__(self, handlers, api_key=None, client=None, system=None):
        super().__init__(api_key=api_key, client=client, system=(system or ca.SYSTEM_PROMPT) + AGENT_PROMPT)
        self.handlers = handlers

    def run_tool(self, name, args):
        err = _validate(name, args)
        if err:
            return err, True
        try:
            out = self.handlers[name](args)
        except Exception as e:  # report to Claude instead of crashing the chat
            return f"The tool failed: {e}", True
        return out if isinstance(out, str) else json.dumps(out, default=str), False

    def ask(self, text, on_text, on_tool=None):
        """Like Conversation.ask, but runs tool calls until Claude answers.

        on_tool(name, args) is called before each tool runs (for the chat display).
        """
        start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        answer, cost = [], 0.0
        try:
            for _ in range(MAX_ROUNDS):
                with self.client.beta.messages.stream(
                    model=ca.MODEL,
                    max_tokens=ca.MAX_TOKENS,
                    system=self.system,
                    messages=self.messages,
                    tools=TOOLS,
                    tool_choice={"type": "auto"},
                    output_config={"effort": ca.EFFORT},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    for chunk in stream.text_stream:
                        on_text(chunk)
                    message = stream.get_final_message()
                cost += ca.estimate_cost(message.usage)
                self.messages.append({"role": "assistant", "content": message.content})
                answer += [b.text for b in message.content if b.type == "text"]
                if message.stop_reason != "tool_use":
                    break
                results = []
                for block in message.content:
                    if block.type != "tool_use":
                        continue
                    if on_tool:
                        on_tool(block.name, block.input)
                    out, is_error = self.run_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": out[:20000], "is_error": is_error})
                self.messages.append({"role": "user", "content": results})
            else:
                on_text("\n\n(Stopped after several lookups. Ask again to continue.)")
        except Exception:
            del self.messages[start:]       # keep the history valid for the next try
            raise
        self.total_cost += cost
        return "\n".join(answer), message.stop_reason, cost
