"""
Deterministic "Top pick" scoring for Casalingo.

A listing is Top when it meets enough of 4 criteria:
  1. outdoor quality  - terrazzo or giardino (not just a balcone)
  2. nature proximity - hillside hood, or a green area within ~5 min WALK
  3. price value      - price/m2 at or below the zone threshold (needs m2),
                        and the flat does not need renovation
  4. quiet setting    - NOT on a major artery AND the text explicitly says quiet

Why fixed rules instead of asking the model again every day: the listing data
does not change between runs, so a rule gives the same answer every time and
costs nothing; a model re-judging the same text daily would add noise, and
because Top flags are never removed, one lucky "yes" would stay forever.

Guarantees (all covered by scripts/test_scoring.py):
  * STICKY   - promote_top() only ever turns top:false into top:true.
  * LOCKABLE - a listing with  topLocked: true  is never touched (use it to keep
               a listing out of Top by hand, or to freeze a hand-picked one).
  * SAFE     - the edit is confined to the listing's own {...} object, and
               verify_promotions() re-checks that nothing except the intended
               flags changed; the caller must abort (not write) if it raises.

Threshold: PROMOTE_MIN = 3 of 4 for promotion. NEW listings always enter as top:false (the search prompt
and update_listings.main() both force it), so this one rule decides Top for old and new listings alike; the
model's own looser 2-of-4 judgement is not used. Known consequence: a listing flagged Top before this rule
existed (s1, Corso Dante, 2 of 4) stays Top because flags are sticky. Change PROMOTE_MIN to tune.
"""
import re

PROMOTE_MIN = 3
ARTERIES = ("corso vittorio emanuele", "corso francia", "corso regina", "corso giulio cesare")
PRICE_PER_M2_MAX = {          # EUR/m2, from the search prompt
    "Vanchiglia": 3500, "Vanchiglietta": 3500,   # Vanchiglietta is not listed in the prompt: uses Vanchiglia's
    "San Salvario": 3200, "Gran Madre": 3800, "Cavoretto": 3800,
}
HILL_HOODS = ("Cavoretto", "Gran Madre")
WORDS = {"un": 1, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5}
CLOSE_PHRASES = ("affacciato direttamente", "sotto casa", "limitrofo", "a due passi", "a pochi passi")
# "quiet" needs an explicit statement. Courtyard / "residenziale" alone are NOT evidence of a quiet street.
QUIET_WORDS = ("tranquill", "silenzio", "quiet")
NOISY_WORDS = ("rumor", "traffic")

_TIME = re.compile(r"(?<!\w)(\d+(?:\s*[–-]\s*\d+)?|un|uno|due|tre|quattro|cinque)\s+minut")
_TRANSPORT_AFTER = re.compile(
    r"\b(in|con|di)\s+(auto|macchina|bici|bicicletta|moto|scooter|metro|tram|bus|autobus|treno|mezzi|taxi)\b")
_TRANSPORT_BEFORE = re.compile(r"(fermata|metro|tram|bus|autobus|stazione|treno|parcheggio)")


def walk_minutes(text: str):
    """Smallest WALKING time in minutes mentioned in the text, or None.

    Ignores times given for a car/bike/transit ("in auto", "in bici"), times for a
    stop or station ("fermata ... a 3 minuti"), and "più di / oltre / almeno N".
    For a range ("7-8 minuti") the upper bound is used: when unsure, say no.
    """
    t = (text or "").lower()
    best = None
    for m in _TIME.finditer(t):
        before = t[max(0, m.start() - 25):m.start()]
        after = t[m.end():m.end() + 30]
        if re.search(r"(più di|oltre|almeno)\s*$", before):
            continue
        if _TRANSPORT_BEFORE.search(before[-20:]) or _TRANSPORT_AFTER.search(after):
            continue
        raw = m.group(1)
        nums = [int(x) for x in re.findall(r"\d+", raw)] or [WORDS[raw]]
        v = max(nums)
        best = v if best is None else min(best, v)
    return best


