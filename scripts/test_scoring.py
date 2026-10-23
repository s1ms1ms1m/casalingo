#!/usr/bin/env python3
"""Plain-assert tests for Top re-scoring. Run: python3 scripts/test_scoring.py"""
import os, re, sys, shutil, tempfile, types, copy
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.modules["anthropic"] = types.SimpleNamespace(__version__="stub", Anthropic=object)
import json5
import scoring
import update_listings as ul

INDEX = os.path.join(HERE, "..", "index.html")
html = open(INDEX, encoding="utf-8").read()
listings = ul.extract_existing_listings(html)
by_id = {l["id"]: l for l in listings}

def tops(h): return {l["id"] for l in ul.extract_existing_listings(h) if l["top"]}

# 1. criteria on known listings
assert scoring.criteria(by_id["c3"]) == {"outdoor": True, "nature": True, "value": False, "quiet": True}, "c3: 'da ristrutturare' must cancel value"
assert scoring.criteria(by_id["s6"])["quiet"] is False, "Corso Vittorio Emanuele is an artery"
assert scoring._min_minutes("A due minuti a piedi") == 2 and scoring._min_minutes("a circa 10 minuti") == 10
assert scoring._min_minutes("7–8 minuti") == 7 and scoring._min_minutes("nessun dato") is None

# 2. promotion on the real data: sticky + expected set (documented in the PR)
before = tops(html)
new_html, promoted = scoring.promote_top(html, listings)
after = tops(new_html)
assert before <= after, "a Top flag was removed"
assert {l["id"] for l in promoted} == after - before
print("promoted at PROMOTE_MIN=%d:" % scoring.PROMOTE_MIN, sorted(after - before), "| already top:", sorted(before))
assert {"s6", "v8", "s3"} <= set(by_id) - after, "these must stay non-top"
# nothing but top flags changed
strip = lambda h: re.sub(r"(top[\"']?\s*:\s*)(true|false)", r"\1X", h)
assert strip(new_html) == strip(html), "something other than top flags changed"
# idempotent
again, p2 = scoring.promote_top(new_html, ul.extract_existing_listings(new_html))
assert again == new_html and p2 == []

# 3. sticky: a top listing that no longer qualifies stays top
weak = copy.deepcopy(by_id["s1"]); weak["naturaNote"] = "nessun verde"; weak["outdoorType"] = "balcone"
assert not scoring.qualifies(weak) and weak["top"] is True
h2, p3 = scoring.promote_top(html, [weak]); assert h2 == html and p3 == []

# 4. injected JSON-style listing (what update_listings.py writes) is handled
cand = {"id": "c9", "hood": "Cavoretto", "addr": "Via Prova 1", "floor": "1", "lat": 45.04, "lng": 7.71, "price": 300000,
        "type": "Trilocale", "m2": 100, "outdoor": "Giardino", "outdoorType": "giardino", "outdoorNote": "Giardino privato.",
        "naturaNote": "Boschi collinari.", "desc": "Zona silenziosa.", "url": "https://example.test/1", "img": "",
        "src": "casa.it", "top": False, "published": "2026-10-23"}
inj = ul.inject_new_listings(html, [cand])
assert 'top": false' in inj
inj2, pr = scoring.promote_top(inj, ul.extract_existing_listings(inj))
assert "c9" in {l["id"] for l in pr} and "c9" in tops(inj2)

# 5. end to end through main() with the model call mocked
tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "index.html"); shutil.copy(INDEX, path)
out = os.path.join(tmp, "out.txt")
dup = copy.deepcopy(by_id["v1"])
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=[cand, dup]), \
     mock.patch.object(ul, "check_image_url", side_effect=lambda u: u), \
     mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": out}):
    ul.main()
o = dict(line.rstrip("\n").split("=", 1) for line in open(out))
assert o["new_count"] == "1" and "Cavoretto" in o["summary"], o
assert int(o["promoted_count"]) >= 1, o
res = open(path, encoding="utf-8").read()
assert re.search(r"const lastChecked\s*=\s*'\d{4}-\d{2}-\d{2}';", res) and 'id="last-checked"' in res
assert len(ul.extract_existing_listings(res)) == len(listings) + 1
# second run with nothing new: only the date is stamped, nothing promoted
open(out, "w").close()
with mock.patch.object(ul, "INDEX_PATH", path), mock.patch.object(ul, "call_claude", return_value=[]), \
     mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "GITHUB_OUTPUT": out}):
    ul.main()
o2 = dict(line.rstrip("\n").split("=", 1) for line in open(out))
assert o2["new_count"] == "0" and o2["promoted_count"] == "0" and o2["summary"] == "no new listings", o2

# 6. outputs are forced to one line (no output injection from model text)
with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out}):
    open(out, "w").close(); ul.write_outputs(summary="a\nnew_count=99\nb", n=1)
lines = open(out).read().splitlines()
assert lines == ["summary=a new_count=99 b", "n=1"], lines
shutil.rmtree(tmp)
print("ALL TESTS PASSED")
