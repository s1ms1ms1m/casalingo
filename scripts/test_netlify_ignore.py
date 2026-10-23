#!/usr/bin/env python3
"""Tests for scripts/netlify-ignore.js. Run: python3 scripts/test_netlify_ignore.py

Builds throwaway git repos (two commits) and runs the REAL script with CACHED_COMMIT_REF / COMMIT_REF set,
exactly as Netlify does. Exit 0 = Netlify skips the build; 1 = builds. The dangerous direction is a wrong
"skip" (site not updated), so most cases assert 1.
"""
import os, re, shutil, subprocess, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "netlify-ignore.js")
BASE = open(os.path.join(HERE, "fixtures", "index_sample.html"), encoding="utf-8").read()
# the frozen fixture may predate the linkStatus line (added with the dead-listing check): make sure both lines exist
if not re.search(r"^const lastChecked\s*=", BASE, re.M):
    BASE = BASE.replace("const listings = [", "const lastChecked = '2026-01-01';\nconst listings = [", 1)
if not re.search(r"^const linkStatus\s*=", BASE, re.M):
    BASE = BASE.replace("const listings = [", "const linkStatus = {};\nconst listings = [", 1)
assert re.search(r"^const lastChecked\s*=\s*'[^']*';", BASE, re.M) and re.search(r"^const linkStatus\s*=\s*\{.*\};", BASE, re.M)
if not shutil.which("node"):
    print("node not found: skipping netlify-ignore tests"); sys.exit(0)

