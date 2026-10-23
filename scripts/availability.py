"""
Availability check for new listings, using Anthropic's web-fetch tool (stdlib + anthropic only).

Why: the search tool returns stale pages, and the portals answer 403 to our own servers, so the script cannot
read an ad itself. In a one-off experiment (9 listings checked by hand + the 19 originals) Anthropic's fetcher was
NOT blocked: it read idealista 2/2, immobiliare 5/5 + 2/2, wikicasa 8/12, and every listing the owner had found
removed failed to fetch (6/6), while a sold-but-still-online ad was read and correctly called sold.

Policy (fail closed): a candidate is accepted ONLY if
  1. the fetch tool really returned page text (no error code), and
  2. the model, reading that page, says it is currently for sale.
Anything else is rejected with a reason: page not fetchable (removed, or blocked: the API cannot tell which),
sold/removed, under offer, check failed. "Cannot verify" therefore means "not published today": a real listing is
only delayed, a dead one is never added.

Known weakness: an ad marked "in trattativa" was called available in the experiment (1 of 1), so the prompt now
names those labels explicitly; this is not yet validated beyond that one case.
"""
import json
import os
import sys

MODEL = os.environ.get("AVAILABILITY_MODEL", "claude-opus-4-5")
TOOL = {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}
BETAS = ["web-fetch-2025-09-10"]
MAX_CHECKS = 12            # per run; the rest are simply not added today (the search finds them again tomorrow)
PROMPT = """Open this property-listing page with the web_fetch tool and tell me whether the ad is CURRENTLY available to buy.
URL: __URL__
Use only what the fetched page says. Look at the title area, price area and any badge or banner.
- sold_or_removed: "venduto", "sold", "non disponibile", "annuncio non più disponibile", "annuncio scaduto", "questo annuncio non sembra esserci più", or the ad is gone.
- under_offer: "in trattativa", "sotto offerta", "riservato", "under offer", "reserved", "trattativa riservata".
- available: the page shows an active ad for sale with none of the labels above.
- cannot_tell: you could not fetch the page, or it does not show a property ad.
Reply with ONLY JSON: {"status": "available" | "under_offer" | "sold_or_removed" | "cannot_tell", "evidence": "<short exact quote from the page, or the reason you could not fetch it>"}"""


class VerifyError(RuntimeError):
    """Every check failed with an exception (tool not accepted, API down): a systematic fault, not a finding."""


def as_dict(x):
    if hasattr(x, "model_dump"):
        try:
            return x.model_dump()
        except Exception:                       # noqa: BLE001
            pass
    if isinstance(x, dict):
        return {k: as_dict(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [as_dict(v) for v in x]
    if hasattr(x, "__dict__"):
        return {k: as_dict(v) for k, v in vars(x).items() if not k.startswith("_")}
    return x


def walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


def fetch_outcome(content):
    """Did the fetch tool actually return a page? Read from the response blocks, never from the model's words."""
    blocks = [as_dict(b) for b in content]
    called = any(b.get("type") == "server_tool_use" and "fetch" in str(b.get("name", "")) for b in blocks)
    results = [b for b in blocks if "fetch" in str(b.get("type", "")) and b.get("type") != "server_tool_use"]
    errors = sorted({str(d["error_code"]) for r in results for d in walk(r) if d.get("error_code")})
    text = ""
    for r in results:
        for d in walk(r):
            for k in ("data", "text"):
                if isinstance(d.get(k), str) and len(d[k]) > len(text):
                    text = d[k]
    return {"called": called, "errors": errors, "chars": len(text), "fetched": bool(called and text.strip() and not errors)}


def parse_verdict(texts):
    t, dec = "\n".join(texts), json.JSONDecoder()
    i = t.rfind("{")
    while i != -1:
        try:
            v, _ = dec.raw_decode(t, i)
            if isinstance(v, dict) and "status" in v:
                return v
        except ValueError:
            pass
        i = t.rfind("{", 0, i)
    return None


def check_listing(client, url):
    """One listing -> {accept, reason, status, evidence, fetched, tokens_in, tokens_out}. Raises on API errors."""
    resp = client.beta.messages.create(model=MODEL, max_tokens=1024, betas=BETAS, tools=[TOOL],
                                       messages=[{"role": "user", "content": PROMPT.replace("__URL__", url)}])
    texts = [b.text for b in resp.content if getattr(b, "type", "") == "text" and getattr(b, "text", None)]
    usage = getattr(resp, "usage", None)
    f = fetch_outcome(resp.content)
    v = parse_verdict(texts) or {}
    status, evidence = str(v.get("status") or "none"), " ".join(str(v.get("evidence") or "").split())[:160]
    out = {"status": status, "evidence": evidence, "fetched": f["fetched"], "tokens_in": int(getattr(usage, "input_tokens", 0) or 0),
           "tokens_out": int(getattr(usage, "output_tokens", 0) or 0)}
    if not f["fetched"]:
        why = ",".join(f["errors"]) or ("fetch tool not used" if not f["called"] else "empty page")
        out.update(accept=False, reason=f"page not fetchable ({why}): removed or blocked, cannot verify")
    elif status == "available":
        out.update(accept=True, reason="page fetched, shows an active ad")
    elif status in ("sold_or_removed", "under_offer"):
        out.update(accept=False, reason=f"{status}: {evidence}")
    else:
        out.update(accept=False, reason=f"model could not tell ({status}): {evidence}")
    return out


def verify(client, listings, max_checks=MAX_CHECKS):
    """(accepted listings, [(listing, reason)] rejected, info). Fail closed; raises VerifyError if every check crashed."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    accepted, rejected, errors, tin, tout, evidence = [], [], 0, 0, 0, {}
    todo = list(listings)
    for l in todo[:max_checks]:
        try:
            r = check_listing(client, l.get("url", ""))
        except Exception as e:                  # noqa: BLE001
            errors += 1
            print(f"  availability check failed for {l.get('addr')}: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            rejected.append((l, f"check failed ({type(e).__name__}): not added"))
            continue
        tin += r["tokens_in"]; tout += r["tokens_out"]
        evidence[l.get("url", "")] = r["evidence"]       # never stored on the listing itself: it would be written into index.html
        if r["accept"]:
            accepted.append(l)
        else:
            rejected.append((l, r["reason"]))
        print(f"  availability {l.get('addr')}: {'OK' if r['accept'] else 'REJECTED'} — {r['reason'][:120]}")
    for l in todo[max_checks:]:
        rejected.append((l, f"not checked today (more than {max_checks} candidates)"))
    if todo and errors == min(len(todo), max_checks):
        raise VerifyError(f"all {errors} availability checks raised an exception (see log); nothing was added")
    return accepted, rejected, {"checked": min(len(todo), max_checks), "errors": errors, "tokens_in": tin, "tokens_out": tout, "evidence": evidence}


def main(argv):
    """CLI for on-demand checks: python scripts/availability.py URL [URL...]  -> prints one line per URL (no site changes)."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    for u in argv:
        try:
            r = check_listing(client, u)
            print(f"{'ACCEPT' if r['accept'] else 'REJECT'}\t{u}\t{r['reason']}")
        except Exception as e:                  # noqa: BLE001
            print(f"ERROR\t{u}\t{type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main(sys.argv[1:])
