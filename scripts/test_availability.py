#!/usr/bin/env python3
"""Tests for scripts/availability.py and its use in update_listings.main(). No network: fake client.
Run: python3 scripts/test_availability.py
Block shapes are modelled on what the fetch tool returned in the experiment (server_tool_use + web_fetch_tool_result,
errors as error_code); only the fields the code reads are modelled."""
import copy, os, shutil, sys, tempfile, types
from types import SimpleNamespace as NS
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.dont_write_bytecode = True
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(__version__="stub", Anthropic=object)
import availability as av
import update_listings as ul

use = NS(type="server_tool_use", name="web_fetch")
page = lambda body="Trilocale in vendita " * 30: NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url="u", content=NS(type="document", source=NS(type="text", data=body))))
err = lambda code: NS(type="web_fetch_tool_result", content=NS(type="web_fetch_tool_error", error_code=code))
txt = lambda t: NS(type="text", text=t)
js = lambda status, ev="x": txt('{"status": "%s", "evidence": "%s"}' % (status, ev))
rep = lambda content: NS(content=content, stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=20))

class Client:
    def __init__(self, replies):
        self.queue, self.calls = list(replies), []
        self.beta = NS(messages=NS(create=self._create))
    def _create(self, **kw):
        self.calls.append(kw)
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

def one(content):
    c = Client([rep(content)]); r = av.check_listing(c, "https://www.immobiliare.it/annunci/12345678/"); return r, c

# ── check_listing
r, c = one([use, page(), js("available", "Trilocale in vendita")])
assert r["accept"] and r["fetched"] and r["status"] == "available" and r["tokens_in"] == 1000
k = c.calls[0]; assert k["tools"][0]["type"] == "web_fetch_20250910" and k["betas"] == ["web-fetch-2025-09-10"] and "12345678" in k["messages"][0]["content"]
r, _ = one([use, page(), txt('Here you go: {"status": "available", "evidence": "in vendita"}')]); assert r["accept"]            # JSON after chatter
r, _ = one([use, err("url_not_accessible"), js("cannot_tell", "403")]); assert not r["accept"] and "url_not_accessible" in r["reason"] and not r["fetched"]
r, _ = one([use, err("url_not_accessible"), js("available", "looks fine")]); assert not r["accept"], "trusted the model's words over a failed fetch"
r, _ = one([js("available", "from memory")]); assert not r["accept"] and "fetch tool not used" in r["reason"]                          # never called the tool
r, _ = one([use, page(""), js("available")]); assert not r["accept"] and "empty page" in r["reason"]
r, _ = one([use, page(), js("sold_or_removed", "Sold on 04/09/2026")]); assert not r["accept"] and r["reason"].startswith("sold_or_removed") and "Sold on" in r["reason"]
r, _ = one([use, page(), js("under_offer", "In trattativa")]); assert not r["accept"] and r["reason"].startswith("under_offer")
r, _ = one([use, page(), txt("I think it is fine")]); assert not r["accept"] and "none" in r["reason"]
r, _ = one([use, page(), js("weird_status")]); assert not r["accept"]

# ── verify
L = lambda n, **kw: {"id": f"x{n}", "addr": f"Via {n}", "url": f"https://www.immobiliare.it/annunci/1000000{n}/", **kw}
ls = [L(1), L(2), L(3), L(4)]; before = copy.deepcopy(ls)
cl = Client([rep([use, page(), js("available", "ok1")]), rep([use, err("url_not_accessible"), js("cannot_tell")]),
             rep([use, page(), js("sold_or_removed", "venduto")]), RuntimeError("boom")])
acc, rej, info = av.verify(cl, ls)
assert [l["id"] for l in acc] == ["x1"] and [l["id"] for l, _ in rej] == ["x2", "x3", "x4"], (acc, rej)
assert ls == before, "verify mutated the listings (evidence must not be written into them)"
assert info["evidence"][L(1)["url"]] == "ok1" and info["errors"] == 1 and info["checked"] == 4 and info["tokens_in"] == 3000
assert "check failed" in rej[2][1]
# every check crashing is a systematic fault -> VerifyError (but one crash among others is not)
try: av.verify(Client([RuntimeError("a"), RuntimeError("b")]), [L(1), L(2)]); raise SystemExit("VerifyError not raised")
except av.VerifyError: pass
assert av.verify(Client([RuntimeError("a"), rep([use, page(), js("available")])]), [L(1), L(2)])[2]["errors"] == 1
# cap
many = [L(i) for i in range(10, 24)]
cl = Client([rep([use, page(), js("available")])] * 12)
acc, rej, info = av.verify(cl, many)
assert len(acc) == 12 and len(rej) == 2 and all("not checked today" in w for _, w in rej) and len(cl.calls) == 12
assert av.verify(Client([]), []) == ([], [], {"checked": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "evidence": {}})

# ── main(): rejected candidates are reported and never written; a systematic failure turns the run red and writes nothing
FIXTURE = os.path.join(HERE, "fixtures", "index_sample.html")
tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); shutil.copy(FIXTURE, path)
summ, outp = os.path.join(tmp, "s.md"), os.path.join(tmp, "o.txt")
env = {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": outp, "GITHUB_STEP_SUMMARY": summ}
mkc = lambda i, addr, url: {"id": i, "hood": "Cavoretto", "addr": addr, "url": url, "floor": "1", "lat": 45.04, "lng": 7.71, "price": 300000, "type": "Trilocale",
                            "m2": 100, "outdoor": "Giardino", "outdoorType": "giardino", "outdoorNote": "x", "naturaNote": "x", "desc": "x", "img": "",
                            "src": "idealista.it", "top": False, "published": "2026-10-23"}
good, sold = mkc("c80", "Via Viva 1", "https://www.idealista.it/immobile/80000001/"), mkc("c81", "Via Venduta 2", "https://www.idealista.it/immobile/80000002/")
verdicts = {good["url"]: rep([use, page(), js("available", "in vendita")]), sold["url"]: rep([use, page(), js("sold_or_removed", "Venduto")])}
class Router(Client):
    def _create(self, **kw):
        self.calls.append(kw); u = kw["messages"][0]["content"].split("URL: ")[1].split("\n")[0]; return verdicts[u]
def go(client, summary_lines=True):
    with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=[good, sold]), \
         mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), mock.patch.object(ul.anthropic, "Anthropic", lambda api_key=None: client), \
         mock.patch.dict(os.environ, env):
        ul.main()
go(Router([]))
final = {l["addr"]: l for l in ul.extract_existing_listings(open(path, encoding="utf-8").read())}
assert "Via Viva 1" in final and "Via Venduta 2" not in final and "_evidence" not in str(final["Via Viva 1"])
s = open(summ).read(); o = dict(line.rstrip("\n").split("=", 1) for line in open(outp))
assert "**1** not available or unverifiable" in s and "not added (availability): Via Venduta 2" in s and "verified: in vendita" in s, s
assert "1 not available/unverifiable, 1 new" in o["search_report"], o
# systematic failure: every check raises -> run fails, file untouched
shutil.copy(FIXTURE, path); untouched = open(path, "rb").read(); open(summ, "w").close()
class Boom(Client):
    def _create(self, **kw): raise RuntimeError("tool not accepted")
try: go(Boom([])); raise SystemExit("main() survived a systematic availability failure")
except av.VerifyError: pass
assert open(path, "rb").read() == untouched and "❌ Availability check failed" in open(summ).read()
shutil.rmtree(tmp)
print("ALL AVAILABILITY TESTS PASSED")