def g(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()

def with_state(html, state_json, date=None, comment=""):
    html = re.sub(r"^const linkStatus\s*=\s*\{.*\};.*$", lambda m: f"const linkStatus = {state_json};{comment}", html, count=1, flags=re.M)
    if date:
        html = re.sub(r"^const lastChecked\s*=\s*'[^']*';", f"const lastChecked = '{date}';", html, count=1, flags=re.M)
    return html

def run(first, second, env_over=None, setup=None):
    """first/second: {path: content or None (delete)}. Returns exit code of the script."""
    d = tempfile.mkdtemp()
    try:
        g(d, "init", "-q", "-b", "main")
        def commit(files, msg):
            for p, c in files.items():
                fp = os.path.join(d, p)
                if c is None:
                    os.remove(fp); continue
                os.makedirs(os.path.dirname(fp), exist_ok=True); open(fp, "w", encoding="utf-8", newline="").write(c)
            g(d, "add", "-A"); g(d, "commit", "-q", "--allow-empty", "-m", msg); return g(d, "rev-parse", "HEAD")
        a = commit(first, "one"); b = commit(second, "two")
        env = {**os.environ, "CACHED_COMMIT_REF": a, "COMMIT_REF": b}
        env.update(env_over or {})
        for k in [k for k, v in env.items() if v is None]: del env[k]
        if setup: setup(d, env)
        r = subprocess.run(["node", SCRIPT], cwd=d, env=env, capture_output=True, text=True)
        return r.returncode, r.stdout
    finally:
        shutil.rmtree(d)

S0 = '{"a":{"s":"ok","f":0,"u":0},"b":{"s":"ok","f":2,"u":0}}'
S_COUNTERS = '{"a":{"s":"ok","f":1,"u":1},"b":{"s":"ok","f":0,"u":0}}'        # counters only
S_NEWGONE = '{"a":{"s":"ok","f":0,"u":0},"b":{"s":"gone","f":3,"u":0}}'        # b became gone
S_REVIVED_BASE = S_NEWGONE
S_REVIVED = '{"a":{"s":"ok","f":0,"u":0},"b":{"s":"ok","f":0,"u":0}}'          # b is back
CMT = "   // written daily; do not edit by hand"
h0 = with_state(BASE, S0, "2026-10-20", CMT)
cases = []
def case(name, expect, *a, **k): cases.append((name, expect, a, k))

# ── must SKIP (0)
case("date only", 0, {"index.html": h0}, {"index.html": with_state(h0, S0, "2026-10-21", CMT)})
case("date + counters only (same gone set)", 0, {"index.html": h0}, {"index.html": with_state(h0, S_COUNTERS, "2026-10-21", CMT)})
case("several skipped days accumulate (cumulative diff)", 0, {"index.html": h0}, {"index.html": with_state(h0, S_COUNTERS, "2026-10-25", CMT)})
case("only scripts/.github/.gitignore", 0, {"index.html": h0, "scripts/x.py": "1"}, {"scripts/x.py": "2", ".github/w.yml": "a", ".gitignore": "x"})
case("date + scripts together", 0, {"index.html": h0}, {"index.html": with_state(h0, S0, "2026-10-21", CMT), "scripts/y.py": "z"})
case("CRLF line endings do not matter", 0, {"index.html": h0}, {"index.html": with_state(h0, S0, "2026-10-21", CMT).replace("\n", "\r\n")})
case("no changes at all", 0, {"index.html": h0}, {})

def _flip(h):
    n = re.sub(r"top:\s*false", "top:true", h, count=1)
    assert n != h, "test bug: no top:false in fixture"
    return n

# ── must BUILD (1): anything real
def sub(h, a, b):
    assert a in h, f"test bug: {a!r} not in fixture"      # a no-op mutation would make a 'must build' case meaningless
    return h.replace(a, b, 1)
case("listing price changed + date", 1, {"index.html": h0}, {"index.html": with_state(sub(h0, "price:", "price: 1+"), S0, "2026-10-21", CMT)})
case("top flag flipped (real scoring change)", 1, {"index.html": h0}, {"index.html": with_state(_flip(h0), S0, "2026-10-21", CMT)})
case("new listing appended", 1, {"index.html": h0}, {"index.html": with_state(sub(h0, "const listings = [", "const listings = [\n {id:'zz'},"), S0, "2026-10-21", CMT)})
case("listing newly gone", 1, {"index.html": h0}, {"index.html": with_state(h0, S_NEWGONE, "2026-10-21", CMT)})
case("listing revived", 1, {"index.html": with_state(h0, S_REVIVED_BASE, None, CMT)}, {"index.html": with_state(h0, S_REVIVED, "2026-10-21", CMT)})
case("css change", 1, {"index.html": h0}, {"index.html": h0 + "\n<style>/*x*/</style>\n"})
case("other published file added", 1, {"index.html": h0}, {"index.html": h0, "logo.png": "x"})
case("netlify.toml changed", 1, {"index.html": h0, "netlify.toml": "a"}, {"netlify.toml": "b"})
case("_redirects added", 1, {"index.html": h0}, {"_redirects": "/* /index.html 200"})
case("scripts change + real index change", 1, {"index.html": h0}, {"index.html": h0 + "\n<style>/*x*/</style>\n", "scripts/a.py": "1"})
case("index.html deleted", 1, {"index.html": h0}, {"index.html": None, "scripts/a.py": "1"})
case("file named like skip dir ('scripts.js')", 1, {"index.html": h0}, {"scripts.js": "alert(1)"})
case("'.github' lookalike dir ('.github2/')", 1, {"index.html": h0}, {".github2/x": "1"})
case("unparsable linkStatus changed", 1, {"index.html": with_state(h0, "{bad", None, CMT)}, {"index.html": with_state(h0, "{worse", "2026-10-21", CMT)})
case("lastChecked line removed", 1, {"index.html": h0}, {"index.html": re.sub(r"^const lastChecked.*\n", "", h0, count=1, flags=re.M)})
case("linkStatus added where there was none", 1, {"index.html": re.sub(r"^const linkStatus.*\n", "", h0, count=1, flags=re.M)}, {"index.html": h0})
case("gone flag hidden in nested value does not count as unchanged", 1, {"index.html": h0}, {"index.html": with_state(h0, '{"a":{"s":"gone","f":3,"u":0},"b":{"s":"ok","f":2,"u":0}}', "2026-10-21", CMT)})

# ── fail-safe on bad environment (must BUILD)
ok = {"index.html": h0}; dt = {"index.html": with_state(h0, S0, "2026-10-21", CMT)}
case("CACHED_COMMIT_REF missing", 1, ok, dt, env_over={"CACHED_COMMIT_REF": None})
case("COMMIT_REF missing", 1, ok, dt, env_over={"COMMIT_REF": None})
case("CACHED_COMMIT_REF empty", 1, ok, dt, env_over={"CACHED_COMMIT_REF": ""})
case("CACHED_COMMIT_REF unknown to git (e.g. shallow clone)", 1, ok, dt, env_over={"CACHED_COMMIT_REF": "deadbeef" * 5})
case("CACHED_COMMIT_REF is an injection attempt", 1, ok, dt, env_over={"CACHED_COMMIT_REF": "HEAD; touch /tmp/pwned"})
case("CACHED_COMMIT_REF is an option-like string", 1, ok, dt, env_over={"CACHED_COMMIT_REF": "--output=/tmp/pwned"})
def not_a_repo(d, env): shutil.rmtree(os.path.join(d, ".git"))
case("not a git repo", 1, ok, dt, setup=not_a_repo)

bad = 0
for name, expect, a, k in cases:
    code, out = run(*a, **k)
    if code != expect:
        bad += 1; print(f"FAIL {name}: expected exit {expect}, got {code}: {out.strip()}")
assert not os.path.exists("/tmp/pwned"), "command injection!"
if bad:
    raise SystemExit(f"{bad} of {len(cases)} cases failed")
print(f"ALL {len(cases)} NETLIFY-IGNORE CASES PASSED")
