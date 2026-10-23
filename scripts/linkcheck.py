"""
Dead-listing check for Casalingo (stdlib only).

Once a day, request each listing's page on the portal and decide, conservatively,
whether the ad is still online:

  alive    page answers 200 and still points at this listing
  dead     clear evidence it is gone: HTTP 404/410, a redirect away from the
           listing (the portal sends removed ads to a search page), or a
           "no longer available" headline in <title>/<h1>
  unknown  anything else (blocked, captcha, 403/429/5xx, timeout, odd redirect)

Design rules, because we cannot know in advance how each portal treats a bot:
  * "unknown" NEVER changes anything. Only "dead" counts.
  * A listing becomes "gone" only after DEAD_AFTER consecutive dead results.
  * A portal is trusted to say "dead" only once it has served us at least one
    real, alive page (otherwise a portal that answers 404 to every bot would
    wipe out its listings).
  * If a run sees dead on >= half of a portal's listings (3 or more checked),
    those results are discarded as suspicious.
  * Only https URLs on the known portals are ever requested (the URLs come from
    model output); redirects are followed by hand and must stay on those hosts.

State lives in index.html as one line, `const linkStatus = {...};`, owned by the
script: {id: {"s": "ok"|"gone", "f": consecutive_dead, "u": consecutive_unknown,
"a": last_alive_date, "c": last_check_date, "why": reason}}.
"""
import html as htmlmod
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_HOST_SUFFIXES = ("immobiliare.it", "idealista.it", "wikicasa.it", "casa.it")
DEAD_AFTER = 3            # consecutive dead results before a listing is "gone"
SUSPICIOUS_RATIO = 0.5    # dead share of a portal's listings that we refuse to believe
SUSPICIOUS_MIN = 3        # ...once at least this many were checked
MAX_BYTES = 262144
MAX_HOPS = 5
USER_AGENT = "Mozilla/5.0 (compatible; CasalingoBot/1.0; personal apartment tracker)"

# Matched against <title>, <h1> and og:title only. Body text is NOT trusted: removed ads
# are surrounded by "similar listings" widgets that contain words like "venduto".
# Phrases only: a bare "404" would match a street number, "non disponibile" a feature list.
DEAD_TITLE_MARKERS = (
    "non più disponibile", "non è più disponibile", "non e' più disponibile", "annuncio non disponibile",
    "annuncio scaduto", "annuncio rimosso", "non più online", "non esiste più",
    "pagina non trovata", "page not found", "errore 404", "error 404", "404 not found",
)
# Whole phrases / words, so "Corso Botanico" is not a bot wall.
BLOCK_TITLE_MARKERS = ("captcha", "just a moment", "access denied", "attention required", "are you human", "datadome")
BLOCK_TITLE_WORDS = re.compile(r"\b(robot|bot)\b")


def portal_of(url: str):
    """Allowed portal domain for this https URL, or None."""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host:
        return None
    for suf in ALLOWED_HOST_SUFFIXES:
        if host == suf or host.endswith("." + suf):
            return suf
    return None


