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
     mock.patch.dict(os.environ, env):
    ul.main()
s = open(summ).read()
assert "✅ Search answered" in s and "model returned **1** candidate(s): **1** already on the site, **0** rejected (link is not a numeric ad URL), **0** not available or unverifiable, **0** new" in s and "already known" in s, s
o = dict(line.rstrip("\n").split("=", 1) for line in open(outp))
assert "model returned 1 (1 already known, 0 rejected bad link, 0 not available/unverifiable, 0 new" in o["search_report"] and "\n" not in o["search_report"], o

# ── 4. link shape, url normalisation, unique ids
ok = ul.ad_url_ok
for u in ("https://www.immobiliare.it/annunci/131084574/", "https://www.immobiliare.it/en/annunci/121264012/", "https://www.idealista.it/immobile/34352825/",
          "https://www.wikicasa.it/annuncio/24382672", "https://www.wikicasa.it/en/annuncio/24382672", "https://www.casa.it/immobili/54343667/",
          "https://casa.it/immobili/54343667/?x=1"):
    assert ok(u), u
for u in ("https://www.casa.it/immobili/lungo-dora-voghera-94/", "https://www.casa.it/immobili/via-montebello-29/",      # the two fabricated links
          "http://www.casa.it/immobili/54343667/", "https://evil.test/immobili/54343667/", "https://casa.it.evil.test/immobili/54343667/",
          "https://www.immobiliare.it/vendita-case/torino/", "https://www.immobiliare.it/annunci/12/", "", None, "https://www.casa.it/immobili/54343667x/"):
    assert not ok(u), u
n = ul.norm_url
assert n("https://www.immobiliare.it/en/annunci/121264012/?a=1#x") == n("http://immobiliare.it/annunci/121264012") == "immobiliare.it/annunci/121264012"
assert n("https://www.idealista.it/immobile/1/") != n("https://www.idealista.it/immobile/2/")

mk = lambda i: {"id": i}
new_l = [mk("v1"), mk("v1"), mk("v20"), mk(""), mk("zz"), {"id": None}]
ren = ul.ensure_unique_ids(new_l, {"v1", "v2", "v20"})
ids = [l["id"] for l in new_l]
assert ids[0] == "v21" and ids[1] == "v22" and ids[2] == "v23" and ids[4] == "zz", ids
assert len(set(ids) | {"v1", "v2", "v20"}) == 9 and all(ids), ids
assert ("v1", "v21") in ren

# ── 5. main(): fabricated link rejected and reported, colliding id renamed, link-seen flags in the report
shutil.copy(FIXTURE, path); open(summ, "w").close(); open(outp, "w").close()
existing = ul.extract_existing_listings(open(path, encoding="utf-8").read())
mkc = lambda i, addr, url: {"id": i, "hood": "Cavoretto", "addr": addr, "url": url, "floor": "1", "lat": 45.04, "lng": 7.71, "price": 300000, "type": "Trilocale",
                            "m2": 100, "outdoor": "Giardino", "outdoorType": "giardino", "outdoorNote": "x", "naturaNote": "x", "desc": "x", "img": "",
                            "src": "casa.it", "top": True, "published": "2026-10-23"}
good = mkc(existing[0]["id"], "Via Reale 1", "https://www.casa.it/immobili/55555555/")          # id collides with an existing listing
seen_not = mkc("c90", "Via Inventata 2", "https://www.casa.it/immobili/66666666/")               # right shape, but never in search results
slug = mkc("v91", "Via Slug 3", "https://www.casa.it/immobili/via-slug-3/")
def fake_search(_):
    ul.LAST_SEARCH.clear(); ul.LAST_SEARCH.update(stop_reason="end_turn", web_searches=2, search_errors=[], result_hosts=[("casa.it", 1)], text_blocks=1,
        text_chars=1, tokens_in=1, tokens_out=1, continuations=0, preview="", result_urls={ul.norm_url("https://www.casa.it/immobili/55555555/")})
    return [good, seen_not, slug]
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", side_effect=fake_search), \
     mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), \
     mock.patch.object(ul.availability, "verify", side_effect=lambda client, ls: (ls, [], {"evidence": {}})), mock.patch.dict(os.environ, env):
    ul.main()
