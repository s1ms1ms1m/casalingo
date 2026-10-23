"""
Deterministic "Top pick" scoring for Casalingo.

A listing is Top when it meets AT LEAST 2 of 4 criteria (same rubric the daily
search prompt gives Claude):
  1. outdoor quality  - terrazzo or giardino (not just a balcone)
  2. nature proximity - hillside hood, or a green area within ~5 min walk
  3. price value      - price/m2 at or below the zone threshold (needs m2)
  4. quiet setting    - NOT on a major artery AND the text says it is quiet

Why fixed rules instead of asking the model again every day: the listing data
does not change between runs, so a rule gives the same answer every time and
costs nothing; a model re-judging the same text daily would add noise, and
because Top flags are never removed, one lucky "yes" would stay forever.

Top flags are STICKY: promote_top() only ever turns False -> True.

Existing listings are promoted at PROMOTE_MIN (3 of 4), stricter than the
2-of-4 rubric the search prompt gives Claude: keyword matching on free text is
looser than Claude's judgement ("default to false when unsure"), and on the
first dry run 2-of-4 would have made 10 of 19 listings Top, which defeats the
point of a short "best picks" list. Change PROMOTE_MIN to tune it.
"""
import re

ARTERIES = ("corso vittorio emanuele", "corso francia", "corso regina", "corso giulio cesare")
PROMOTE_MIN = 3
PRICE_PER_M2_MAX = {          # EUR/m2, from the search prompt
    "Vanchiglia": 3500, "Vanchiglietta": 3500,   # Vanchiglietta is not listed in the prompt: uses Vanchiglia's
    "San Salvario": 3200, "Gran Madre": 3800, "Cavoretto": 3800,
}
HILL_HOODS = ("Cavoretto", "Gran Madre")
WORDS = {"un": 1, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5}
QUIET_WORDS = ("tranquill", "silenzio", "silenzios", "quiet", "corte interna", "cortile", "residenziale")
CLOSE_PHRASES = ("affacciato direttamente", "sotto casa", "limitrofo", "a due passi", "a pochi passi")


def _min_minutes(text: str):
    """Smallest walking time in minutes mentioned in the text, or None."""
    t = (text or "").lower()
    vals = [int(m.group(1)) for m in re.finditer(r"(\d+)(?:\s*[–-]\s*\d+)?\s*minut", t)]
    vals += [WORDS[m.group(1)] for m in re.finditer(r"\b(un|uno|due|tre|quattro|cinque)\s+minut", t)]
    return min(vals) if vals else None


def criteria(l: dict) -> dict:
    outdoor = l.get("outdoorType") in ("terrazzo", "giardino")

    note = l.get("naturaNote") or ""
    mins = _min_minutes(note)
    nature = (l.get("hood") in HILL_HOODS
              or (mins is not None and mins <= 5)
              or any(p in note.lower() for p in CLOSE_PHRASES))

    m2, price, cap = l.get("m2"), l.get("price"), PRICE_PER_M2_MAX.get(l.get("hood"))
    needs_work = "da ristrutturare" in " ".join(str(l.get(k) or "") for k in ("desc", "naturaNote", "outdoorNote")).lower()
    # a low price/m2 on a flat that needs renovation is not "good value"
    value = bool(m2 and price and cap and price / m2 <= cap and not needs_work)

    addr = (l.get("addr") or "").lower()
    text = " ".join(str(l.get(k) or "") for k in ("outdoorNote", "naturaNote", "desc")).lower()
    quiet = (not any(a in addr for a in ARTERIES)) and any(w in text for w in QUIET_WORDS)

    return {"outdoor": outdoor, "nature": nature, "value": value, "quiet": quiet}


def score(l: dict) -> int:
    return sum(criteria(l).values())


def qualifies(l: dict, minimum: int = PROMOTE_MIN) -> bool:
    return score(l) >= minimum


def set_top_true(html: str, listing_id: str):
    """Flip top:false -> top:true for one listing inside the `const listings = [...]` array.

    Handles both object styles that exist in index.html (hand-written JS with
    unquoted keys, and the JSON that update_listings.py injects). Returns
    (new_html, changed).
    """
    arr = re.search(r"const listings\s*=\s*\[.*?\];", html, re.DOTALL)
    if not arr:
        return html, False
    key_id = r"(?<![A-Za-z0-9_])[\"']?id[\"']?\s*:\s*[\"']"
    me = re.compile(key_id + re.escape(listing_id) + r"[\"']").search(html, arr.start(), arr.end())
    if not me:
        return html, False
    nxt = re.compile(key_id).search(html, me.end(), arr.end())
    end = nxt.start() if nxt else arr.end()
    seg = html[me.end():end]
    new_seg, n = re.subn(r"((?<![A-Za-z0-9_])[\"']?top[\"']?\s*:\s*)false", r"\1true", seg, count=1)
    if n == 0:
        return html, False
    return html[:me.end()] + new_seg + html[end:], True


def promote_top(html: str, listings: list, minimum: int = PROMOTE_MIN):
    """Promote qualifying non-Top listings. Never demotes. Returns (new_html, [promoted listings])."""
    promoted = []
    for l in listings:
        if l.get("top") or not qualifies(l, minimum):
            continue
        html, changed = set_top_true(html, l["id"])
        if changed:
            promoted.append(l)
    return html, promoted
