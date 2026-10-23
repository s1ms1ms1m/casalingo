#!/usr/bin/env python3
"""Tests for the search step (parse_candidates, call_claude, SearchError, summary report).
Run: python3 scripts/test_search.py   -- no network: a fake Anthropic client returns canned replies.
Reply shapes are modelled on what web search produces (narration text blocks between server_tool_use /
web_search_tool_result blocks); the exact live shapes are NOT verified from here, so every attribute is read defensively.
"""
import json, os, shutil, sys, tempfile, types
from types import SimpleNamespace as NS
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.dont_write_bytecode = True
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(__version__="stub", Anthropic=object)
import update_listings as ul

C1 = {"id": "v20", "addr": "Via Nuova 1", "url": "https://immobiliare.it/a"}
C2 = {"id": "s9", "addr": "Via Altra 2", "url": "https://idealista.it/b"}
J = json.dumps([C1, C2])

# ── 1. parse_candidates
P = ul.parse_candidates
assert P([J]) == [C1, C2]
assert P(["```json\n" + J + "\n```"]) == [C1, C2]
assert P(["Let me search for listings...", J]) == [C1, C2]                       # narration in separate blocks
assert P(["Let me search...\nHere is the result:\n" + J]) == [C1, C2]            # narration in the SAME block
assert P(["Searching [1] and [2]...", J]) == [C1, C2]                            # bracketed noise ignored
assert P(["I looked at everything.", "[]"]) == []                                # clean "nothing"
assert P(["Found nothing new, so: []"]) == []
assert P([J + "\nHope that helps."]) == [C1, C2]                                 # trailing chatter
assert P(["first try: " + json.dumps([C1]), "final: " + J]) == [C1, C2]          # last array wins
assert P(["[1, 2, 3]"]) is None                                                  # not objects
assert P([J[:60]]) is None                                                       # truncated
assert P(["I could not find any listings today."]) is None                       # prose only
assert P([]) is None and P(["", "  "]) is None

# ── 2. call_claude with a fake client
txt = lambda t: NS(type="text", text=t)
use = NS(type="server_tool_use", name="web_search")
res = lambda *urls: NS(type="web_search_tool_result", content=[NS(url=u) for u in urls])
err = lambda code: NS(type="web_search_tool_result", content=NS(error_code=code))
def reply(content, stop="end_turn", i=100, o=50): return NS(content=content, stop_reason=stop, usage=NS(input_tokens=i, output_tokens=o))

class Fake:
    queue, calls = [], []
    def __init__(self, api_key=None): self.beta = NS(messages=NS(create=self._create))
    def _create(self, **kw):
        Fake.calls.append(kw); return Fake.queue.pop(0)
def run(replies):
    Fake.queue, Fake.calls = list(replies), []
    with mock.patch.object(ul.anthropic, "Anthropic", Fake), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
        return ul.call_claude([{"url": "u", "addr": "a"}])

out = run([reply([txt("Searching immobiliare..."), use, res("https://www.immobiliare.it/x", "https://idealista.it/y"),
                  txt("Now checking more."), use, err("max_uses_exceeded"), txt(J)])])
assert out == [C1, C2]
d = ul.LAST_SEARCH
assert d["stop_reason"] == "end_turn" and d["web_searches"] == 2 and d["search_errors"] == ["max_uses_exceeded"]
assert dict(d["result_hosts"]) == {"immobiliare.it": 1, "idealista.it": 1} and d["text_blocks"] == 3 and (d["tokens_in"], d["tokens_out"]) == (100, 50)
assert Fake.calls[0]["max_tokens"] >= 8192

assert run([reply([txt("Nothing new."), txt("[]")])]) == []                       # clean empty is NOT an error

def must_fail(replies, needle):
    try: run(replies)
    except ul.SearchError as e:
        assert needle in str(e), str(e); return
    raise SystemExit(f"expected SearchError({needle})")
must_fail([reply([txt("I could not find any listings today.")])], "no JSON array")        # the old code returned [] here
must_fail([reply([txt("Let me search..."), txt(J[:70])], stop="max_tokens")], "max_tokens")
must_fail([reply([use, res("https://x.it")], stop="end_turn")], "no text")
must_fail([reply([txt("hmm")], stop="refusal")], "refusal")
assert ul.LAST_SEARCH["stop_reason"] == "refusal"

# complete JSON despite max_tokens is accepted (with a warning)
assert run([reply([txt(J)], stop="max_tokens")]) == [C1, C2]

# pause_turn: resumes, hands back what was produced so far, combines both halves
a1 = [txt("Searching..."), use, res("https://casa.it/z")]
out = run([reply(a1, stop="pause_turn"), reply([txt(J)])])
assert out == [C1, C2] and len(Fake.calls) == 2 and ul.LAST_SEARCH["continuations"] == 1 and ul.LAST_SEARCH["web_searches"] == 1
m = Fake.calls[1]["messages"]; assert [x["role"] for x in m] == ["user", "assistant"] and m[1]["content"] == a1
# never-ending pauses are bounded, then reported
must_fail([reply([txt("..")], stop="pause_turn")] * (ul.MAX_CONTINUATIONS + 1), "pause_turn")
assert len(Fake.calls) == ul.MAX_CONTINUATIONS + 1
# unknown block types / missing usage do not crash
assert run([NS(content=[NS(type="mystery"), txt(J)], stop_reason="end_turn")]) == [C1, C2]
# an API exception still propagates (and is not turned into [])
class Boom(Fake):
    def _create(self, **kw): raise RuntimeError("boom")
with mock.patch.object(ul.anthropic, "Anthropic", Boom), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
    try: ul.call_claude([]); raise SystemExit("API error swallowed")
    except RuntimeError: pass

# ── 3. main(): a broken search fails the run and leaves the site untouched; a good one reports its funnel
FIXTURE = os.path.join(HERE, "fixtures", "index_sample.html")
tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); shutil.copy(FIXTURE, path)
before = open(path, "rb").read(); summ, outp = os.path.join(tmp, "s.md"), os.path.join(tmp, "o.txt")
env = {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": outp, "GITHUB_STEP_SUMMARY": summ}
def broken(_): ul.LAST_SEARCH.update(stop_reason="end_turn", web_searches=3, search_errors=[], result_hosts=[], text_blocks=1, text_chars=9,
                                     tokens_in=1, tokens_out=1, continuations=0, preview="I could not find"); raise ul.SearchError("no JSON array found in the reply")
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", side_effect=broken), mock.patch.dict(os.environ, env):
    try: ul.main(); raise SystemExit("main() survived a broken search")
    except ul.SearchError: pass
assert open(path, "rb").read() == before and "❌ Search failed" in open(summ).read() and "I could not find" in open(summ).read()

existing = ul.extract_existing_listings(open(path, encoding="utf-8").read())
known = {"id": "zz", "addr": existing[0]["addr"], "url": "https://elsewhere.test/1", "src": "casa.it"}
open(summ, "w").close()
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=[known]), \
     mock.patch.object(ul.linkcheck, "check_all", return_value={}), mock.patch.dict(os.environ, env):
    ul.main()
s = open(summ).read()
assert "✅ Search answered" in s and "model returned **1** candidate(s): **1** already on the site, **0** new" in s and "already known" in s, s
shutil.rmtree(tmp)
print("ALL SEARCH TESTS PASSED")
