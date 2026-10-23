#!/usr/bin/env python3
"""Plain-assert tests for Top re-scoring. Run: python3 scripts/test_scoring.py

Uses a FROZEN fixture (scripts/fixtures/index_sample.html), never the live
index.html, so editing real listings can never make these tests (and therefore
the daily job) fail. They only fail when the code is broken.
"""
import copy, os, re, shutil, sys, tempfile, types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(__version__="stub", Anthropic=object)
import json5
import scoring
import update_listings as ul

FIXTURE = os.path.join(HERE, "fixtures", "index_sample.html")
html = open(FIXTURE, encoding="utf-8").read()
listings = ul.extract_existing_listings(html)
by_id = {l["id"]: l for l in listings}
tops = lambda h: {l["id"] for l in ul.extract_existing_listings(h) if l["top"]}
parse = lambda h: ul.extract_existing_listings(h)
strip_top = lambda h: re.sub(r"(top[\"']?\s*:\s*)(true|false)", r"\1X", h)

# ── 1. walking-time parsing ───────────────────────────────────────────────────
for text, expect in [
    ("A due minuti a piedi dalle sponde del Po", 2),
    ("raggiungibile a piedi in circa 5 minuti", 5),
    ("a circa 12–15 minuti a piedi", 15),            # range -> upper bound (when unsure, say no)
    ("A 7-8 minuti a piedi", 8),
    ("in meno di 5 minuti a piedi", 5),
    ("Il Valentino è a 12 minuti ma il Po a 3 minuti a piedi", 3),
]:
    assert scoring.walk_minutes(text) == expect, (text, scoring.walk_minutes(text))
for text in [
    "Il Po è a 5 minuti in auto",
    "Il parco è a più di 5 minuti a piedi",
    "Parco a oltre 5 minuti",
    "Parco a 4 minuti in bici",
    "Fermata metro a 3 minuti",
    "Il Valentino è a 12 minuti ma il Po a 3 minuti in auto",
    "nessun dato",
]:
    assert scoring.walk_minutes(text) is None or scoring.walk_minutes(text) > 5, text
    assert not scoring.criteria({"hood": "Vanchiglia", "naturaNote": text})["nature"], text

# ── 2. quiet ──────────────────────────────────────────────────────────────────
q = lambda **k: scoring.criteria({"addr": "Via Prova 1", **k})["quiet"]
assert q(desc="Zona tranquilla") and q(outdoorNote="affaccio silenzioso") and q(naturaNote="zona verde e quieta")
assert not q(outdoorNote="balcone sulla corte interna")            # courtyard alone is not evidence
assert not q(desc="complesso residenziale")                        # nor is "residenziale"
assert not q(desc="Zona tranquilla ma strada rumorosa")
assert not scoring.criteria({"addr": "Corso Vittorio Emanuele II 5", "desc": "tranquillo"})["quiet"]
assert scoring.criteria(by_id["c3"]) == {"outdoor": True, "nature": True, "value": False, "quiet": True}   # renovation cancels value

# ── 3. fixture regression: expected promotions at PROMOTE_MIN=3 ───────────────
assert scoring.PROMOTE_MIN == 3
before = tops(html)
assert before == {"v3", "s1", "c1"}
new_html, promoted = scoring.promote_top(html, listings)
assert {l["id"] for l in promoted} == {"v7", "c2", "c3", "c5"}, sorted(l["id"] for l in promoted)
assert tops(new_html) == before | {"v7", "c2", "c3", "c5"}
assert strip_top(new_html) == strip_top(html), "something other than top flags changed"
assert not scoring.qualifies(by_id["s1"]) and by_id["s1"]["top"], "s1: stays Top only because flags are sticky"
assert not scoring.qualifies(by_id["v1"]), "v1 quiet rested only on 'corte interna'"
again, p2 = scoring.promote_top(new_html, parse(new_html))
assert again == new_html and p2 == [], "not idempotent"
scoring.verify_promotions(listings, parse(new_html), [l["id"] for l in promoted])

# ── 4. sticky ─────────────────────────────────────────────────────────────────
weak = copy.deepcopy(by_id["s1"]); weak["naturaNote"] = "nessun verde"; weak["outdoorType"] = "balcone"
assert not scoring.qualifies(weak) and weak["top"] is True
h2, p3 = scoring.promote_top(html, [weak]); assert h2 == html and p3 == []

# ── 5. edit is confined to the right object, whatever the key order / content ─
def js(**kw):  # JSON-style object, keys in the order given
    return "{" + ", ".join(f'"{k}": {json5.dumps(v)}' for k, v in kw.items()) + "}"
A = dict(top=False, id="A", hood="Cavoretto", addr="Via A", outdoorType="terrazzo", naturaNote="Boschi collinari.",
         desc="Zona silenziosa } { con apice \\' e // finto commento", price=300000, m2=100)
