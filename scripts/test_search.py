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
# The tests of the web-search side must never read immobiliare.it (or use the network): a stub answers "nothing found".
EMPTY_IMM = {"listings": [], "seen_rows": [], "report": "immobiliare.it read live: stub", "details": [], "tokens": 0}
ul.immobiliare.discover = lambda *a, **k: dict(EMPTY_IMM)

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
assert "model returned 1 (1 already known, 0 rejected bad link, 0 on the removed list, 0 not available/unverifiable, 0 new" in o["search_report"] and "\n" not in o["search_report"], o

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
assert "Via Slug 3 <https://www.casa.it/immobili/via-slug-3/>" in o["rejected_links"], o

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
assert "Sources to search: idealista.it, wikicasa.it, casa.it." in prompt and all("immobiliare" not in q for q in q1 + q2), "immobiliare.it is read directly, not searched"
assert ul.SEARCH_PORTALS == ["idealista.it", "wikicasa.it"]

shutil.copy(FIXTURE, path); open(summ, "w").close(); open(outp, "w").close()
existing = ul.extract_existing_listings(open(path, encoding="utf-8").read())
unread = mkc("c91", "Via Illeggibile 4", "https://www.idealista.it/immobile/77777777/"); unread["price"] = 185000
dead = mkc("c92", "Via Venduta 5", "https://www.wikicasa.it/annuncio/88888888")
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
o = dict(line.rstrip("\n").split("=", 1) for line in open(outp))
assert "Via Illeggibile 4 <https://www.idealista.it/immobile/77777777/>" in o["unreadable"] and "Venduta" not in o["unreadable"], o
assert o["rejected_links"] == "", o

# ── 7. removed-listings list: a removed URL is never added again
import tempfile as _tf
rp = os.path.join(tmp, "removed.txt")
open(rp, "w").write("# comment\n\nhttps://www.immobiliare.it/en/annunci/127335231/   # v22\n  # indented comment\nhttps://www.idealista.it/immobile/35498392\n")
rs = ul.load_removed(rp)
assert rs == {ul.norm_url("https://www.immobiliare.it/annunci/127335231/"), ul.norm_url("https://www.idealista.it/immobile/35498392/")}, rs
assert ul.load_removed(os.path.join(tmp, "nope.txt")) == set()
real = ul.load_removed()
assert ul.norm_url("https://www.immobiliare.it/annunci/128608380/") in real and ul.norm_url("https://www.immobiliare.it/annunci/130839936?x=1") in real and len(real) >= 10, real

shutil.copy(FIXTURE, path); open(summ, "w").close(); open(outp, "w").close()
existing = ul.extract_existing_listings(open(path, encoding="utf-8").read())
back = mkc("c93", "Via Tornata 6", "https://www.idealista.it/immobile/35498392/")                  # on the removed list (temp file above), other spelling
fresh = mkc("c94", "Via Nuova 7", "https://www.idealista.it/immobile/99999991/")
def removed_search(_):
    ul.LAST_SEARCH.clear(); ul.LAST_SEARCH.update(stop_reason="end_turn", web_searches=1, search_errors=[], result_hosts=[], text_blocks=1,
        text_chars=1, tokens_in=1, tokens_out=1, continuations=0, preview="", result_urls=set())
    return [back, fresh]
seen_by_verify = []
def vfy(client, ls):
    seen_by_verify.extend(l["addr"] for l in ls); return ls, [], {"evidence": {}}
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", side_effect=removed_search), mock.patch.object(ul, "REMOVED_PATH", rp), \
     mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), \
     mock.patch.object(ul.availability, "verify", side_effect=vfy), mock.patch.dict(os.environ, env):
    ul.main()
final = {l["addr"] for l in ul.extract_existing_listings(open(path, encoding="utf-8").read())}
assert "Via Tornata 6" not in final and "Via Nuova 7" in final and seen_by_verify == ["Via Nuova 7"], (final, seen_by_verify)
o = dict(line.rstrip("\n").split("=", 1) for line in open(outp))
assert "1 on the removed list" in o["search_report"], o

# ── 8. immobiliare.it side: merged with the web search, seen file written only when the site is, failure = continue + report
IMM_L = lambda i, addr, url: {"id": i, "hood": "Vanchiglia", "addr": addr, "floor": "2° piano", "lat": 45.07, "lng": 7.69, "price": 250000, "type": "Trilocale", "m2": 80,
                              "outdoor": "Balcone", "outdoorType": "balcone", "outdoorNote": "", "naturaNote": "", "desc": "x", "url": url, "img": "", "src": "immobiliare.it",
                              "top": False, "published": "2026-10-28"}
imm_result = lambda ls, rows: {"listings": ls, "seen_rows": rows, "report": "immobiliare.it read live: 2/2 search pages, 50 ads; 1 added", "details": ["vanchiglia: 25 ads, live copy (1.0 h old)"], "tokens": 4242}
tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); summ, outp = os.path.join(tmp, "s.md"), os.path.join(tmp, "o.txt"); seen_file = os.path.join(tmp, "seen.txt")
env = {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": outp, "GITHUB_STEP_SUMMARY": summ}
def fresh_site(): shutil.copy(FIXTURE, path); open(summ, "w").close(); open(outp, "w").close(); open(seen_file, "w").close()
def outputs(): return dict(line.rstrip("\n").split("=", 1) for line in open(outp))
def run_main(imm, web, verify=None, extra=()):
    stack = [mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", side_effect=lambda _: web),
             mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), mock.patch.object(ul, "REMOVED_PATH", os.path.join(tmp, "none.txt")),
             mock.patch.object(ul.immobiliare, "SEEN_PATH", seen_file), mock.patch.object(ul.immobiliare, "discover", side_effect=imm if callable(imm) else (lambda *a, **k: imm)),
             mock.patch.object(ul.availability, "verify", side_effect=verify or (lambda client, ls: (ls, [], {"evidence": {}}))), mock.patch.dict(os.environ, env), *extra]
    from contextlib import ExitStack
    with ExitStack() as st:
        for m in stack: st.enter_context(m)
        ul.main()
