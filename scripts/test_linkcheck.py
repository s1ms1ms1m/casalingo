#!/usr/bin/env python3
"""Tests for the dead-listing check. Run: python3 scripts/test_linkcheck.py

classify() is exercised against a real local HTTP server (real sockets, real
redirects), never the internet. Uses the frozen fixture, not the live index.html.
"""
import copy, os, re, shutil, sys, tempfile, threading, time, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(__version__="stub", Anthropic=object)
import linkcheck as lc
import update_listings as ul

# ── 1. which URLs may ever be requested (SSRF guard) ─────────────────────────
for ok in ["https://www.immobiliare.it/annunci/131464854/", "https://immobiliare.it/x", "https://www.wikicasa.it/annuncio/1",
           "https://img4.idealista.it/a", "https://www.casa.it/x"]:
    assert lc.portal_of(ok), ok
for bad in ["http://www.immobiliare.it/x", "https://immobiliare.it.evil.com/x", "https://evilcasa.it/x", "https://127.0.0.1/x",
            "https://169.254.169.254/latest/meta-data", "https://immobiliare.it@evil.com/x", "ftp://www.casa.it/x", "", "not a url",
            "https://localhost/x"]:
    assert lc.portal_of(bad) is None, bad
assert lc.classify("https://169.254.169.254/x", get=lambda *a: (_ for _ in ()).throw(AssertionError("must not request")))[0] == "unknown"
assert lc.listing_ref("https://www.wikicasa.it/annuncio/30990084") == "30990084"
assert lc.listing_ref("https://www.immobiliare.it/annunci/131464854/") == "131464854"
assert lc.listing_ref("https://www.idealista.it/immobile/34683058/") == "34683058"
assert lc.listing_ref("https://example.test/abc") is None

# ── 2. classify() against a real local server ────────────────────────────────
PORT2 = [0]
PAGE = "<html><head><title>{t}</title></head><body><h1>{h}</h1><p>{b}</p></body></html>"
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body="", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        try: self.wfile.write(body.encode())
        except (BrokenPipeError, ConnectionResetError): pass       # client gave up (timeout / truncated read): expected
    def do_GET(self):
        p = self.path
        if p == "/annuncio/12345678":  self._send(200, PAGE.format(t="Trilocale via Roma, Torino", h="Trilocale", b="Ascensore non disponibile. Annunci simili: Questo annuncio non è più disponibile (venduto), annuncio scaduto, pagina non trovata."))
        elif p == "/annuncio/22222222": self._send(404, "nf")
        elif p == "/annuncio/33333333": self._send(410, "gone")
        elif p == "/annuncio/44444444": self._send(302, "", {"Location": "/ricerca?citta=torino"})
        elif p == "/ricerca?citta=torino": self._send(200, PAGE.format(t="Case in vendita a Torino", h="Risultati", b="x"))
        elif p == "/annuncio/55555555": self._send(200, PAGE.format(t="Annuncio non più disponibile", h="Ops", b="x"))
        elif p == "/annuncio/55555556": self._send(200, PAGE.format(t="Trilocale", h="Questo annuncio non è più disponibile", b="x"))
        elif p == "/annuncio/66666666": self._send(403, "forbidden")
        elif p == "/annuncio/66666667": self._send(429, "slow down")
        elif p == "/annuncio/66666668": self._send(503, "down")
        elif p == "/annuncio/77777777": self._send(200, PAGE.format(t="Just a moment...", h="Checking", b="x"))
        elif p == "/annuncio/77777778": self._send(200, PAGE.format(t="Captcha", h="x", b="x"))
        elif p == "/annuncio/88888888": self._send(200, PAGE.format(t="Appartamento Corso Botanico 404, Torino", h="Trilocale", b="x"))
        elif p == "/annuncio/99999999": self._send(302, "", {"Location": f"http://127.0.0.1:{PORT2[0]}/annuncio/99999999"})
        elif p == "/loop": self._send(302, "", {"Location": "/loop"})
        elif p == "/annuncio/12121212": self._send(301, "", {"Location": "/annunci/12121212-trilocale-via-roma"})
        elif p == "/annunci/12121212-trilocale-via-roma": self._send(200, PAGE.format(t="Trilocale", h="x", b="x"))
        elif p == "/annuncio/13131313": self._send(302, "")                       # redirect without Location
        elif p == "/slow": time.sleep(2); self._send(200, PAGE.format(t="late", h="x", b="x"))
        elif p == "/annuncio/14141414": self._send(200, PAGE.format(t="Trilocale", h="x", b="y" * 2_000_000))   # huge body
        elif p == "/noref": self._send(200, PAGE.format(t="Trilocale", h="x", b="x"))
        else: self._send(404, "nf")
class H2(H):
    def do_GET(self): self._send(200, PAGE.format(t="Trilocale", h="x", b="x"))      # would look 'alive' if followed
