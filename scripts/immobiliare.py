"""
immobiliare.it discovery by READING the portal's own search pages, instead of asking a search engine.

Why: the web-search tool returned stale pages (dead ads, copies weeks old) and the per-ad check could not tell a live
ad from a dead one. Measured on 27 Oct 2026 (GitHub runner, default settings, no evasion):
  * our own servers get 403 from immobiliare.it (plain HTTP and headless Chromium alike);
  * Anthropic's web-fetch tool DOES read immobiliare.it search pages live (retrieved_at = request time, 25 ads per page,
    ~10-12k input tokens) and ad pages (stored copies up to 5 days old were seen, so every copy's age is checked);
  * a search page lists, per ad: price, link with a numeric ad ID, and a title "<type> <street> <n>, <zone>, Torino".
    It does NOT list m2, rooms or outdoor space: those come from the ad page, whose feature line reads
    "Contratto Vendita Piano 9 Piani edificio 9 Ascensore Sì Superficie 75 m² Locali 3 ... Balcone Sì Terrazzo No".
  * auctions appear as "Appartamento all'asta ..." with "da € 245.850,00"; they are skipped.

So: everything checkable is parsed by code (no model involved): price, floor, m2, rooms, balcony/terrace/garden, contract
type, canonical ad ID, age of the copy. The model only writes the short Italian texts (desc, outdoorNote, naturaNote)
from the ad's own description, and a failure there falls back to a plain sentence: it can never change a fact.

Checks that reject an ad (all fail closed; nothing is guessed):
  search page not a live copy (older than SEARCH_MAX_AGE_H) or without ads -> the page is not used at all;
  ad page unreadable / not this ad (canonical ID differs) / not "Vendita" / copy older than MAX_AGE_DAYS;
  price on the ad page not among the prices printed on its card (the card proves the ad is listed today at that price);
  no balcony/terrace/garden stated, fewer than 2 rooms.

Known limits: "in trattativa" / "venduto" labels are NOT visible in the text the fetcher returns (24 Oct, s7/s9), so a
listing still shown on the search page but in negotiation can be added; coordinates are not in the page, so the pin is
the neighbourhood centre moved by at most ~100 m (as with the existing listings, approximate).
"""
import datetime
import json
import os
import re
import sys

import availability

MODEL = os.environ.get("IMMOBILIARE_MODEL", "claude-haiku-4-5")
BASE = "https://www.immobiliare.it/vendita-case/torino/{slug}/?criterio=data&ordine=desc&prezzoMinimo=180000&prezzoMassimo=700000&localiMinimo=2"
SEARCH_PAGES = [                      # (label, slug, page): newest first; Vanchiglia has two pages (it has the most ads)
    ("vanchiglia", "vanchiglia", 1), ("vanchiglia p2", "vanchiglia", 2), ("vanchiglietta", "vanchiglietta", 1),
    ("san-salvario", "san-salvario", 1), ("cavoretto-gran-madre", "cavoretto-gran-madre", 1),
]
PRICE_MIN, PRICE_MAX = 180_000, 700_000
SEARCH_MAX_AGE_H = 24                 # a search page must be a live copy; an older one may list sold ads
MAX_AGE_DAYS = availability.MAX_AGE_DAYS
MAX_EXAMINE = 10                      # ad pages read per run (cost cap); the rest wait for the next day
RETRY_LIMIT = 3                       # an ad that could not be read is retried on 3 different days, then dropped

SEEN_PATH = os.path.join(os.path.dirname(__file__), "immobiliare_seen.txt")
PERMANENT = {"added", "no_outdoor", "few_rooms", "gone", "not_for_sale"}      # never examined again
RETRYABLE = {"unreadable", "stale", "mismatch"}                                # examined again, up to RETRY_LIMIT times

HOOD_OF_ZONE = {                      # zone label on the card -> the site's `hood`; anything else is not a target area
    "vanchiglia": "Vanchiglia", "vanchiglietta": "Vanchiglietta", "san salvario": "San Salvario",
    "gran madre": "Gran Madre", "madonna del pilone": "Gran Madre", "cavoretto": "Cavoretto",
}
HOOD_COORDS = {"Vanchiglia": (45.0706, 7.6944), "Vanchiglietta": (45.0680, 7.6970), "San Salvario": (45.0520, 7.6780),
               "Gran Madre": (45.0620, 7.7010), "Cavoretto": (45.0490, 7.7050)}