def listing_ref(url: str):
    """The numeric ad id inside the URL path (longest run of 5+ digits), or None."""
    runs = re.findall(r"\d{5,}", urllib.parse.urlparse(url).path)
    return max(runs, key=len) if runs else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http_get(url: str, timeout: float):
    """One GET without following redirects. Returns (status, headers, body_bytes)."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5",
                                               "Accept-Language": "it-IT,it;q=0.9"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BYTES)
        except Exception:
            body = b""
        return e.code, e.headers, body


def _headlines(body: bytes, headers) -> str:
    charset = "utf-8"
    try:
        charset = headers.get_content_charset() or "utf-8"
    except Exception:
        pass
    text = body.decode(charset, errors="replace")
    parts = []
    for pat in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>",
                r"""<meta[^>]+property=["']og:title["'][^>]*content=["']([^"']*)["']""",
                r"""<meta[^>]+content=["']([^"']*)["'][^>]*property=["']og:title["']"""):
        for m in re.finditer(pat, text, re.I | re.S):
            parts.append(htmlmod.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
    return " ".join(" ".join(parts).lower().split())


def classify(url: str, timeout: float = 12.0, get=_http_get, host_ok=portal_of):
    """Returns (result, reason): result is 'alive' | 'dead' | 'unknown'."""
    if not host_ok(url):
        return "unknown", "url not on an allowed portal"
    ref = listing_ref(url)
    cur = url
    for _ in range(MAX_HOPS + 1):
        try:
            status, headers, body = get(cur, timeout)
        except Exception as e:
            return "unknown", f"{type(e).__name__}"
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("Location") if headers else None
            if not loc:
                return "unknown", f"http {status} without Location"
            nxt = urllib.parse.urljoin(cur, loc)
            if not host_ok(nxt):
                return "unknown", "redirect leaves the portal"
            cur = nxt
            continue
        if status in (404, 410):
            return "dead", f"http {status}"
        if status != 200:
            return "unknown", f"http {status}"
        heads = _headlines(body, headers)
        if any(b in heads for b in BLOCK_TITLE_MARKERS) or BLOCK_TITLE_WORDS.search(heads):
            return "unknown", "block / captcha page"
        if any(m in heads for m in DEAD_TITLE_MARKERS):
            return "dead", "headline says it is no longer available"
        if ref and ref not in urllib.parse.urlparse(cur).path:
            return "dead", "redirected away from the listing"
        return "alive", "ok"
    return "unknown", "too many redirects"


def check_all(listings: list, today: str, *, budget_s: float = 180.0, pause_s: float = 1.0,
              classify_fn=classify, skip_ids=()):
    """Classify every listing (except skip_ids), politely and within a time budget.
    Returns {id: (result, reason)}. Listings not reached in time are simply absent."""
    out, started = {}, time.monotonic()
    for l in listings:
        if l["id"] in skip_ids:
            continue
        if time.monotonic() - started > budget_s:
            break
        out[l["id"]] = classify_fn(l.get("url", ""))
        if pause_s:
            time.sleep(pause_s)
    return out


def update_state(prev: dict, listings: list, results: dict, today: str):
    """Pure. Returns (new_state, changes) where changes = {'gone': [...], 'revived': [...], 'suspicious': [portal...]}.

    Listings flagged skipLinkCheck are always 'ok'. Ids no longer in `listings` are dropped."""
    ids = [l["id"] for l in listings]
    portal = {l["id"]: portal_of(l.get("url", "")) for l in listings}
    locked = {l["id"] for l in listings if l.get("skipLinkCheck")}
    prev = {i: dict(prev[i]) for i in ids if i in prev and isinstance(prev[i], dict)}

    # portals that have ever served us a real page (earlier runs or this one)
    trusted = {portal[i] for i, r in prev.items() if r.get("a") and portal.get(i)}
    trusted |= {portal[i] for i, (res, _) in results.items() if res == "alive" and portal.get(i)}

    # suspicious: this run says "dead" for at least half of a portal's checked listings
    suspicious = []
    for p in sorted({portal[i] for i in results if portal.get(i)}):
        mine = [i for i in results if portal.get(i) == p]
        dead = [i for i in mine if results[i][0] == "dead"]
        if len(mine) >= SUSPICIOUS_MIN and len(dead) / len(mine) >= SUSPICIOUS_RATIO:
            suspicious.append(p)

    new, changes = {}, {"gone": [], "revived": [], "suspicious": suspicious}
    for i in ids:
        rec = prev.get(i, {"s": "ok", "f": 0, "u": 0})
        was_gone = rec.get("s") == "gone"
        if i in locked:
            rec.update(s="ok", f=0, u=0, c=today, why="skipLinkCheck")
        elif i in results:
            res, why = results[i]
            if res == "dead" and (portal.get(i) in suspicious or portal.get(i) not in trusted):
                res, why = "unknown", "dead ignored: " + ("suspicious run" if portal.get(i) in suspicious else "portal never served a real page")
            if res == "alive":
                rec.update(s="ok", f=0, u=0, a=today)
            elif res == "dead":
                rec["f"] = int(rec.get("f", 0)) + 1
                rec["u"] = 0
                if rec["f"] >= DEAD_AFTER:
                    rec["s"] = "gone"
            else:
                rec["u"] = int(rec.get("u", 0)) + 1
            rec.update(c=today, why=why)
        rec.setdefault("s", "ok"); rec.setdefault("f", 0); rec.setdefault("u", 0)
        if rec["s"] == "gone" and not was_gone:
            changes["gone"].append(i)
        if was_gone and rec["s"] == "ok":
            changes["revived"].append(i)
        new[i] = rec
    return new, changes


STATE_RE = re.compile(r"const linkStatus\s*=\s*\{[^\n]*\};")


def read_state(html: str) -> dict:
    import json
    m = STATE_RE.search(html)
    if not m:
        raise ValueError("Could not find 'const linkStatus' in index.html")
    body = m.group(0)
    return json.loads(body[body.index("{"):body.rindex("}") + 1])


def write_state(html: str, state: dict) -> str:
    import json
    line = "const linkStatus = " + json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + ";"
    new, n = STATE_RE.subn(lambda _m: line, html, count=1)
    if n == 0:
        raise ValueError("Could not find 'const linkStatus' in index.html")
    return new


def markdown_report(listings: list, results: dict, state: dict) -> str:
    clean = lambda s: " ".join(str(s).replace("|", "/").split())[:60]
    rows = ["| id | portale | risultato | note | giorni consecutivi | stato |", "|---|---|---|---|---|---|"]
    for l in listings[:100]:
        i = l["id"]
        res, why = results.get(i, ("-", "not checked"))
        rec = state.get(i, {})
        rows.append(f"| {clean(i)} | {portal_of(l.get('url', '')) or '?'} | {res} | {clean(why)} | {rec.get('f', 0)} | {rec.get('s', 'ok')} |")
    return "\n".join(rows)