srv2 = ThreadingHTTPServer(("127.0.0.1", 0), H2); port2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
PORT2[0] = port2
srv = ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
host_ok = lambda u: u.startswith(base + "/")
cl = lambda path, **k: lc.classify(base + path, host_ok=host_ok, timeout=k.pop("timeout", 1.0), **k)
for path, want in [
    ("/annuncio/12345678", "alive"),     # body text like "venduto" / "non disponibile" must not matter
    ("/annuncio/22222222", "dead"), ("/annuncio/33333333", "dead"),
    ("/annuncio/44444444", "dead"),      # redirected away to a search page
    ("/annuncio/55555555", "dead"), ("/annuncio/55555556", "dead"),   # headline in <title> / <h1>
    ("/annuncio/66666666", "unknown"), ("/annuncio/66666667", "unknown"), ("/annuncio/66666668", "unknown"),
    ("/annuncio/77777777", "unknown"), ("/annuncio/77777778", "unknown"),   # block pages
    ("/annuncio/88888888", "alive"),     # "404" in a street number, "Botanico" is not a bot wall
    ("/annuncio/99999999", "unknown"),   # redirect to another host is never followed
    ("/loop", "unknown"), ("/annuncio/13131313", "unknown"),
    ("/annuncio/12121212", "alive"),     # canonical redirect that keeps the ad id
    ("/slow", "unknown"),                # timeout
    ("/annuncio/14141414", "alive"),     # huge body is truncated, not hung on
    ("/noref", "alive"),                 # no ad id in the URL: redirect rule not applicable
]:
    got = cl(path)
    assert got[0] == want, (path, got, want)
dead_reasons = {cl(p)[1] for p in ("/annuncio/22222222", "/annuncio/44444444", "/annuncio/55555555")}
assert dead_reasons == {"http 404", "redirected away from the listing", "headline says it is no longer available"}, dead_reasons
srv.shutdown(); srv2.shutdown()

# ── 3. update_state: hysteresis, trust, suspicious runs, revive, lock ─────────
def mk(i, url, **kw): return {"id": i, "url": url, **kw}
W = lambda n: f"https://www.wikicasa.it/annuncio/{30000000 + n}"
I = lambda n: f"https://www.immobiliare.it/annunci/{130000000 + n}/"
L = [mk("w1", W(1)), mk("w2", W(2)), mk("w3", W(3)), mk("w4", W(4)), mk("i1", I(1)), mk("i2", I(2))]
alive = lambda ids: {i: ("alive", "ok") for i in ids}
state, ch = lc.update_state({}, L, alive(["w1", "w2", "w3", "w4", "i1", "i2"]), "2026-10-01")
assert all(r["s"] == "ok" and r["a"] == "2026-10-01" for r in state.values()) and ch == {"gone": [], "revived": [], "suspicious": []}

dead_w1 = {**alive(["w2", "w3", "w4", "i1", "i2"]), "w1": ("dead", "http 404")}
s1, c1 = lc.update_state(state, L, dead_w1, "2026-10-02"); assert s1["w1"]["s"] == "ok" and s1["w1"]["f"] == 1 and c1["gone"] == []
s2, c2 = lc.update_state(s1, L, dead_w1, "2026-10-03"); assert s2["w1"]["s"] == "ok" and s2["w1"]["f"] == 2
s3, c3 = lc.update_state(s2, L, dead_w1, "2026-10-04"); assert s3["w1"]["s"] == "gone" and c3["gone"] == ["w1"]
s4, c4 = lc.update_state(s3, L, dead_w1, "2026-10-05"); assert s4["w1"]["s"] == "gone" and c4["gone"] == [], "reported once"
# alive in between resets the counter
sx, _ = lc.update_state(s2, L, alive(["w1", "w2", "w3", "w4", "i1", "i2"]), "2026-10-04"); assert sx["w1"]["f"] == 0
# unknown changes nothing (and counts separately)
unk = {**alive(["w2", "w3", "w4", "i1", "i2"]), "w1": ("unknown", "http 403")}
su, cu = lc.update_state(s2, L, unk, "2026-10-04"); assert su["w1"]["f"] == 2 and su["w1"]["s"] == "ok" and su["w1"]["u"] == 1 and cu["gone"] == []
sg, _ = lc.update_state(s3, L, unk, "2026-10-06"); assert sg["w1"]["s"] == "gone", "unknown must not revive"
# revive when it answers alive again
sr, cr = lc.update_state(s3, L, alive(["w1"]), "2026-10-06"); assert sr["w1"]["s"] == "ok" and cr["revived"] == ["w1"]