ID_PREFIX = {"Vanchiglia": "v", "Vanchiglietta": "vt", "San Salvario": "s", "Gran Madre": "c", "Cavoretto": "c"}
TYPE_WORDS = ("Monolocale", "Bilocale", "Trilocale", "Quadrilocale", "Pentalocale", "Appartamento", "Attico", "Mansarda", "Loft")
STREET = re.compile(r"^(?:via|viale|corso|c\.so|piazza|piazzale|largo|strada|stradale|lungo|lungodora|vicolo|galleria|str\.)\b", re.I)

LINK = re.compile(r'\[([^\]]+)\]\((https://www\.immobiliare\.it/annunci/(\d{5,})/?)(?:\s+"[^"]*")?\)')
PRICE = re.compile(r"€\s?(\d{1,3}(?:\.\d{3})+)(?:,\d{2})?")


class ImmobiliareError(RuntimeError):
    """The portal could not be read at all (no live search page): a fault to report, not a finding of 'nothing new'."""


# ── Pure parsing (no network, no model) ─────────────────────────────────────

def ad_id(url):
    """The numeric ad ID of an immobiliare.it ad URL, else None."""
    m = re.match(r"^https?://(?:www\.)?immobiliare\.it/(?:en/)?annunci/(\d{5,})(?:[/?#]|$)", (url or "").strip(), re.I)
    return m.group(1) if m else None


def hood_of(zone):
    z = re.sub(r"\s*[-–].*$", "", (zone or "").strip().lower())          # "San Salvario - Dante" -> "san salvario"
    return HOOD_OF_ZONE.get(z)


def parse_cards(text):
    """Cards of a search page, in page order: [{id, url, title, price, prices, zone, hood, type, addr}].

    The price is printed just BEFORE the title link, so a card's prices are the ones between the previous link and this one.
    Auction cards ("all'asta", price "da € ...") are returned with auction=True so the caller can count and skip them."""
    cards, seen, prev = [], set(), 0
    for m in LINK.finditer(text or ""):
        prices = [int(p.replace(".", "")) for p in PRICE.findall(text[prev:m.start()])]
        prev = m.end()
        if m.group(3) in seen:
            continue
        seen.add(m.group(3))
        title = " ".join(m.group(1).split())
        parts = [p.strip() for p in title.split(",")]
        if parts and parts[-1].lower() == "torino":
            parts = parts[:-1]
        zone = parts[-1] if len(parts) > 1 else ""
        head = ", ".join(parts[:-1])
        tm = re.match(r"(" + "|".join(TYPE_WORDS) + r")\b\s*(.*)$", head, re.I)
        typ, rest = (tm.group(1).capitalize(), tm.group(2)) if tm else ("", head)
        rest = re.sub(r"\s+Torino$", "", re.sub(r",\s*(\d)", r" \1", rest.strip()))
        cards.append({"id": m.group(3), "url": m.group(2), "title": title, "zone": zone, "hood": hood_of(zone), "type": typ,
                      "addr": rest if STREET.match(rest) else "", "price": prices[-1] if prices else None, "prices": prices,
                      "auction": bool(re.search(r"\basta\b", title, re.I))})
    return cards