def criteria(l: dict) -> dict:
    outdoor = l.get("outdoorType") in ("terrazzo", "giardino")

    note = l.get("naturaNote") or ""
    mins = walk_minutes(note)
    nature = (l.get("hood") in HILL_HOODS
              or (mins is not None and mins <= 5)
              or any(p in note.lower() for p in CLOSE_PHRASES))

    m2, price, cap = l.get("m2"), l.get("price"), PRICE_PER_M2_MAX.get(l.get("hood"))
    needs_work = "da ristrutturare" in " ".join(str(l.get(k) or "") for k in ("desc", "naturaNote", "outdoorNote")).lower()
    # a low price/m2 on a flat that needs renovation is not "good value"
    value = bool(m2 and price and cap and price / m2 <= cap and not needs_work)

    addr = (l.get("addr") or "").lower()
    text = " ".join(str(l.get(k) or "") for k in ("outdoorNote", "naturaNote", "desc")).lower()
    quiet = (not any(a in addr for a in ARTERIES)
             and any(w in text for w in QUIET_WORDS)
             and not any(w in text for w in NOISY_WORDS))

    return {"outdoor": outdoor, "nature": nature, "value": value, "quiet": quiet}


def score(l: dict) -> int:
    return sum(criteria(l).values())


def qualifies(l: dict, minimum: int = PROMOTE_MIN) -> bool:
    return score(l) >= minimum


# ── editing index.html safely ────────────────────────────────────────────────

def _listing_spans(html: str):
    """(start, end) of every top-level {...} object inside `const listings = [ ... ]`.

    Walks the text honouring quotes (with escapes) and comments, so braces or
    apostrophes inside strings cannot confuse it.
    """
    m = re.search(r"const listings\s*=\s*\[", html)
    if not m:
        return []
    spans, depth, start, quote, i, n = [], 0, None, None, m.end(), len(html)
    while i < n:
        c = html[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif html.startswith("//", i):
            j = html.find("\n", i)
            i = n if j == -1 else j
            continue
        elif html.startswith("/*", i):
            j = html.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, i + 1))
                start = None
        elif c == "]" and depth == 0:
            break
        i += 1
    return spans


def set_top_true(html: str, listing_id: str):
    """Flip top:false -> top:true inside ONE listing's own object. Returns (new_html, changed).

    Refuses (returns unchanged) if the id is missing or appears in more than one
    object: it never guesses. Handles both object styles in index.html
    (hand-written JS with unquoted keys, and the JSON update_listings.py injects)
    and any key order.
    """
    key_id = re.compile(r"""(?<![A-Za-z0-9_])["']?id["']?\s*:\s*["']""" + re.escape(listing_id) + r"""["']""")
    hits = [(s, e) for s, e in _listing_spans(html) if key_id.search(html[s:e])]
    if len(hits) != 1:
        return html, False
    s, e = hits[0]
    seg, n = re.subn(r"""((?<![A-Za-z0-9_])["']?top["']?\s*:\s*)false""", r"\1true", html[s:e], count=1)
    if n == 0:
        return html, False
    return html[:s] + seg + html[e:], True


def promote_top(html: str, listings: list, minimum: int = PROMOTE_MIN):
    """Promote qualifying non-Top, non-locked listings. Never demotes.
    Returns (new_html, [promoted listings])."""
    promoted = []
    for l in listings:
        if l.get("top") or l.get("topLocked") or not qualifies(l, minimum):
            continue
        html, changed = set_top_true(html, l["id"])
        if changed:
            promoted.append(l)
    return html, promoted


def verify_promotions(before: list, after: list, promoted_ids) -> None:
    """Raise ValueError unless `after` equals `before` except top:false->true on promoted_ids.
    Call on the re-parsed file BEFORE writing it."""
    promoted_ids = set(promoted_ids)
    if [l.get("id") for l in before] != [l.get("id") for l in after]:
        raise ValueError("listing ids/order changed")
    for b, a in zip(before, after):
        expected = dict(b)
        if b["id"] in promoted_ids:
            expected["top"] = True
        if a != expected:
            raise ValueError(f"unexpected change in listing {b['id']}")


def markdown_report(listings: list, promoted_ids) -> str:
    """Per-listing criteria table for the job summary (so each promotion can be audited)."""
    promoted_ids = set(promoted_ids)
    clean = lambda s: " ".join(str(s).replace("|", "/").split())[:60]
    rows = ["| id | indirizzo | Top | punti | criteri |", "|---|---|---|---|---|"]
    for l in listings[:100]:
        if l["id"] in promoted_ids:
            mark = "★ NEW"
        elif l.get("topLocked"):
            mark = "🔒" + (" ★" if l.get("top") else "")
        else:
            mark = "★" if l.get("top") else ""
        hit = ", ".join(k for k, v in criteria(l).items() if v) or "-"
        rows.append(f"| {clean(l['id'])} | {clean(l.get('addr', ''))} | {mark} | {score(l)} | {hit} |")
    return "\n".join(rows)