# a portal that never served a real page cannot make anything 'gone'
only_dead = {i: ("dead", "http 404") for i in ("i1", "i2")}
sp = {}
for d in range(5):
    sp, cp = lc.update_state(sp, L, only_dead, f"2026-10-0{d+1}")
assert sp["i1"]["s"] == "ok" and sp["i1"]["f"] == 0 and "never served" in sp["i1"]["why"], sp["i1"]
# ...but once one of its listings is alive in the same run, it is trusted
mixed = {"i1": ("alive", "ok"), "i2": ("dead", "http 404")}
sm, _ = lc.update_state({}, L, mixed, "2026-10-01"); assert sm["i2"]["f"] == 1

# suspicious: >= half of a portal's (>=3) checked listings dead in one run -> ignored
trusted_prev = state
sus = {"w1": ("dead", "x"), "w2": ("dead", "x"), "w3": ("alive", "ok"), "w4": ("alive", "ok")}
ss, cs = lc.update_state(trusted_prev, L, sus, "2026-10-09")
assert cs["suspicious"] == ["wikicasa.it"] and ss["w1"]["f"] == 0 and ss["w2"]["f"] == 0, (cs, ss["w1"])
one = {"w1": ("dead", "x"), "w2": ("alive", "ok"), "w3": ("alive", "ok"), "w4": ("alive", "ok")}
so, co = lc.update_state(trusted_prev, L, one, "2026-10-09"); assert co["suspicious"] == [] and so["w1"]["f"] == 1
two_listings = {"i1": ("dead", "x"), "i2": ("dead", "x")}          # below SUSPICIOUS_MIN: trust rule still protects
sq, _ = lc.update_state(trusted_prev, L, two_listings, "2026-10-09"); assert sq["i1"]["f"] == 1   # i1 was alive before -> trusted, counts

# skipLinkCheck, pruning, not-checked, new listings
L2 = copy.deepcopy(L); L2[0]["skipLinkCheck"] = True
sl, _ = lc.update_state(s3, L2, {}, "2026-10-10"); assert sl["w1"]["s"] == "ok", "skipLinkCheck must un-gone"
sp2, _ = lc.update_state(s3, L[1:], {}, "2026-10-10"); assert "w1" not in sp2 and len(sp2) == len(L) - 1
unchecked, _ = lc.update_state(s3, L, {}, "2026-10-10"); assert unchecked["w1"]["s"] == "gone"
fresh, _ = lc.update_state({}, L + [mk("n1", W(9))], {}, "2026-10-10"); assert fresh["n1"]["s"] == "ok"

# ── 4. state line round trip, budget ─────────────────────────────────────────
html = open(os.path.join(HERE, "fixtures", "index_sample.html"), encoding="utf-8").read()
assert lc.read_state(html) == {}
h2 = lc.write_state(html, s3)
assert lc.read_state(h2) == s3 and h2.count("const linkStatus") == 1
assert re.sub(r"const linkStatus[^\n]*", "", h2) == re.sub(r"const linkStatus[^\n]*", "", html), "write_state touched more than its line"
assert lc.read_state(lc.write_state(h2.replace(lc.STATE_RE.search(h2).group(0), lc.STATE_RE.search(h2).group(0) + "   // note"), {"a": {"s": "ok"}})) == {"a": {"s": "ok"}}
tricky = "const linkStatus = {};   // note\nfunction f(){ return 1; }\nconst x = {a:1};\nconst y = {b:2};\n"
w = lc.write_state(tricky, {"q": {"s": "ok"}})
assert "const x = {a:1};" in w and "const y = {b:2};" in w and "function f(){ return 1; }" in w and w.count("const linkStatus") == 1, w
assert lc.read_state(w) == {"q": {"s": "ok"}}
for bad in (lambda x: x.replace("const linkStatus", "const other"),):
    try: lc.read_state(bad(html)); raise SystemExit("read_state accepted a missing line")
    except ValueError: pass
slow = lambda url: (time.sleep(0.06), ("alive", "ok"))[1]
r = lc.check_all([mk(f"x{i}", W(i)) for i in range(20)], "2026-10-10", budget_s=0.2, pause_s=0, classify_fn=slow)
assert 1 <= len(r) < 20, len(r)
assert set(lc.check_all([mk("a", W(1)), mk("b", W(2))], "d", pause_s=0, classify_fn=slow, skip_ids={"a"})) == {"b"}

# ── 5. end to end through main(): model + network mocked, link step isolated ──
FIX = os.path.join(HERE, "fixtures", "index_sample.html")
parse = ul.extract_existing_listings
ids_all = [l["id"] for l in parse(html)]
def run_main(path, summary_file, out_file, check_all, write_state=None, candidates=()):
    patches = [mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=list(candidates)),
               mock.patch.object(ul, "check_image_url", side_effect=lambda u: u),
               mock.patch.object(ul.linkcheck, "check_all", side_effect=check_all),
               mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": out_file, "GITHUB_STEP_SUMMARY": summary_file})]
    if write_state: patches.append(mock.patch.object(ul.linkcheck, "write_state", side_effect=write_state))
    for p_ in patches: p_.start()
    try: ul.main()
    finally:
        for p_ in reversed(patches): p_.stop()
    return dict(line.rstrip("\n").split("=", 1) for line in open(out_file))

tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); out = os.path.join(tmp, "o.txt"); summ = os.path.join(tmp, "s.md")
shutil.copy(FIX, path)
seen = {}
def all_alive(listings, today, skip_ids=(), **k):
    seen["skip"] = set(skip_ids); return {l["id"]: ("alive", "ok") for l in listings if l["id"] not in skip_ids}
o = run_main(path, summ, out, all_alive)
res = open(path, encoding="utf-8").read(); st = lc.read_state(res)
assert set(st) == set(ids_all) and all(r["s"] == "ok" and r.get("a") for r in st.values()) and o["gone_count"] == "0", o
assert "### Link check" in open(summ).read() and "| wikicasa.it |" in open(summ).read()

# three consecutive dead days for v3 (a wikicasa listing; its portal is trusted because the others are alive)
def v3_dead(listings, today, skip_ids=(), **k):
    return {l["id"]: (("dead", "http 404") if l["id"] == "v3" else ("alive", "ok")) for l in listings if l["id"] not in skip_ids}
for day in range(3):
    open(out, "w").close(); open(summ, "w").close()
    o = run_main(path, summ, out, v3_dead)
    assert o["gone_count"] == ("1" if day == 2 else "0"), (day, o)
assert "no longer online" in o["link_summary"] and "Via Giulia di Barolo 16" in o["link_summary"], o
res = open(path, encoding="utf-8").read()
assert lc.read_state(res)["v3"]["s"] == "gone"
assert {l["id"]: l["price"] for l in parse(res)} == {l["id"]: l["price"] for l in parse(html)}, "listing data changed"
open(out, "w").close()
o = run_main(path, summ, out, v3_dead)                              # 4th day: still gone, reported only once
assert o["gone_count"] == "0" and lc.read_state(open(path, encoding="utf-8").read())["v3"]["s"] == "gone"

# new listings and skipLinkCheck listings are never requested
shutil.copy(FIX, path)
txt = open(path, encoding="utf-8").read().replace("{id:'v1',", "{id:'v1',skipLinkCheck:true,", 1); open(path, "w", encoding="utf-8").write(txt)
cand = {"id": "c9", "hood": "Cavoretto", "addr": "Via Prova 1", "floor": "1", "lat": 45.04, "lng": 7.71, "price": 300000, "type": "Trilocale",
        "m2": 100, "outdoor": "Giardino", "outdoorType": "giardino", "outdoorNote": "x", "naturaNote": "Boschi collinari.", "desc": "Zona silenziosa.",
        "url": "https://www.wikicasa.it/annuncio/99999991", "img": "", "src": "Wikicasa", "top": False, "published": "2026-10-23"}
open(out, "w").close()
o = run_main(path, summ, out, all_alive, candidates=[cand])
assert {"c9", "v1"} <= seen["skip"], seen["skip"]
assert lc.read_state(open(path, encoding="utf-8").read())["c9"]["s"] == "ok"

# a crash in the link step must not stop the update (date stamp + promotions still go out)
shutil.copy(FIX, path); open(out, "w").close()
def boom(*a, **k): raise RuntimeError("portal exploded")
import contextlib, io
e2 = io.StringIO()
with contextlib.redirect_stderr(e2):
    o = run_main(path, summ, out, boom)
assert "portal exploded" in e2.getvalue()
res = open(path, encoding="utf-8").read()
assert o["gone_count"] == "0" and lc.read_state(res) == {} and re.search(r"const lastChecked\s*=\s*'\d{4}-\d{2}-\d{2}';", res)
assert int(o["promoted_count"]) == 4, "Top promotions must still happen when the link step fails"

# a link step that would damage the listings array is rejected and discarded
shutil.copy(FIX, path); open(out, "w").close()
_real_write_state = lc.write_state          # keep the real one: the patched name would recurse
def corrupting(html_, state):
    return _real_write_state(html_, state).replace("price:355000", "price:1", 1)
import contextlib, io
err = io.StringIO()
with contextlib.redirect_stderr(err):
    o = run_main(path, summ, out, all_alive, write_state=corrupting)
res = open(path, encoding="utf-8").read()
assert "changed the listings array" in err.getvalue(), err.getvalue()      # rejected by the integrity check, not by some other error
assert "price:1," not in res and lc.read_state(res) == {} and o["gone_count"] == "0", "corrupted html was written"
shutil.rmtree(tmp)
print("ALL TESTS PASSED")