def parse_ad(text):
    """Facts of an ad page, read by regex from the fetched text. Missing facts are None, never guessed."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text or "")                  # drop image markdown (alt text says "Balcone" for photos)
    t = " ".join(t.split())
    g = lambda pat: (m.group(1) if (m := re.search(pat, t, re.I)) else None)
    # floor text is free-form: "9", "T", "Piano rialzato", "Piano terra, piano rialzato", "Interrato (-1), 10" (seen on real pages 28 Oct)
    feat = re.search(r"Contratto\s+(\w+)\s+Piano\s+(.+?)(?=\s+Piani edificio|\s+Ascensore|\s+Superficie|\s+Locali|$)(?:\s+Piani edificio\s+(\d+))?(?:\s+Ascensore\s+(Sì|No))?", t)
    m2 = g(r"Superficie\s+(\d+(?:[.,]\d+)?)\s*m²")
    price = g(r"Informazioni sul prezzo\s+Prezzo\s+€\s?(\d{1,3}(?:\.\d{3})+)")
    yes = lambda label: (None if (v := g(label + r"\s+(Sì|No)\b")) is None else v.lower() == "sì")
    garden = bool(re.search(r"\bGiardino\s+(?:privato|comune|condominiale)\b|\bGiardino\s+Sì\b", t, re.I))
    locali = g(r"\bLocali\s+(\d+)")
    return {
        "canonical_id": g(r"canonical:\s*https://www\.immobiliare\.it/annunci/(\d+)"),
        "contract": feat.group(1).lower() if feat else None,
        "floor_raw": feat.group(2) if feat else None,
        "elevator": None if not feat or not feat.group(4) else feat.group(4) == "Sì",
        "m2": round(float(m2.replace(",", "."))) if m2 else None,
        "rooms": int(locali) if locali else None,
        "price": int(price.replace(".", "")) if price else None,
        "balcone": yes("Balcone"), "terrazzo": yes("Terrazzo"), "giardino": garden,
        "updated": g(r"Annuncio aggiornato il (\d{2}/\d{2}/\d{4})"),
        "image": g(r"meta-og:image:\s*(https://\S+)"),
        "gone": bool(re.search(r"non è più disponibile|La pagina che stai cercando non è presente|annuncio non (?:è )?più", t, re.I)),
        "description": (m.group(1) if (m := re.search(r"Descrizione\s+(.{80,4000}?)(?:\s+leggi tutto\b|\s+Caratteristiche\b|$)", t)) else ""),
    }


def floor_text(raw, elevator):
    """Site wording for the floor. The page text is free-form; a positive number wins ("Interrato (-1), 10" = 10th floor)."""
    r = re.sub(r"^piano\s+", "", (raw or "").strip().lower())
    nums = [int(x) for x in re.findall(r"(?<![-\d])\d+", r)]
    if nums:
        base = f"{max(nums)}° piano"
    elif "rialzato" in r and "terra" in r:
        base = "Piano terra / rialzato"
    elif "rialzato" in r or r == "r":
        base = "Piano rialzato"
    elif "seminterrato" in r or "interrato" in r or r in ("s", "s1"):
        base = "Seminterrato"
    elif "terra" in r or r == "t":
        base = "Piano terra"
    else:
        return "Da verificare"
    return base + ("" if elevator is None else " · ascensore" if elevator else " · senza ascensore")


def outdoor_of(ad):
    """(outdoor text, outdoorType) from the stated features, or (None, None) when no outdoor space is stated."""
    names = [n for n, ok in (("Terrazzo", ad["terrazzo"]), ("Giardino", ad["giardino"]), ("Balcone", ad["balcone"])) if ok]
    if not names:
        return None, None
    kind = "terrazzo" if ad["terrazzo"] else "giardino" if ad["giardino"] else "balcone"
    low = [n.lower() for n in names[1:]]
    text = names[0] + (", ".join(low[:-1]) and ", " + ", ".join(low[:-1]) or "") + (f" e {low[-1]}" if low else "")
    return text, kind


def type_of(card_type, rooms):
    if card_type in ("Appartamento", "") and rooms:
        return {2: "Bilocale", 3: "Trilocale", 4: "Quadrilocale"}.get(rooms, f"{rooms} locali" if rooms > 4 else "Appartamento")
    return card_type or "Appartamento"


def pin(hood, ad_number):
    """Neighbourhood centre moved by at most ~100 m (deterministic from the ad ID) so new pins do not stack."""
    lat, lng = HOOD_COORDS[hood]
    n = int(ad_number)
    return round(lat + ((n % 17) - 8) * 0.00011, 5), round(lng + ((n // 17 % 17) - 8) * 0.00015, 5)


def next_id(hood, taken):
    p = ID_PREFIX[hood]
    nums = [int(m.group(1)) for t in taken if (m := re.fullmatch(re.escape(p) + r"(\d+)", str(t)))]
    return f"{p}{max(nums or [0]) + 1}"


def age_days(retrieved_at, now):
    try:
        got = datetime.datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if got.tzinfo is None:
        got = got.replace(tzinfo=datetime.timezone.utc)
    return (now - got).total_seconds() / 86400


def clean_text(s, limit):
    """Model text made safe for the public page: one line, no URLs / e-mails / phone-like numbers, capped."""
    s = " ".join(str(s or "").split())
    if re.search(r"https?://|www\.|@|(?:\+?\d[\s./-]?){9,}", s):
        return ""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    dot = max(cut.rfind(". "), cut.rfind("."))                         # end at the last full sentence if there is a decent one
    if dot >= limit // 3:
        return cut[:dot + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…" if " " in cut else cut


def parse_texts(texts):
    """The model's {desc, outdoorNote, naturaNote} object from its text blocks, or {}."""
    t, dec = "\n".join(texts), json.JSONDecoder()
    i = t.rfind("{")
    while i != -1:
        try:
            v, _ = dec.raw_decode(t, i)
            if isinstance(v, dict) and "desc" in v:
                return v
        except ValueError:
            pass
        i = t.rfind("{", 0, i)
    return {}