final = {l["id"]: l for l in ul.extract_existing_listings(open(path, encoding="utf-8").read())}
by_addr = {l["addr"]: l for l in final.values()}
assert "Via Slug 3" not in by_addr, "slug link was accepted"
assert by_addr["Via Reale 1"]["id"] != existing[0]["id"] and len(final) == len(existing) + 2 and len({*final}) == len(final)
assert all(by_addr[a]["top"] in (True, False) for a in ("Via Reale 1", "Via Inventata 2"))
s = open(summ).read()
assert "**1** rejected" in s and "rejected: Via Slug 3" in s and "link seen in search results" in s and "link NOT in search results" in s, s
o = dict(line.rstrip("\n").split("=", 1) for line in open(outp))
assert "1 rejected bad link" in o["search_report"] and "1 of them with link seen in search results" in o["search_report"], o

# ── 6. rotating queries, prompt template, and the "check by hand" list
import datetime as _dt, re as _re
d1, d2 = _dt.date(2026, 10, 24), _dt.date(2026, 10, 25)
q1, q2 = ul.daily_queries(d1), ul.daily_queries(d2)
assert len(q1) == 8 and len(set(q1)) == 8 and q1 == ul.daily_queries(d1), q1
assert all("in vendita" in q and "Torino" in q and "site:casa.it" not in q for q in q1 + q2), q1
pair = lambda q: (q.split()[0], next(h for h in ul.SEARCH_HOODS if f" {h} Torino" in q))
assert len({pair(q) for q in q1 + q2}) == len(ul.SEARCH_PORTALS) * len(ul.SEARCH_HOODS), "two days must cover every portal x hood pair"
prompt = ul.SEARCH_PROMPT.format(existing_json="[]", today="2026-10-24", queries="\n".join(f"- {q}" for q in q1))
assert all(q in prompt for q in q1) and "180,000" in prompt and "200,000" not in prompt and "{queries}" not in prompt
assert "nuda proprietà" in prompt and "FOR SALE" in prompt

shutil.copy(FIXTURE, path); open(summ, "w").close(); open(outp, "w").close()
existing = ul.extract_existing_listings(open(path, encoding="utf-8").read())
unread = mkc("c91", "Via Illeggibile 4", "https://www.idealista.it/immobile/77777777/"); unread["price"] = 185000
dead = mkc("c92", "Via Venduta 5", "https://www.immobiliare.it/annunci/88888888/")
def one_search(_):
    ul.LAST_SEARCH.clear(); ul.LAST_SEARCH.update(stop_reason="end_turn", web_searches=1, search_errors=[], result_hosts=[], text_blocks=1,
        text_chars=1, tokens_in=1, tokens_out=1, continuations=0, preview="", result_urls=set())
    return [unread, dead]
rej = lambda client, ls: ([], [(ls[0], "page not fetchable (url_not_accessible): removed or unreadable by the fetcher, cannot verify"),
                               (ls[1], "sold_or_removed: Venduto il 01/09/2025")], {"evidence": {}})
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", side_effect=one_search), \
     mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), \
     mock.patch.object(ul.availability, "verify", side_effect=rej), mock.patch.dict(os.environ, env):
    ul.main()
s = open(summ).read()
hand = s.split("Please check by hand")[1]
assert "Via Illeggibile 4" in hand and "€185,000" in hand and "https://www.idealista.it/immobile/77777777/" in hand, s
assert "Via Venduta 5" not in hand, "a listing the page showed as sold must not be in the check-by-hand list"
assert len(ul.extract_existing_listings(open(path, encoding="utf-8").read())) == len(existing), "nothing unverified may be added"

shutil.rmtree(tmp)
print("ALL SEARCH TESTS PASSED")
