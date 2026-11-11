"""Tests for claude_assistant.py. No real API calls are made.

Run:  python -m unittest test_claude_assistant -v
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import aldl_logger as al
import analysis
import claude_assistant as ca


def fiero_history(seconds=30):
    d = al.load_definition(al.DEFAULT_DEF)
    sim = al.SimSource()
    hist = []
    for i in range(int(seconds / 0.5)):
        frame = bytearray(sim.frame(100 + i * 0.5))
        frame[12] |= 0x20  # trouble code 32
        rows = al.decode(bytes(frame), d)
        codes, flags = al.decode_status(bytes(frame), d)
        v = {n: x for n, x, _, _ in rows}
        v["_codes"], v["_flags"] = codes, flags
        hist.append((i * 0.5, v))
    return hist, {p["name"]: p for p in d["parameters"]}


class SummaryTests(unittest.TestCase):
    def test_summary_has_stats_codes_flags_findings_and_series(self):
        hist, params = fiero_history()
        s = ca.summary_for_live(hist, params)
        self.assertIn("Engine RPM:", s)
        self.assertIn("MAP:", s)
        self.assertIn(" V", s)                          # units carried through
        self.assertIn("32 (Exhaust gas recirculation (EGR) failure)", s)
        self.assertIn("Closed loop:", s)
        self.assertIn("Rule findings", s)
        self.assertIn("Idle is hunting", s)
        self.assertIn("t,Engine RPM", s)

    def test_long_history_is_downsampled(self):
        hist, params = fiero_history(seconds=600)       # 1200 frames
        s = ca.build_data_summary(hist, params, "x")
        series = s.split("## Time series")[1].strip().splitlines()[2:]
        self.assertLessEqual(len(series), ca.MAX_ROWS)

    def test_log_summary(self):
        hist, params = fiero_history()
        d = al.load_definition(al.DEFAULT_DEF)
        path = os.path.join(tempfile.mkdtemp(), "log.csv")
        import csv
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "elapsed_s"] + [al.column_name(p) for p in d["parameters"]]
                       + ["trouble_codes", "status_flags", "raw_hex"])
            for t, v in hist:
                w.writerow(["x", t] + [f"{v[p['name']]:.3f}" for p in d["parameters"]]
                           + [al.codes_text(v["_codes"]), al.flags_text(v["_flags"]), "00"])
        s = ca.summary_for_log(path, params)
        self.assertIn("Recorded log log.csv", s)
        self.assertIn("32 (Exhaust gas recirculation (EGR) failure)", s)


class FakeStream:
    def __init__(self, chunks, message, fail=False):
        self.chunks, self.message, self.fail = chunks, message, fail

    def __enter__(self):
        if self.fail:
            raise RuntimeError("network down")
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self.chunks)

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self):
        self.calls, self.fail_next = [], False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kw):
        kw["messages"] = list(kw["messages"])   # snapshot, like a real request
        self.calls.append(kw)
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text="Check the EGR valve.")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0))
        fail, self.fail_next = self.fail_next, False
        return FakeStream(["Check the ", "EGR valve."], msg, fail)


class ConversationTests(unittest.TestCase):
    def test_request_shape_streaming_and_followups(self):
        client = FakeClient()
        conv = ca.Conversation(client=client)
        got = []
        answer, stop, cost = conv.ask("question 1", got.append)
        self.assertEqual("".join(got), "Check the EGR valve.")
        self.assertEqual(answer, "Check the EGR valve.")
        self.assertEqual(stop, "end_turn")
        self.assertAlmostEqual(cost, 1000 * 4 / 1e6 + 500 * 20 / 1e6)
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"], {"effort": "medium"})
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", kw["betas"])
        self.assertNotIn("thinking", kw)               # Opus 5.5: adaptive by default
        conv.ask("question 2", lambda s: None)
        roles = [m["role"] for m in client.calls[1]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        # Full content (thinking block included) is passed back unchanged.
        self.assertEqual(client.calls[1]["messages"][1]["content"][0].type, "thinking")

    def test_failed_request_leaves_history_valid(self):
        client = FakeClient()
        conv = ca.Conversation(client=client)
        client.fail_next = True
        with self.assertRaises(RuntimeError):
            conv.ask("q", lambda s: None)
        self.assertEqual(conv.messages, [])
        conv.ask("q again", lambda s: None)
        self.assertEqual([m["role"] for m in conv.messages], ["user", "assistant"])


try:
    import anthropic  # noqa: F401
    import httpx2
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@unittest.skipUnless(HAVE_SDK, "anthropic SDK not installed")
class RealSdkTests(unittest.TestCase):
    """The real SDK against a local mock transport: checks the request it
    builds and that it parses a streamed reply. Nothing leaves the machine."""

    def test_real_sdk_request_and_stream(self):
        import anthropic
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            events = [
                {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1200, "output_tokens": 1}}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "Likely a vacuum leak. "}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "Check the MAP hose."}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                    "stop_sequence": None},
                 "usage": {"output_tokens": 300}},
                {"type": "message_stop"},
            ]
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=_sse(events))

        client = anthropic.Anthropic(
            api_key="sk-test", base_url="https://mock.invalid",
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
        conv = ca.Conversation(client=client)
        chunks = []
        answer, stop, cost = conv.ask("Is my idle OK?", chunks.append)

        self.assertEqual(answer, "Likely a vacuum leak. Check the MAP hose.")
        self.assertEqual("".join(chunks), answer)
        self.assertEqual(stop, "end_turn")
        self.assertGreater(cost, 0)
        body = seen["body"]
        self.assertTrue(seen["url"].endswith("/v1/messages?beta=true")
                        or seen["url"].endswith("/v1/messages"))
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertTrue(body["stream"])
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertIn("server-side-fallback-2026-07-01", seen["headers"]["anthropic-beta"])
        self.assertIn("Fiero", body["system"])
        self.assertEqual(body["messages"], [{"role": "user", "content": "Is my idle OK?"}])


if __name__ == "__main__":
    unittest.main()