# ── Seen file ───────────────────────────────────────────────────────────────

def load_seen(path=None):
    """{ad id: [status, ...]} from the seen file (tab-separated: id, date, status, note). Missing file = empty."""
    out = {}
    try:
        lines = open(path or SEEN_PATH, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for l in lines:
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        p = l.split("\t")
        if len(p) >= 3 and p[0].strip().isdigit():
            out.setdefault(p[0].strip(), []).append(p[2].strip())
    return out


def is_done(statuses):
    return any(s in PERMANENT for s in statuses) or sum(s in RETRYABLE for s in statuses) >= RETRY_LIMIT


def append_seen(rows, path=None):
    """Append (id, date, status, note) rows. Notes are one short line of our own words, never page text."""
    if not rows:
        return
    p = path or SEEN_PATH
    new = not os.path.exists(p)
    with open(p, "a", encoding="utf-8") as f:
        if new:
            f.write("# immobiliare.it ads already examined: id<TAB>date<TAB>status<TAB>note. Statuses added/no_outdoor/few_rooms/gone/"
                    "not_for_sale are final; unreadable/stale/mismatch are retried on up to 3 days. Written by the daily job.\n")
        for r in rows:
            f.write("\t".join(" ".join(str(x).split()) for x in r) + "\n")


# ── Network steps (client is injected so tests need no network) ─────────────

def _fetch(client, url, prompt, max_tokens):
    resp = client.beta.messages.create(model=MODEL, max_tokens=max_tokens, betas=availability.BETAS, tools=[availability.TOOL],
                                       messages=[{"role": "user", "content": prompt.replace("__URL__", url)}])
    usage = getattr(resp, "usage", None)
    texts = [b.text for b in resp.content if getattr(b, "type", "") == "text" and getattr(b, "text", None)]
    return (availability.fetch_outcome(resp.content), texts,
            int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "output_tokens", 0) or 0))


SEARCH_PROMPT = "Open __URL__ with the web_fetch tool, then reply with the single word: done"
AD_PROMPT = """Open this property ad with the web_fetch tool: __URL__
Using ONLY what the ad page says, reply with ONLY this JSON (Italian text, no markdown):
{"desc": "at most 2 short sentences (under 250 characters in total) describing the flat for a buyer; no price, no address, no agency, no phone, no e-mail",
 "outdoorNote": "one short sentence about the balcony/terrace/garden ONLY if the ad gives detail beyond its existence (size, view, exposure, access); otherwise \\"\\"",
 "naturaNote": "one short sentence about parks, river, hills or green areas near the flat, ONLY if the ad text says so (copy any walking time exactly as written), else \\"\\""}"""