fresh_site(); existing = ul.extract_existing_listings(open(path, encoding="utf-8").read()); n0 = len(existing)
imm_l = IMM_L("v50", "Via Immobiliare 1", "https://www.immobiliare.it/annunci/133901445/")
web_l = mkc("v50", "Via Web 2", "https://www.idealista.it/immobile/12121212/"); web_l["src"] = "idealista.it"             # same id as the immobiliare one
web_imm = mkc("c95", "Via Dal Web 3", "https://www.immobiliare.it/annunci/133999999/"); web_imm["src"] = "immobiliare.it"   # model returned an immobiliare ad
web_imm2 = mkc("c96", "Via Dal Web 4", "https://www.idealista.it/immobile/13131313/"); web_imm2["src"] = "Immobiliare.it"   # wrong host, right-looking src
web_imm3 = mkc("c98", "Via Dal Web 8", "https://www.immobiliare.it/annunci/133888888/"); web_imm3["src"] = "idealista.it"     # right src label, immobiliare link
run_main(imm_result([imm_l], [("133901445", "2026-10-28", "added", "ok")]), [web_l, web_imm, web_imm2, web_imm3])
final = ul.extract_existing_listings(open(path, encoding="utf-8").read()); fa = {l["addr"]: l for l in final}
assert len(final) == n0 + 2 and "Via Immobiliare 1" in fa and "Via Web 2" in fa and "Via Dal Web 3" not in fa and "Via Dal Web 4" not in fa and "Via Dal Web 8" not in fa, [l["addr"] for l in final]
assert fa["Via Immobiliare 1"]["id"] == "v50" and fa["Via Web 2"]["id"] != "v50" and len({l["id"] for l in final}) == len(final), "ids must stay unique across both sources"
assert fa["Via Immobiliare 1"]["top"] in (True, False) and fa["Via Immobiliare 1"]["src"] == "immobiliare.it"
assert "133901445\t2026-10-28\tadded\tok" in open(seen_file).read().splitlines()
o = outputs(); s = open(summ).read()
assert o["new_count"] == "2" and o["immobiliare_error"] == "" and o["immobiliare_report"].startswith("immobiliare.it read live: 2/2"), o
assert "3 immobiliare.it result(s) from the web search ignored" in o["search_report"], o["search_report"]
assert "### 🏠 immobiliare.it read: ✅" in s and "vanchiglia: 25 ads" in s and "4,242" in s, s
assert "**0** rejected" in s and "model returned **1** candidate(s)" in s, s          # only the idealista candidate counts as a web-search candidate

# portal could not be read: web search results are still processed, the error is an output (the workflow turns it red), nothing is marked seen
def imm_down(*a, **k): raise ul.immobiliare.ImmobiliareError("no immobiliare.it search page could be read as a live copy: vanchiglia: not readable (url_not_accessible)")
fresh_site()
run_main(imm_down, [mkc("c97", "Via Solo Web 5", "https://www.idealista.it/immobile/14141414/")])
final = ul.extract_existing_listings(open(path, encoding="utf-8").read())
assert len(final) == n0 + 1 and any(l["addr"] == "Via Solo Web 5" for l in final), "web search must still work when immobiliare.it is down"
o = outputs(); s = open(summ).read()
assert "url_not_accessible" in o["immobiliare_error"] and o["immobiliare_report"] == "" and "\n" not in o["immobiliare_error"], o
assert "### 🏠 immobiliare.it read: ❌" in s and open(seen_file).read() == "", s

# nothing is marked seen when the run does not reach the write (Top verification fails): tomorrow examines the same ads again
fresh_site(); before = open(path, "rb").read()
def bad_verify(*a, **k): raise RuntimeError("promotion check failed")
try:
    run_main(imm_result([IMM_L("v60", "Via Perduta 6", "https://www.immobiliare.it/annunci/133901446/")], [("133901446", "2026-10-28", "added", "ok")]), [],
             extra=[mock.patch.object(ul.scoring, "verify_promotions", side_effect=bad_verify)])
    raise SystemExit("main() survived a failing promotion check")
except RuntimeError: pass
assert open(path, "rb").read() == before and open(seen_file).read() == "", "index.html and the seen file must both stay untouched"

# only immobiliare finds something: counts and the commit summary line include it
fresh_site()
run_main(imm_result([IMM_L("v61", "Via Sola 7", "https://www.immobiliare.it/annunci/133901447/")], [("133901447", "2026-10-28", "added", "ok")]), [])
o = outputs(); assert o["new_count"] == "1" and "Vanchiglia Trilocale €250,000" in o["summary"], o

shutil.rmtree(tmp)
print("ALL SEARCH TESTS PASSED")
