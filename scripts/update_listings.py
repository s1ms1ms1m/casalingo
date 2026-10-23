#!/usr/bin/env python3
"""
Casalingo — daily listing updater
Calls the Anthropic API to search for new apartments and adds them to index.html.
Requires: ANTHROPIC_API_KEY environment variable.
"""

import os
import re
import sys
import json
import datetime
import urllib.request
import urllib.error
import anthropic
import json5  # handles JS object syntax: unquoted keys, single-quoted strings
import scoring
import linkcheck

# ── Config ──────────────────────────────────────────────────────────────────

INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "index.html")

SEARCH_PROMPT = """You are updating a personal apartment-tracking website for Torino, Italy.

## YOUR TASK
Search the web for NEW apartment listings matching the criteria below.
Return ONLY a valid JSON array of new listing objects — nothing else.
If you find no new listings, return an empty array: []

## SEARCH CRITERIA
- City: Torino, Italy
- Budget: €200,000 – €700,000
- Rooms: 2 or more (bilocale, trilocale, quadrilocale…)
- Outdoor space: balcony, terrace or garden — REQUIRED
- Target neighbourhoods: Vanchiglia, Vanchiglietta, San Salvario, Gran Madre, Cavoretto, Borgo Dora, pre-collina, collina, Corso Casale
- Sources to search: immobiliare.it, idealista.it, wikicasa.it, casa.it

## SEARCH QUERIES TO RUN (run all of them)
- site:immobiliare.it trilocale Vanchiglia Torino balcone
- site:immobiliare.it appartamento San Salvario Torino terrazzo
- site:idealista.it bilocale Gran Madre Torino balcone
- site:idealista.it appartamento Cavoretto Torino terrazzo
- site:wikicasa.it trilocale Vanchiglietta Torino balcone
- site:wikicasa.it appartamento Corso Casale Torino
- site:casa.it trilocale Vanchiglia Torino balcone
- site:casa.it appartamento San Salvario Torino terrazzo

## EXISTING LISTINGS (do NOT re-add these — deduplicate by url AND addr)
{existing_json}

## OUTPUT FORMAT
Return a JSON array. Each object must have EXACTLY these fields:

{{
  "id": "v20",
  "hood": "Vanchiglia",
  "addr": "Via X 1",
  "floor": "Da verificare",
  "lat": 45.0706,
  "lng": 7.6944,
  "price": 355000,
  "type": "Trilocale",
  "m2": 93,
  "outdoor": "2 balconi",
  "outdoorType": "balcone",
  "outdoorNote": "Brief note about the outdoor space.",
  "naturaNote": "Brief note about greenery or nature nearby.",
  "desc": "1-2 sentence description.",
  "url": "https://...",
  "img": "",
  "src": "immobiliare.it",
  "top": false,
  "published": "{today}"
}}

## FIELD RULES
- id: prefix v=Vanchiglia, vt=Vanchiglietta, s=San Salvario, c=Cavoretto/GranMadre + sequential number
- hood MUST be one of: "Vanchiglia" | "Vanchiglietta" | "San Salvario" | "Gran Madre" | "Cavoretto"
- outdoorType MUST be one of: "balcone" | "terrazzo" | "giardino"
- src MUST be one of: "immobiliare.it" | "idealista.it" | "Wikicasa" | "casa.it"
- price: integer, no € symbol
- top: set true when the listing scores well on AT LEAST 2 of these 4 criteria:
    1. OUTDOOR QUALITY — has a terrazzo or giardino (not just a balcone)
    2. NATURE PROXIMITY — within ~5 min walk of the Po riverfront, a hillside (collina/pre-collina), or a large park (Valentino, Michelotti, Lungo Dora)
    3. PRICE VALUE — price/m² is notably below zone average (Vanchiglia ≤€3.500/m², San Salvario ≤€3.200/m², Gran Madre/Cavoretto ≤€3.800/m²); if m² unknown, price is at least 10% below comparable listings
    4. QUIET SETTING — not fronting a major traffic artery (no Corso Vittorio Emanuele, Corso Francia, Corso Regina, Corso Giulio Cesare); residential street or courtyard
  Default to false when unsure.
- img: copy an image URL EXACTLY as it appears in a page you actually read (e.g. an <img> src or og:image).
  NEVER construct, guess or complete an image URL. If you did not see one, use the empty string "".
  The site shows a neutral placeholder for an empty img, which is far better than a broken link.
- Neighbourhood coordinates (WGS84):
  Vanchiglia: 45.0706, 7.6944 | Vanchiglietta: 45.0680, 7.6970
  San Salvario: 45.0520, 7.6780 | Gran Madre: 45.0620, 7.7010
  Cavoretto: 45.0490, 7.7050 | Borgo Dora: 45.0800, 7.6830
  Corso Casale: 45.0640, 7.7030

Return ONLY the JSON array. No explanation, no markdown, no code fences.
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def extract_existing_listings(html: str) -> list[dict]:
    """Pull the listings array out of index.html."""
    match = re.search(r'const listings\s*=\s*(\[.*?\]);', html, re.DOTALL)
    if not match:
        raise ValueError("Could not find 'const listings = [...]' in index.html")
    try:
        # Use json5 to handle JS object syntax (unquoted keys, single quotes, trailing commas)
        return json5.loads(match.group(1))
    except Exception as e:
        raise ValueError(f"Could not parse listings JS: {e}")


def update_last_checked(html: str, date: str) -> str:
    """Set the lastChecked date (runs every day, even with no new listings)."""
    new_html, n = re.subn(r"const lastChecked\s*=\s*'[^']*';", f"const lastChecked = '{date}';", html, count=1)
    if n == 0:
        raise ValueError("Could not find 'const lastChecked' in index.html")
    return new_html


def inject_new_listings(html: str, new_listings: list[dict]) -> str:
    """Insert new listing objects at the top of the const listings array."""
    if not new_listings:
        return html
    new_js = ",\n    ".join(
        json.dumps(l, ensure_ascii=False, indent=2).replace('\n', '\n    ')
        for l in new_listings
    )
    replacement = f"const listings = [\n    {new_js},\n    "
    return re.sub(r'const listings\s*=\s*\[', replacement, html, count=1)


class SearchError(RuntimeError):
    """The search step produced no usable answer. Different from a clean "nothing new" ([]):
    a broken search must turn the run red instead of being committed as "no new listings"."""


LAST_SEARCH: dict = {}      # diagnostics of the latest call_claude(); main() writes them to the job summary
MAX_CONTINUATIONS = 3       # web search can pause a long turn (stop_reason "pause_turn"); we resume a few times


def parse_candidates(text_blocks):
    """The candidate list from the model's text blocks, or None if there is no usable JSON array.

    Web search interleaves narration ("Let me search...") with the answer, so the concatenated text is NOT
    valid JSON even when the answer is fine. Order: the last text block as a whole (code fences allowed);
    otherwise the LAST JSON array in the text whose items are all objects. Never guesses beyond that."""
    def as_list(t):
        t = re.sub(r"^```(?:json)?\s*", "", t.strip())
        t = re.sub(r"\s*```$", "", t)
        try:
            v = json.loads(t)
        except ValueError:
            return None
        return v if isinstance(v, list) and all(isinstance(x, dict) for x in v) else None

    blocks = [b.strip() for b in text_blocks if b and b.strip()]
    if not blocks:
        return None
    last = as_list(blocks[-1])
    if last is not None:
        return last
    text, dec, best, i = "\n".join(blocks), json.JSONDecoder(), None, 0
    i = text.find("[")
    while i != -1:
        try:
            v, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("[", i + 1)
            continue
        if isinstance(v, list) and all(isinstance(x, dict) for x in v):
            best = v
        i = text.find("[", end)
    return best


def _tool_stats(content):
    """(number of web searches, distinct error codes, Counter of result hosts) from the response blocks."""
    from collections import Counter
    from urllib.parse import urlparse
    searches, errors, hosts = 0, set(), Counter()
    for b in content:
        t = getattr(b, "type", "")
        if t == "server_tool_use":
            searches += 1
        elif t == "web_search_tool_result":
            c = getattr(b, "content", None)
            if isinstance(c, list):
                for r in c:
                    u = getattr(r, "url", None)
                    if u:
                        hosts[(urlparse(u).netloc.lower() or "?").removeprefix("www.")] += 1
            elif getattr(c, "error_code", None):
                errors.add(str(c.error_code))
    return searches, sorted(errors), hosts


def call_claude(existing: list[dict]) -> list[dict]:
    """Ask Claude (via API) to find new listings. Raises SearchError if the answer is unusable."""
    api_key = os.environ["ANTHROPIC_API_KEY"]
    print(f"API key present: {'yes' if api_key else 'no'} (length: {len(api_key)})")

    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.date.today().isoformat()

    # Compact representation of existing listings for dedup
    existing_compact = [{"url": l.get("url", ""), "addr": l.get("addr", "")} for l in existing]

    prompt = SEARCH_PROMPT.format(
        existing_json=json.dumps(existing_compact, ensure_ascii=False),
        today=today
    )

    print(f"Calling Anthropic API (anthropic SDK version: {anthropic.__version__})…")
    messages = [{"role": "user", "content": prompt}]
    content, tokens_in, tokens_out, stop, continuations = [], 0, 0, None, 0
    for attempt in range(MAX_CONTINUATIONS + 1):
        try:
            response = client.beta.messages.create(
                betas=["web-search-2025-03-05"],
                model="claude-opus-4-5",
                max_tokens=8192,     # narration between searches counts too; 4096 risked cutting the JSON off
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 12
                }],
                messages=messages
            )
        except Exception as e:
            print(f"ERROR calling Anthropic API: {type(e).__name__}: {e}", file=sys.stderr)
            # Write a summary of the error to GitHub step summary
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary_path:
                with open(summary_path, "a") as f:
                    f.write(f"### ❌ API Error\n```\n{type(e).__name__}: {e}\n```\n")
            raise
        content += list(response.content)
        stop = response.stop_reason
        usage = getattr(response, "usage", None)
        tokens_in += int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out += int(getattr(usage, "output_tokens", 0) or 0)
        print(f"API response received. Stop reason: {stop}")
        if stop != "pause_turn":
            break
        continuations += 1
        # resume the paused turn: hand back everything produced so far as the assistant message
        messages = [messages[0], {"role": "assistant", "content": list(content)}]

    texts = [b.text for b in content if getattr(b, "type", "") == "text" and getattr(b, "text", None)]
    searches, errors, hosts = _tool_stats(content)
    preview = " ".join((texts[-1] if texts else "").split())[:300]
    LAST_SEARCH.clear()
    LAST_SEARCH.update(stop_reason=stop, web_searches=searches, search_errors=errors, result_hosts=hosts.most_common(8),
                       text_blocks=len(texts), text_chars=sum(len(t) for t in texts), tokens_in=tokens_in,
                       tokens_out=tokens_out, continuations=continuations, preview=preview)
    print(f"Search report: stop_reason={stop}, web searches={searches}, tool errors={errors or 'none'}, "
          f"text blocks={len(texts)}, tokens in/out={tokens_in}/{tokens_out}, continuations={continuations}")
    print(f"Result hosts: {hosts.most_common(8) or 'none'}")
    print(f"Final text starts: {preview!r}")

    result = parse_candidates(texts)
    if result is None:
        if not texts:
            raise SearchError(f"the reply contained no text (stop_reason={stop})")
        raise SearchError(f"no JSON array found in the reply (stop_reason={stop}); it starts: {preview[:120]!r}")
    if stop not in ("end_turn", "stop_sequence"):
        print(f"WARNING: reply ended with stop_reason={stop} but a complete JSON array was found; using it.", file=sys.stderr)
    return result


def check_image_url(url: str, timeout: float = 8.0) -> str:
    """Return url if it looks usable, else "".

    Conservative on purpose: only drop an image on a DEFINITE failure
    (malformed URL, 404/410, or a 200 that is not an image). Network errors,
    timeouts and 401/403 (hotlink protection that may block the bot but not
    visitors) keep the URL; the page shows a placeholder if the image fails.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https://[^\s/]+/\S*$", url):
        print(f"  image dropped (not an https URL): {url[:80]}")
        return ""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CasalingoBot/1.0)", "Range": "bytes=0-0"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if not ctype.startswith("image/"):
                print(f"  image dropped (content-type {ctype!r}): {url[:80]}")
                return ""
            return url
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            print(f"  image dropped (HTTP {e.code}): {url[:80]}")
            return ""
        print(f"  image kept (HTTP {e.code}, cannot confirm): {url[:80]}")
        return url
    except Exception as e:
        print(f"  image kept ({type(e).__name__}, cannot confirm): {url[:80]}")
        return url