def read_search_pages(client, now, pages=SEARCH_PAGES):
    """([cards], [page report lines], tokens). A page is used only if it is a live copy with ads; others are reported."""
    cards, report, tokens, ok = {}, [], 0, 0
    for label, slug, page in pages:
        url = BASE.format(slug=slug) + (f"&pag={page}" if page > 1 else "")
        try:
            f, _, tok = _fetch(client, url, SEARCH_PROMPT, 200)
        except Exception as e:                                           # noqa: BLE001
            report.append(f"{label}: API error {type(e).__name__}")
            print(f"  search page {label}: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            continue
        tokens += tok
        age = age_days(f["retrieved_at"], now) if f["fetched"] else None
        found = parse_cards(f["text"]) if f["fetched"] else []
        if not f["fetched"]:
            why = ",".join(f["errors"]) or ("fetch tool not used" if not f["called"] else "empty page")
            report.append(f"{label}: not readable ({why})")
        elif age is None:
            report.append(f"{label}: no retrieval date")
        elif age * 24 > SEARCH_MAX_AGE_H:
            report.append(f"{label}: stale copy ({age:.1f} days old), not used")
        elif not found:
            report.append(f"{label}: no ads found in the page text")
        else:
            ok += 1
            report.append(f"{label}: {len(found)} ads, live copy ({age * 24:.1f} h old)")
            for c in found:
                cards.setdefault(c["id"], c)
        print(f"  search page {report[-1]}")
    if ok == 0:
        raise ImmobiliareError("no immobiliare.it search page could be read as a live copy: " + "; ".join(report))
    return list(cards.values()), report, tokens, ok


def examine(client, card, today, now):
    """Read one ad page. -> (listing dict or None, status, note, tokens). Status is a key of PERMANENT/RETRYABLE or 'added'."""
    try:
        f, texts, tok = _fetch(client, card["url"], AD_PROMPT, 700)
    except Exception as e:                                               # noqa: BLE001
        print(f"  ad {card['id']}: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        raise
    ad = parse_ad(f["text"]) if f["fetched"] else None
    if not f["fetched"]:
        why = ",".join(f["errors"]) or ("fetch tool not used" if not f["called"] else "empty page")
        return None, "unreadable", f"page not fetchable ({why})", tok
    if ad["gone"] and ad["canonical_id"] != card["id"]:
        return None, "gone", "page says the ad is no longer available", tok
    if ad["canonical_id"] != card["id"]:
        return None, "mismatch", f"page canonical ID {ad['canonical_id']} is not this ad", tok
    if ad["contract"] is None:
        return None, "mismatch", "no contract type on the page (partial copy?)", tok
    if ad["contract"] != "vendita":
        return None, "not_for_sale", f"contract type {ad['contract']!r}", tok
    age = age_days(f["retrieved_at"], now)
    if age is None or age > MAX_AGE_DAYS:
        return None, "stale", f"copy of the ad page is {'of unknown age' if age is None else f'{age:.0f} days old'}", tok
    if ad["price"] is None or ad["price"] not in card["prices"]:
        return None, "mismatch", f"ad page price {ad['price']} is not among the card prices {card['prices']}", tok
    if not (PRICE_MIN <= ad["price"] <= PRICE_MAX):
        return None, "mismatch", f"price {ad['price']} outside the budget", tok
    if ad["rooms"] is None or ad["rooms"] < 2:
        return None, "few_rooms", f"rooms: {ad['rooms']}", tok
    outdoor, kind = outdoor_of(ad)
    if not outdoor:
        return None, "no_outdoor", "no balcony, terrace or garden stated", tok
    model = parse_texts(texts)
    typ = type_of(card["type"], ad["rooms"])
    zone_name = card["zone"] or card["hood"]
    addr = card["addr"] or f"Indirizzo non indicato ({zone_name})"
    lat, lng = pin(card["hood"], card["id"])
    fallback = f"{typ} di {ad['m2']} m² a {card['hood']}." if ad["m2"] else f"{typ} a {card['hood']}."
    listing = {
        "id": "", "hood": card["hood"], "addr": addr, "floor": floor_text(ad["floor_raw"], ad["elevator"]),
        "lat": lat, "lng": lng, "price": ad["price"], "type": typ, "m2": ad["m2"], "outdoor": outdoor, "outdoorType": kind,
        "outdoorNote": clean_text(model.get("outdoorNote"), 200), "naturaNote": clean_text(model.get("naturaNote"), 220),
        "desc": clean_text(model.get("desc"), 320) or fallback, "url": card["url"], "img": ad["image"] or "",
        "src": "immobiliare.it", "top": False, "published": today,
    }
    return listing, "added", f"read {str(f['retrieved_at'])[:10]}, updated {ad['updated'] or '?'}", tok


def discover(client, existing, removed_urls, seen, today=None, now=None, max_examine=MAX_EXAMINE, pages=SEARCH_PAGES):
    """Find new immobiliare.it listings by reading the search pages.

    Returns {listings, seen_rows, report (one line), details (list of lines), tokens}. Raises ImmobiliareError when no
    search page could be read live, or when every ad read raised an exception (a systematic fault)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = today or now.date().isoformat()
    cards, page_report, tokens, pages_ok = read_search_pages(client, now, pages)
    known = {i for l in existing if (i := ad_id(l.get("url")))}
    removed = {i for u in removed_urls if (i := ad_id("https://" + u))}
    counts = {"cards": len(cards), "auction": 0, "other_zone": 0, "other_type": 0, "no_price": 0, "out_of_budget": 0,
              "on_site": 0, "removed": 0, "done_before": 0}
    todo = []
    for c in cards:
        if c["auction"]:
            counts["auction"] += 1
        elif not c["hood"]:
            counts["other_zone"] += 1
        elif not c["type"]:
            counts["other_type"] += 1
        elif c["price"] is None:
            counts["no_price"] += 1
        elif not PRICE_MIN <= c["price"] <= PRICE_MAX:
            counts["out_of_budget"] += 1
        elif c["id"] in known:
            counts["on_site"] += 1
        elif c["id"] in removed:
            counts["removed"] += 1
        elif is_done(seen.get(c["id"], [])):
            counts["done_before"] += 1
        else:
            todo.append(c)
    todo.sort(key=lambda c: int(c["id"]), reverse=True)            # larger ID = newer ad
    batch = todo[:max_examine]
    listings, rows, outcome, errors, taken, lines = [], [], {}, 0, {l.get("id") for l in existing}, []
    for c in batch:
        try:
            l, status, note, tok = examine(client, c, today, now)
        except Exception:                                                   # noqa: BLE001
            errors += 1
            outcome["error"] = outcome.get("error", 0) + 1
            continue
        tokens += tok
        outcome[status] = outcome.get(status, 0) + 1
        rows.append((c["id"], today, status, note))
        if l:
            l["id"] = next_id(l["hood"], taken)
            taken.add(l["id"])
            listings.append(l)
        lines.append(f"{c['id']} {c['hood']} {c['addr'] or c['zone']} €{c['price']:,}: {status} — {note}")
        print(f"  ad {lines[-1]}")
    if batch and errors == len(batch):
        raise ImmobiliareError(f"all {errors} ad reads raised an exception (see log); nothing was added")
    skipped = " ".join(f"{k} {v}" for k, v in counts.items() if v and k != "cards")
    report = (f"immobiliare.it read live: {pages_ok}/{len(pages)} search pages, {counts['cards']} ads"
              f" ({skipped or 'none filtered'}); {len(todo)} new to examine, {len(batch)} examined today"
              f" ({', '.join(f'{k} {v}' for k, v in sorted(outcome.items())) or 'none'}), {len(listings)} added"
              + (f", {len(todo) - len(batch)} wait for the next run" if len(todo) > len(batch) else ""))
    return {"listings": listings, "seen_rows": rows, "report": report, "details": page_report + lines, "tokens": tokens}


def main():
    """Dry run for a look by hand: python scripts/immobiliare.py  (reads the pages, prints what it would add, writes nothing)."""
    import anthropic
    sys.path.insert(0, os.path.dirname(__file__))
    import update_listings as ul
    html = open(ul.INDEX_PATH, encoding="utf-8").read()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    r = discover(client, ul.extract_existing_listings(html), ul.load_removed(), load_seen())
    print(r["report"])
    for l in r["listings"]:
        print(json.dumps(l, ensure_ascii=False))


if __name__ == "__main__":
    main()
