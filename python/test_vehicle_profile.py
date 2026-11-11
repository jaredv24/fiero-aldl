"""Tests for vehicle_profile.py.   Run:  python -m unittest test_vehicle_profile -v"""

import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import normals
import vehicle_profile as vp


def builtin(name_part):
    return next(p for _, p in vp.list_profiles() if name_part in p["vehicle"]["name"])


class ValidationTests(unittest.TestCase):
    def test_builtin_profiles_are_valid(self):
        names = [p["vehicle"]["name"] for _, p in vp.list_profiles()]
        self.assertTrue(any("Fiero" in n for n in names))
        self.assertTrue(any("Grand Prix GTP" in n for n in names))

    def test_fiero_normals_still_load(self):
        self.assertEqual(normals.get("iac_warm_idle")["high"], 40)
        self.assertEqual(normals.limit("tps_closed", "fail_high"), 1.25)

    def test_rejects_unsafe_formula(self):
        p = copy.deepcopy(builtin("GTP"))
        p["enhanced_pids"] = [{"name": "x", "mode": "22", "pid": "1234", "bytes": 1,
                               "formula": "__import__('os').system('rm -rf ~')", "unit": ""}]
        with self.assertRaises(vp.ProfileError):
            vp.validate(p)

    def test_accepts_and_evaluates_safe_formula(self):
        code = vp.check_formula("(A*256+B)/4")
        self.assertEqual(vp.eval_formula(code, [0x0B, 0xB8]), 750)

    def test_sourced_confidence_needs_sources(self):
        p = copy.deepcopy(builtin("GTP"))
        p["normals"]["maf_idle"]["sources"] = []
        with self.assertRaisesRegex(vp.ProfileError, "maf_idle"):
            vp.validate(p)

    def test_rejects_bad_links_and_unknown_signal(self):
        p = copy.deepcopy(builtin("GTP"))
        p["normals"]["ltft"]["sources"] = ["javascript:alert(1)"]
        p["normals"]["stft"]["signal"] = "warp_drive"
        with self.assertRaises(vp.ProfileError) as cm:
            vp.validate(p)
        self.assertIn("ltft", str(cm.exception))
        self.assertIn("warp_drive", str(cm.exception))

    def test_rejects_low_above_high(self):
        p = copy.deepcopy(builtin("GTP"))
        p["normals"]["maf_idle"]["low"] = 9
        with self.assertRaisesRegex(vp.ProfileError, "low is above high"):
            vp.validate(p)

    def test_extract_json_from_chatty_reply(self):
        p = builtin("GTP")
        reply = "Here's what I found.\n\n```json\n" + json.dumps(p) + "\n```\nHope it helps!"
        self.assertEqual(vp.validate(vp.extract_json(reply))["vehicle"]["name"],
                         p["vehicle"]["name"])

    def test_import_copies_into_user_folder(self):
        src = os.path.join(tempfile.mkdtemp(), "mine.json")
        with open(src, "w") as f:
            json.dump(builtin("GTP"), f)
        user = tempfile.mkdtemp()
        dest, prof = vp.import_profile(src, user)
        self.assertTrue(os.path.exists(dest))
        self.assertIn(dest, [path for path, _ in vp.list_profiles(user)])

    def test_import_rejects_invalid_file(self):
        src = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(src, "w") as f:
            f.write('{"schema": "something else"}')
        with self.assertRaises(vp.ProfileError):
            vp.import_profile(src, tempfile.mkdtemp())

    def test_research_prompt_mentions_car_and_schema(self):
        text = vp.research_prompt("2002", "Pontiac", "Grand Prix GTP", "3.8 SC")
        self.assertIn("2002 Pontiac Grand Prix GTP 3.8 SC", text)
        self.assertIn(vp.SCHEMA, text)
        self.assertIn("Never invent a number", text)


class FakeStream:
    def __init__(self, msg, events):
        self.msg, self.events = msg, events

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.msg


def msg(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=1000,
                                                 cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


class ResearchTests(unittest.TestCase):
    def make_client(self, replies, fix_reply=None):
        calls = {"stream": [], "create": []}

        def stream(**kw):
            calls["stream"].append(kw)
            m = replies.pop(0)
            ev = [SimpleNamespace(type="content_block_start",
                                  content_block=SimpleNamespace(type="server_tool_use"))]
            return FakeStream(m, ev)

        def create(**kw):
            calls["create"].append(kw)
            return fix_reply
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            stream=stream, create=create)))
        return client, calls

    def test_research_handles_pause_and_returns_valid_profile(self):
        good = "```json\n" + json.dumps(builtin("GTP")) + "\n```"
        client, calls = self.make_client([msg("", stop="pause_turn"), msg(good)])
        notes = []
        prof, cost = vp.research_with_claude(client, "2002", "Pontiac", "Grand Prix GTP",
                                             on_progress=notes.append)
        self.assertIn("Grand Prix", prof["vehicle"]["name"])
        self.assertEqual(len(calls["stream"]), 2)               # resumed after pause_turn
        tool = calls["stream"][0]["tools"][0]
        self.assertEqual(tool["type"], "web_search_20260209")
        self.assertEqual(calls["stream"][0]["model"], "claude-opus-5-5")
        self.assertEqual(calls["stream"][1]["messages"][-1]["role"], "assistant")
        self.assertIn("Searching the web…", notes)
        self.assertGreater(cost, 0)

    def test_research_repairs_an_invalid_reply_once(self):
        bad = copy.deepcopy(builtin("GTP"))
        bad["normals"]["maf_idle"]["sources"] = []
        good = "```json\n" + json.dumps(builtin("GTP")) + "\n```"
        client, calls = self.make_client(["unused"] and [msg("```json\n" + json.dumps(bad) + "\n```")],
                                         fix_reply=msg(good))
        prof, _ = vp.research_with_claude(client, "2002", "Pontiac", "Grand Prix GTP")
        self.assertEqual(len(calls["create"]), 1)
        self.assertIn("maf_idle", calls["create"][0]["messages"][0]["content"])
        self.assertTrue(prof["normals"]["maf_idle"]["sources"])


if __name__ == "__main__":
    unittest.main()
