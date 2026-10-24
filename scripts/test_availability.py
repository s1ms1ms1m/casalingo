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
import datetime as _dt
_now = lambda days=0: (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat(timespec="seconds")
def page(body=None, ad="12345678", age=0, retrieved="auto"):
    ids = " ".join(f"1000000{n:02d} 1000000{n}" for n in range(1, 40)) if ad == "*" else ad     # "*" = a page that mentions every test ad ID
    body = ("canonical: https://www.immobiliare.it/annunci/%s/\n" % ids + "Trilocale in vendita " * 30) if body is None else body
    inner = NS(type="web_fetch_result", url="u", content=NS(type="document", source=NS(type="text", data=body)))
    if retrieved == "auto":
        inner.retrieved_at = _now(age)
    elif retrieved is not None:
        inner.retrieved_at = retrieved
    return NS(type="web_fetch_tool_result", content=inner)
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

# stale copy / wrong page: an "available" verdict is only trusted for a recent copy of THIS ad
r, _ = one([use, page(age=3), js("available", "ok")]); assert r["accept"] and "page fetched" in r["reason"]
r, _ = one([use, page(age=64), js("available", "ok")]); assert not r["accept"] and r["reason"].startswith("stale copy (retrieved 64 days ago") and av.needs_hand_check(r["reason"]), r
r, _ = one([use, page(age=av.MAX_AGE_DAYS + 1), js("available")]); assert not r["accept"]
r, _ = one([use, page(age=av.MAX_AGE_DAYS - 1), js("available")]); assert r["accept"]
r, _ = one([use, page(ad="99999999"), js("available", "ok")]); assert not r["accept"] and "ID" in r["reason"] and av.needs_hand_check(r["reason"]), r
r, _ = one([use, page(retrieved=None), js("available", "ok")]); assert not r["accept"] and "retrieval date" in r["reason"], r
r, _ = one([use, page(retrieved="garbage"), js("available", "ok")]); assert not r["accept"] and "retrieval date" in r["reason"], r
r, _ = one([use, page(age=64), js("sold_or_removed", "venduto")]); assert r["reason"].startswith("sold_or_removed"), "a sold verdict must win over staleness"
assert not av.needs_hand_check("sold_or_removed: Venduto") and not av.needs_hand_check("under_offer: x") and av.needs_hand_check("page not fetchable (x)")
# naive timestamp is read as UTC; "Z" suffix works
assert av.page_problem({"text": "12345678", "retrieved_at": _now(0)[:19]}, "https://x/annunci/12345678/") is None
assert av.page_problem({"text": "12345678", "retrieved_at": _now(0).replace("+00:00", "Z")}, "https://x/annunci/12345678/") is None

# ── verify
L = lambda n, **kw: {"id": f"x{n}", "addr": f"Via {n}", "url": f"https://www.immobiliare.it/annunci/1000000{n}/", **kw}
ls = [L(1), L(2), L(3), L(4)]; before = copy.deepcopy(ls)
cl = Client([rep([use, page(ad="10000001"), js("available", "ok1")]), rep([use, err("url_not_accessible"), js("cannot_tell")]),
             rep([use, page(ad="10000003"), js("sold_or_removed", "venduto")]), RuntimeError("boom")])
acc, rej, info = av.verify(cl, ls)
assert [l["id"] for l in acc] == ["x1"] and [l["id"] for l, _ in rej] == ["x2", "x3", "x4"], (acc, rej)
assert ls == before, "verify mutated the listings (evidence must not be written into them)"
assert info["evidence"][L(1)["url"]] == "ok1" and info["errors"] == 1 and info["checked"] == 4 and info["tokens_in"] == 3000
assert "check failed" in rej[2][1]
# every check crashing is a systematic fault -> VerifyError (but one crash among others is not)
try: av.verify(Client([RuntimeError("a"), RuntimeError("b")]), [L(1), L(2)]); raise SystemExit("VerifyError not raised")
except av.VerifyError: pass
assert av.verify(Client([RuntimeError("a"), rep([use, page(ad="*"), js("available")])]), [L(1), L(2)])[2]["errors"] == 1
# cap
many = [L(i) for i in range(10, 24)]
cl = Client([rep([use, page(ad="*"), js("available")])] * 12)
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
verdicts = {good["url"]: rep([use, page(ad="80000001"), js("available", "in vendita")]), sold["url"]: rep([use, page(ad="80000002"), js("sold_or_removed", "Venduto")])}
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