B = dict(top=False, id="B", hood="Vanchiglia", addr="Via B", outdoorType="balcone", naturaNote="x", desc="x", price=900000, m2=50)
src = f"const listings = [\n {js(**A)},\n // a real comment with an apostrophe: l'agenzia\n {js(**B)},\n /* block {{ */\n];\n"
Ls = parse(src)
assert scoring.qualifies(Ls[0]) and not scoring.qualifies(Ls[1])
out, pr = scoring.promote_top(src, Ls)
assert [l["id"] for l in pr] == ["A"]
assert {l["id"]: l["top"] for l in parse(out)} == {"A": True, "B": False}, "flipped the wrong listing (top before id)"
# duplicate ids: refuse to guess
dup = f"const listings = [\n {js(**A)},\n {js(**{**A})},\n];\n"
assert scoring.set_top_true(dup, "A") == (dup, False)
# unknown id: unchanged
assert scoring.set_top_true(src, "ZZ") == (src, False)
# mixed styles (hand-written JS + injected JSON) in the real fixture
cand = {"id": "c9", "hood": "Cavoretto", "addr": "Via Prova 1", "floor": "1", "lat": 45.04, "lng": 7.71, "price": 300000,
        "type": "Trilocale", "m2": 100, "outdoor": "Giardino", "outdoorType": "giardino", "outdoorNote": "Giardino privato.",
        "naturaNote": "Boschi collinari.", "desc": "Zona silenziosa.", "url": "https://example.test/1", "img": "",
        "src": "casa.it", "top": False, "published": "2026-10-23"}
inj = ul.inject_new_listings(html, [cand])
inj2, pr = scoring.promote_top(inj, parse(inj))
assert "c9" in {l["id"] for l in pr} and "c9" in tops(inj2) and tops(inj2) >= tops(new_html)

# ── 6. topLocked ──────────────────────────────────────────────────────────────
locked = copy.deepcopy(listings)
next(l for l in locked if l["id"] == "c3")["topLocked"] = True
nh, pl = scoring.promote_top(html, locked)
assert "c3" not in {l["id"] for l in pl} and {l["id"] for l in pl} == {"v7", "c2", "c5"}
lt = copy.deepcopy(by_id["v3"]); lt["topLocked"] = True
assert scoring.promote_top(html, [lt]) == (html, [])

# ── 7. verify_promotions catches collateral damage ────────────────────────────
good = copy.deepcopy(listings); next(l for l in good if l["id"] == "v7")["top"] = True
scoring.verify_promotions(listings, good, ["v7"])
for mutate in (lambda d: d.__setitem__("top", True),           # unpromoted listing flipped
               lambda d: d.__setitem__("price", 1)):           # other field changed
    bad = copy.deepcopy(good); mutate(next(l for l in bad if l["id"] == "c2"))
    try: scoring.verify_promotions(listings, bad, ["v7"]); raise SystemExit("verify missed a change")
    except ValueError: pass
try: scoring.verify_promotions(listings, good[:-1], ["v7"]); raise SystemExit("verify missed a removal")
except ValueError: pass

# ── 8. report ─────────────────────────────────────────────────────────────────
rep = scoring.markdown_report([{**by_id["c5"], "addr": "Via | strana\nriga"}, {**by_id["v3"], "topLocked": True}], ["c5"])
assert "★ NEW" in rep and "🔒 ★" in rep and rep.count("\n") == 3 and "|strana" not in rep and "Via / strana riga" in rep

# ── 9. end to end through main() with the model call mocked ───────────────────
def run_main(path, returned, summary_file, out_file):
    with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=returned), \
         mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), \
         mock.patch.object(ul.linkcheck, "check_all", return_value={}), \
         mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": out_file, "GITHUB_STEP_SUMMARY": summary_file}):
        ul.main()
    return dict(line.rstrip("\n").split("=", 1) for line in open(out_file))

tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); shutil.copy(FIXTURE, path)
out, summ = os.path.join(tmp, "out.txt"), os.path.join(tmp, "summary.md")
o = run_main(path, [cand, copy.deepcopy(by_id["v1"])], summ, out)
assert o["new_count"] == "1" and "Cavoretto" in o["summary"] and o["promoted_count"] == "5", o
res = open(path, encoding="utf-8").read()
assert re.search(r"const lastChecked\s*=\s*'\d{4}-\d{2}-\d{2}';", res) and 'id="last-checked"' in res
assert len(parse(res)) == len(listings) + 1 and tops(res) == before | {"v7", "c2", "c3", "c5", "c9"}
assert "★ NEW" in open(summ).read() and "| id |" in open(summ).read()
open(out, "w").close(); open(summ, "w").close()
o2 = run_main(path, [], summ, out)                         # nothing new, nothing to promote
assert o2["new_count"] == "0" and o2["promoted_count"] == "0" and o2["summary"] == "no new listings", o2

# a bug that flips the wrong listing must abort the run and leave the file untouched
shutil.copy(FIXTURE, path); untouched = open(path, "rb").read(); open(out, "w").close()
def wrong_flip(h, listing_id):
    spans = scoring._listing_spans(h)
    s, e = next((s, e) for s, e in spans if "'v2'" in h[s:e])      # always flips v2, whatever was asked
    seg = re.sub(r"top:false", "top:true", h[s:e], count=1)
    return h[:s] + seg + h[e:], True
try:
    with mock.patch.object(scoring, "set_top_true", side_effect=wrong_flip):
        run_main(path, [], summ, out)
    raise SystemExit("main() wrote a corrupted file")
except ValueError:
    pass
assert open(path, "rb").read() == untouched, "file was modified despite failed verification"

# ── 10. outputs are forced to one line (no output injection from model text) ──
with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}):
    open(out, "w").close(); ul.write_outputs(summary="a\nnew_count=99\nb", n=1)
assert open(out).read().splitlines() == ["summary=a new_count=99 b", "n=1"]
shutil.rmtree(tmp)
print("ALL TESTS PASSED")
