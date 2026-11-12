"""Tests for claude_agent.py with a fake API client.   Run:  python -m unittest test_claude_agent -v"""

import unittest
from types import SimpleNamespace as NS

import claude_agent as cag


def usage():
    return NS(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter([b.text for b in self.msg.content if b.type == "text"])

    def get_final_message(self):
        return self.msg


def client(replies, calls):
    def stream(**kw):
        calls.append({**kw, "messages": list(kw["messages"])})
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeStream(r)
    return NS(beta=NS(messages=NS(stream=stream)))


class AgentTests(unittest.TestCase):
    def test_runs_tools_then_answers(self):
        calls, seen = [], []
        replies = [NS(content=[text("Let me look."), tool("t1", "get_live_snapshot", {}),
                               tool("t2", "get_test_result", {"test_id": "warm_idle"})],
                      stop_reason="tool_use", usage=usage()),
                   NS(content=[text("Idle is hunting; spray the EGR plate.")], stop_reason="end_turn",
                      usage=usage())]
        handlers = {"get_live_snapshot": lambda a: "RPM 600-1150",
                    "get_test_result": lambda a: {"status": "fail"}}
        agent = cag.Agent(handlers, client=client(replies, calls))
        out = []
        answer, stop, cost = agent.ask("What's wrong?", out.append, lambda n, a: seen.append(n))
        self.assertEqual(stop, "end_turn")
        self.assertIn("spray the EGR plate", answer)
        self.assertEqual(seen, ["get_live_snapshot", "get_test_result"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tools"], cag.TOOLS)
        self.assertEqual(calls[0]["tool_choice"], {"type": "auto"})
        self.assertEqual(calls[0]["model"], "claude-opus-5-5")
        results = calls[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])
        self.assertEqual(results[0]["content"], "RPM 600-1150")
        self.assertIn('"fail"', results[1]["content"])
        self.assertIn("diagnostic partner", agent.system)
        self.assertGreater(cost, 0)

    def test_bad_input_and_failing_tool_are_reported_not_raised(self):
        agent = cag.Agent({"start_test": lambda a: 1 / 0}, client=None or NS())
        self.assertTrue(agent.run_tool("start_test", {"test_id": ""})[1])
        self.assertTrue(agent.run_tool("clear_codes", {})[1])            # no such tool
        out, err = agent.run_tool("start_test", {"test_id": "x"})
        self.assertTrue(err)
        self.assertIn("failed", out)
        self.assertNotIn("clear_codes", cag.TOOL_NAMES)

    def test_error_rolls_back_history(self):
        calls = []
        agent = cag.Agent({}, client=client([RuntimeError("boom")], calls))
        with self.assertRaises(RuntimeError):
            agent.ask("hi", lambda c: None)
        self.assertEqual(agent.messages, [])

    def test_round_limit(self):
        loop = [NS(content=[tool(f"t{i}", "list_tests", {})], stop_reason="tool_use", usage=usage())
                for i in range(cag.MAX_ROUNDS)]
        agent = cag.Agent({"list_tests": lambda a: []}, client=client(loop, []))
        out = []
        agent.ask("go", out.append)
        self.assertIn("Stopped after several lookups", "".join(out))


if __name__ == "__main__":
    unittest.main()
