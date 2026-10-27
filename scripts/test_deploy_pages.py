#!/usr/bin/env python3
"""Tests for scripts/deploy_pages.sh, run against a fake `gh` (no network, no GitHub).

The fake is told what to do through environment variables:
  STUB_DISPATCH_FAILS=N   the first N `gh workflow run` calls fail
  STUB_POLL=a,b,c         what successive polling calls print; each item is "none" (no run yet),
                          "error" (the call fails), or "<status>:<conclusion>" (e.g. "in_progress:" or "completed:success")
                          once the list runs out the last item repeats
"""
import os, stat, subprocess, sys, tempfile, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deploy_pages.sh")

FAKE_GH = textwrap.dedent(r"""\
    #!/usr/bin/env bash
    d="$STUB_DIR"
    bump() { n=$(cat "$d/$1" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$d/$1"; echo $n; }
    if [ "$1 $2" = "workflow run" ]; then
      n=$(bump dispatches)
      [ "$n" -le "${STUB_DISPATCH_FAILS:-0}" ] && exit 1
      exit 0
    fi
    if [ "$1 $2" = "run list" ]; then
      # Like the real gh, apply the --jq filter the script passes (so the "newer than before" filter is exercised).
      jqx=""; prev=""; for a in "$@"; do [ "$prev" = "--jq" ] && jqx="$a"; prev="$a"; done
      case " $* " in
        *" --limit 1 "*) echo '[{"databaseId":100}]' | jq -r "$jqx"; exit 0 ;;   # latest run before ours
      esac
      n=$(bump polls)
      IFS=, read -ra items <<< "${STUB_POLL:-completed:success}"
      i=$((n-1)); [ "$i" -ge "${#items[@]}" ] && i=$((${#items[@]}-1))
      it="${items[$i]}"
      old='{"databaseId":99,"status":"completed","conclusion":"failure"}'      # an older run: must be ignored
      case "$it" in
        none)  echo "[$old]" | jq -r "$jqx"; exit 0 ;;
        error) exit 1 ;;
        *)     st="${it%%:*}"; co="${it#*:}"
               echo "[$old,{\"databaseId\":101,\"status\":\"$st\",\"conclusion\":\"$co\"}]" | jq -r "$jqx"; exit 0 ;;
      esac
    fi
    echo "unexpected gh call: $*" >&2; exit 99
""")

def run(poll="completed:success", dispatch_fails=0, attempts=3, max_polls=4, args=("main",)):
    with tempfile.TemporaryDirectory() as d:
        gh = os.path.join(d, "gh")
        with open(gh, "w") as f: f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], STUB_DIR=d, STUB_POLL=poll,
                   STUB_DISPATCH_FAILS=str(dispatch_fails), DEPLOY_ATTEMPTS=str(attempts), DEPLOY_RETRY_WAIT="0",
                   DEPLOY_POLL_WAIT="0", DEPLOY_MAX_POLLS=str(max_polls))
        r = subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60)
        def count(name):
            try: return int(open(os.path.join(d, name)).read())
            except OSError: return 0
        return r.returncode, r.stdout + r.stderr, count("dispatches"), count("polls")

failures = 0
def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f"   <- {detail}"))
    if not cond: failures += 1

# 1. happy path: the run goes queued -> in progress -> success
rc, out, disp, polls = run("queued:,in_progress:,completed:success")
check("success: exit 0", rc == 0, out)
check("success: started once, polled until completed (3 polls)", disp == 1 and polls == 3, f"disp={disp} polls={polls}")
check("success: says so", "finished: success" in out, out)

# 2. a transient failure to start is retried
rc, out, disp, polls = run(dispatch_fails=2)
check("2 failed starts then ok: exit 0 after 3 attempts", rc == 0 and disp == 3, f"rc={rc} disp={disp}\n{out}")

# 3. the start never works: red, no polling
rc, out, disp, polls = run(dispatch_fails=99)
check("start always fails: exit 1", rc == 1, out)
check("start always fails: tried exactly 3 times and never polled", disp == 3 and polls == 0, f"disp={disp} polls={polls}")
check("start always fails: ::error:: annotation", "::error title=GitHub Pages was not redeployed::" in out, out)
check("start always fails: tells the owner how to fix it", "Run workflow" in out, out)

# 4. the deploy runs but fails / is cancelled
for concl in ("failure", "cancelled", "timed_out", "startup_failure"):
    rc, out, *_ = run(f"completed:{concl}")
    check(f"deploy ends '{concl}': exit 1", rc == 1 and concl in out, f"rc={rc}\n{out}")

# 5. never finishes, or no run ever appears
rc, out, disp, polls = run("in_progress:", max_polls=3)
check("never completes: exit 1 after 3 polls", rc == 1 and polls == 3, f"rc={rc} polls={polls}\n{out}")
rc, out, disp, polls = run("none", max_polls=3)
check("no run appears: exit 1", rc == 1 and polls == 3, f"rc={rc} polls={polls}\n{out}")

# 6. a failing status call is retried, not fatal
rc, out, disp, polls = run("error,none,completed:success", max_polls=5)
check("transient status errors are tolerated", rc == 0 and polls == 3, f"rc={rc} polls={polls}\n{out}")

# 7. an older run that failed must not be mistaken for ours (the fake always lists run 99 = failure)
rc, out, *_ = run("completed:success")
check("older failed run (id <= before) is ignored", rc == 0, out)

# 8. usage
rc, out, *_ = run(args=())
check("no branch argument: exit 2", rc == 2, f"rc={rc}\n{out}")

print("\nFAILURES:", failures)
sys.exit(1 if failures else 0)