def deduplicate(candidates: list[dict], existing: list[dict]) -> list[dict]:
    """Remove candidates already in the existing list (by url or addr)."""
    existing_urls = {l.get("url", "").strip().lower() for l in existing}
    existing_addrs = {l.get("addr", "").strip().lower() for l in existing}

    fresh = []
    for c in candidates:
        url = c.get("url", "").strip().lower()
        addr = c.get("addr", "").strip().lower()
        if url and url in existing_urls:
            continue
        if addr and addr in existing_addrs:
            continue
        fresh.append(c)
    return fresh


def write_search_summary(status: str, funnel=None):
    """Job-summary section: what the search did and where candidates were lost. Makes "found nothing new"
    distinguishable from "search broke"."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    clean = lambda x: " ".join(str(x).replace("|", "/").replace("`", "'").split())
    d = LAST_SEARCH
    lines = [f"### 🔎 Search report: {clean(status)}", ""]
    if d:
        lines += [f"- stop reason: `{d['stop_reason']}` · web searches: {d['web_searches']} · continuations: {d['continuations']}"
                  f" · tokens in/out: {d['tokens_in']}/{d['tokens_out']}",
                  f"- search tool errors: {', '.join(d['search_errors']) or 'none'}",
                  "- result hosts: " + (", ".join(f"{h} ×{n}" for h, n in d["result_hosts"]) or "none")]
        if not funnel:
            lines.append(f"- final text starts: {clean(d['preview'])!r}")
    if funnel:
        total, known, new = funnel
        lines.append(f"- model returned **{total}** candidate(s): **{len(known)}** already on the site, **{new}** new")
        for c in known[:8]:
            lines.append(f"  - already known: {clean(c.get('addr', '?'))} ({clean(c.get('src', '?'))})")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # Read current site
    with open(INDEX_PATH, encoding="utf-8") as f:
        html = f.read()

    existing = extract_existing_listings(html)
    print(f"Found {len(existing)} existing listings.")

    # Ask Claude to find new ones
    print("Searching for new listings via Anthropic API…")
    try:
        candidates = call_claude(existing)
    except SearchError as e:
        print(f"ERROR: search failed: {e}", file=sys.stderr)
        write_search_summary(f"❌ Search failed: {e}")
        raise
    print(f"Claude returned {len(candidates)} candidate listing(s).")

    # Deduplicate
    new_listings = deduplicate(candidates, existing)
    print(f"After deduplication: {len(new_listings)} new listing(s).")
    known = [c for c in candidates if c not in new_listings]
    write_search_summary("✅ Search answered", funnel=(len(candidates), known, len(new_listings)))

    # Guard against invented image URLs: an unusable one becomes "" (placeholder on the site)
    for l in new_listings:
        l["img"] = check_image_url(l.get("img", ""))

    # Always stamp the check date, so the site shows the script ran
    today = datetime.date.today().isoformat()
    updated_html = update_last_checked(html, today)

    if new_listings:
        updated_html = inject_new_listings(updated_html, new_listings)
        print(f"Added {len(new_listings)} new listing(s):")
        for l in new_listings:
            print(f"  • {l.get('hood')} — {l.get('addr')} — €{l.get('price'):,} ({l.get('type')})")
    else:
        print("Nothing new — only the last-checked date is updated.")

    # Re-score every listing (old and new) with the fixed Top rules.
    # Top flags are sticky (only false -> true) and topLocked listings are skipped.
    before = extract_existing_listings(updated_html)
    updated_html, promoted = scoring.promote_top(updated_html, before)
    # Safety net: re-read what we are about to write and make sure NOTHING but the
    # intended flags changed. If not, fail loudly instead of publishing wrong data.
    after = extract_existing_listings(updated_html)
    scoring.verify_promotions(before, after, [l["id"] for l in promoted])
    if promoted:
        print(f"Promoted {len(promoted)} listing(s) to Top:")
        for l in promoted:
            print(f"  ★ {l.get('hood')} — {l.get('addr')} — criteria: "
                  + ", ".join(k for k, v in scoring.criteria(l).items() if v))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("### Top scoring (promotion needs "
                    f"{scoring.PROMOTE_MIN} of 4; existing Top flags are never removed)\n\n"
                    + scoring.markdown_report(after, [l["id"] for l in promoted]) + "\n\n")

    # Dead-listing check. Isolated on purpose: whatever happens here, the update above still goes out.
    updated_html, link_info = check_links(updated_html, after, {l["id"] for l in new_listings}, today)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(updated_html)

    summary = "; ".join(
        f"{l.get('hood')} {l.get('type')} €{l.get('price'):,}" for l in new_listings
    ) or "no new listings"
    promoted_summary = "; ".join(f"{l.get('hood')} {l.get('addr')}" for l in promoted)
    write_outputs(new_count=len(new_listings), summary=summary,
                  promoted_count=len(promoted), promoted_summary=promoted_summary,
                  gone_count=link_info["gone_count"], link_summary=link_info["summary"])


def check_links(html: str, listings: list, new_ids: set, today: str):
    """Run the dead-listing check and return (html_with_updated_linkStatus, info).

    Never raises: on any problem the html is returned unchanged and the problem is logged."""
    info = {"gone_count": 0, "summary": ""}
    try:
        prev = linkcheck.read_state(html)
        locked = {l["id"] for l in listings if l.get("skipLinkCheck")}
        results = linkcheck.check_all(listings, today, skip_ids=new_ids | locked)
        state, changes = linkcheck.update_state(prev, listings, results, today)
        new_html = linkcheck.write_state(html, state)
        if extract_existing_listings(new_html) != listings:      # the link step must not touch listing data
            raise RuntimeError("link step changed the listings array")
    except Exception as e:                                         # noqa: BLE001 - deliberately broad
        print(f"WARNING: dead-listing check skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return html, info

    by_id = {l["id"]: l for l in listings}
    label = lambda i: f"{by_id[i].get('hood')} {by_id[i].get('addr')}"
    parts = []
    if changes["gone"]:
        parts.append("no longer online: " + ", ".join(label(i) for i in changes["gone"]))
    if changes["revived"]:
        parts.append("back online: " + ", ".join(label(i) for i in changes["revived"]))
    if changes["suspicious"]:
        parts.append("ignored suspicious results from: " + ", ".join(changes["suspicious"]))
    info = {"gone_count": len(changes["gone"]), "summary": "; ".join(parts)}

    tally, reasons = {}, {}
    for i, (res, why) in results.items():
        p = linkcheck.portal_of(by_id[i].get("url", "")) or "?"
        tally.setdefault(p, {"alive": 0, "dead": 0, "unknown": 0})[res] += 1
        if res != "alive":
            r = " ".join(str(why).replace("|", "/").split())[:50]
            reasons.setdefault(p, {}).setdefault(r, 0)
            reasons[p][r] += 1
    for p, t in sorted(tally.items()):
        print(f"Link check {p}: {t['alive']} alive, {t['dead']} dead, {t['unknown']} unknown")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("### Link check (a listing is marked gone after "
                    f"{linkcheck.DEAD_AFTER} consecutive dead results; blocked or unclear answers change nothing)\n\n")
            f.write("| portale | alive | dead | unknown | motivi (non alive) |\n|---|---|---|---|---|\n")
            for p, t in sorted(tally.items()):
                why = ", ".join(f"{r} ×{n}" for r, n in sorted(reasons.get(p, {}).items(), key=lambda kv: -kv[1])[:3])
                f.write(f"| {p} | {t['alive']} | {t['dead']} | {t['unknown']} | {why or '-'} |\n")
            f.write("\n" + linkcheck.markdown_report(listings, results, state) + "\n\n")
    return new_html, info


def write_outputs(**values):
    """Write step outputs for the workflow. Values are forced to one line: a newline
    in a model-provided string would otherwise inject extra outputs."""
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
        for k, v in values.items():
            f.write(f"{k}={' '.join(str(v).split())[:500]}\n")


if __name__ == "__main__":
    main()
