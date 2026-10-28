#!/usr/bin/env python3
"""Tests for scripts/immobiliare.py. Run: python3 scripts/test_immobiliare.py   (no network, no API key)

The fixtures copy the STRUCTURE captured from real pages on 27 Oct 2026 (card = price, link with ad ID and title,
"messaggio Visita"; ad page = canonical line, "Contratto Vendita Piano ... Superficie ... Locali ... Balcone Sì Terrazzo No",
"Informazioni sul prezzo Prezzo € ...", "Annuncio aggiornato il ...") with invented addresses, prices and IDs.
"""
import datetime, json, os, re, sys, tempfile
from types import SimpleNamespace as NS
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.dont_write_bytecode = True
import immobiliare as im

NOW = datetime.datetime(2026, 10, 28, 11, 15, tzinfo=datetime.timezone.utc)
TODAY = "2026-10-28"
failures = 0
def check(name, cond, detail=""):
    global failures
    print(("ok    " if cond else "FAIL  ") + name + ("" if cond else f"   <- {detail}"))
    if not cond: failures += 1
def iso(**delta): return (NOW - datetime.timedelta(**delta)).isoformat()

# ── fixtures ────────────────────────────────────────────────────────────────
def card(aid, price, title, label="Salone"):
    return (f"- ![{label}](https://pwm.im-cdn.it/image/1{aid}/xxs-c.jpg)\n\n  1/31\n\n  nuovovetrina\n\n  ![Agenzia Test](https://pic.im-cdn.it/imagenoresize/9.jpg)\n\n"
            f"  {price}\n\n  [{title}](https://www.immobiliare.it/annunci/{aid}/ \"{title}\")\n\n  messaggio Visita\n")

def search_page(*cards): return "Immobiliare.it Torino Vanchiglia vendita case\n\n" + "".join(cards) + "\nPagina successiva"

def ad_page(aid, price="195.000", contract="Vendita", floor="Piano 9 Piani edificio 9 Ascensore Sì", m2="75 m²", locali="3",
            balcone="Sì", terrazzo="No", extra="", canonical=None, desc="Trilocale luminoso a due passi dal Po, a 5 minuti a piedi dal Lungo Po."):
    head = f"--- canonical: https://www.immobiliare.it/annunci/{canonical or aid}/ meta-apple-mobile-web-app-title: Immobiliare.it meta-og:image: https://pwm.im-cdn.it/image/55{aid}/m-c.jpg meta-og:locale:alternate: it_IT ---"
    feat = (f"Tipologia Appartamento | Intera proprietà Contratto {contract} {floor} Superficie {m2} Locali {locali} Camere da letto 2 Cucina Cucina angolo cottura "
            f"Bagni 1 Arredato Sì Balcone {balcone} Terrazzo {terrazzo} Riscaldamento Centralizzato {extra}")
    return (f"{head} ![Balcone](https://pwm.im-cdn.it/image/1/m-c.jpg) ![Vista](https://pwm.im-cdn.it/image/2/m-c.jpg) Torino Vanchiglia Corso Belgio, 24 € {price} 3 75 m² "
            f"Annuncio aggiornato il 26/10/2026 Descrizione riferimento: X {desc} leggi tutto Caratteristiche {feat} Dettaglio superficie Abitazione Piano 9 Superficie 75,0 m² "
            f"Informazioni sul prezzo Prezzo € {price} Prezzo al m² € 2.600")

GONE = "La pagina che stai cercando non è presente. L'annuncio non è più disponibile. Cerca altre case a Torino."
MODEL_JSON = json.dumps({"desc": "Trilocale luminoso al nono piano.", "outdoorNote": "Balcone sul fronte strada.", "naturaNote": "Il Lungo Po è a 5 minuti a piedi."})

class Fake:
    """Fake Anthropic client. pages: {url-or-ad-id: (text, retrieved_at)} | {key: "error"} | {key: "raise"}."""
    def __init__(self, pages, model_text=MODEL_JSON):
        self.pages, self.model_text, self.calls = pages, model_text, []
        self.beta = NS(messages=NS(create=self._create))
    def _create(self, **kw):
        prompt = kw["messages"][0]["content"]; self.calls.append(kw)
        url = re.search(r"https://\S+", prompt).group(0)
        m = re.search(r"/annunci/(\d+)/", url)
        key = m.group(1) if m else url.rstrip(",")
        hit = self.pages.get(key)
        if hit is None and not m:                                    # search page: match on the zone slug (+ page)
            for k, v in self.pages.items():
                if not k.isdigit() and k in url and (("pag=2" in url) == ("pag=2" in k)): hit = v; break
        if hit == "raise": raise RuntimeError("boom")
        blocks = [NS(type="server_tool_use", name="web_fetch", input={"url": url})]
        if hit is None or hit == "error":
            blocks.append(NS(type="web_fetch_tool_result", content=NS(type="web_fetch_tool_result_error", error_code="url_not_accessible")))
        else:
            text, when = hit
            blocks.append(NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url, retrieved_at=when,
                             content=NS(type="document", source=NS(type="text", media_type="text/plain", data=text)))))
        blocks.append(NS(type="text", text=self.model_text if m else "done"))
        return NS(content=blocks, stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=50))

# ── 1. parse_cards ──────────────────────────────────────────────────────────
PAGE = search_page(
    card("133901445", "€ 195.000", "Trilocale corso Belgio 24, Vanchiglia, Torino"),
    card("133803572", "€ 215.000", "Trilocale via santa Giulia 37, Vanchiglia, Torino"),
    card("133705538", "€ 349.000", "Quadrilocale corso San Maurizio 46, Giardini Reali, Torino"),
    card("133948517", "€ 495.000", "Appartamento via Saluzzo, 44 Torino, San Salvario - Baretti, Torino"),
    card("133940465", "da € 245.850,00", "Appartamento all'asta via Alessandro Volta 8, Torino"),
    card("133545874", "€ 620.000", "Quadrilocale ottimo stato, primo piano, Vanchiglia, Torino"),
    card("133111111", "€ 330.000", "Trilocale via Madonna del Pilone 12, Madonna del Pilone, Torino"),
    card("133222222", "€ 280.000", "Appartamento piazza Santa Giulia, Vanchiglia, Torino"),
)
cs = im.parse_cards(PAGE)
by = {c["id"]: c for c in cs}
check("8 cards, page order kept", [c["id"] for c in cs] == ["133901445", "133803572", "133705538", "133948517", "133940465", "133545874", "133111111", "133222222"], [c["id"] for c in cs])
c = by["133901445"]
check("card: price, hood, type, address", (c["price"], c["hood"], c["type"], c["addr"]) == (195000, "Vanchiglia", "Trilocale", "corso Belgio 24"), c)
check("card: price is the one printed right before the link (not the previous card's)", by["133803572"]["price"] == 215000 and by["133803572"]["prices"] == [215000])
check("card: zone not in the target set -> no hood", by["133705538"]["hood"] is None)
c = by["133948517"]
check("card: 'via Saluzzo, 44 Torino, San Salvario - Baretti' -> San Salvario, 'via Saluzzo 44'", (c["hood"], c["addr"], c["type"]) == ("San Salvario", "via Saluzzo 44", "Appartamento"), c)
check("card: auction is flagged and has no hood", by["133940465"]["auction"] and by["133940465"]["hood"] is None and by["133940465"]["price"] == 245850)
check("card: street-less title -> empty address (never invented)", by["133545874"]["addr"] == "" and by["133545874"]["hood"] == "Vanchiglia")
check("card: Madonna del Pilone maps to Gran Madre", by["133111111"]["hood"] == "Gran Madre")
check("card: 'piazza ...' counts as a street", by["133222222"]["addr"] == "piazza Santa Giulia")
check("duplicate link on the page is returned once", len(im.parse_cards(search_page(card("133901445", "€ 195.000", "Trilocale corso Belgio 24, Vanchiglia, Torino")) * 2)) == 1)
check("no cards in junk text", im.parse_cards("Access denied. Verifica di sicurezza") == [] and im.parse_cards(None) == [])
check("link without a numeric ID is not a card", im.parse_cards('[x](https://www.immobiliare.it/vendita-case/torino/ "x")') == [])
nop = im.parse_cards(search_page(card("133333333", "Prezzo su richiesta", "Trilocale via Po 1, Vanchiglia, Torino")))[0]
check("card without a price: price None (not the previous card's)", nop["price"] is None)

# ── 2. parse_ad ─────────────────────────────────────────────────────────────
a = im.parse_ad(ad_page("133901445"))
check("ad: canonical, contract, price, m2, rooms", (a["canonical_id"], a["contract"], a["price"], a["m2"], a["rooms"]) == ("133901445", "vendita", 195000, 75, 3), a)
check("ad: floor 9, elevator yes", (a["floor_raw"], a["elevator"]) == ("9", True), a)
check("ad: balcone Sì / terrazzo No", (a["balcone"], a["terrazzo"], a["giardino"]) == (True, False, False), a)
check("ad: photo alt text 'Balcone' is not counted when the feature says No", im.parse_ad(ad_page("133901445", balcone="No"))["balcone"] is False)
check("ad: image and update date", a["image"].endswith("/m-c.jpg") and a["updated"] == "26/10/2026", a)
check("ad: description read between 'Descrizione' and 'leggi tutto'", "Lungo Po" in a["description"], a["description"])
check("ad: garden recognised", im.parse_ad(ad_page("133901445", extra="Altre caratteristiche - Giardino privato - Cantina"))["giardino"] is True)
check("ad: 'Giardino' only in a photo caption is not a garden", im.parse_ad(ad_page("133901445") + " ![Giardino](https://x/y.jpg)")["giardino"] is False)
check("ad: image markdown inside the description does not leak into it", "pwm.im-cdn" not in im.parse_ad(ad_page("133901445", desc="Bel trilocale ![Salone](https://pwm.im-cdn.it/image/9/m-c.jpg) vicino al Po."))["description"])
check("ad: terrace Sì", im.parse_ad(ad_page("133901445", terrazzo="Sì"))["terrazzo"] is True)
check("ad: ground floor and no lift", (lambda r: (r["floor_raw"], r["elevator"]))(im.parse_ad(ad_page("133901445", floor="Piano T Piani edificio 4 Ascensore No"))) == ("T", False))
check("ad: missing facts are None, not guessed", all(v in (None, False, "") for k, v in im.parse_ad("nothing useful here").items()), im.parse_ad("nothing useful here"))
check("ad: gone page recognised", im.parse_ad(GONE)["gone"] is True and im.parse_ad(GONE)["canonical_id"] is None)
check("ad: m2 with decimals rounds", im.parse_ad(ad_page("133901445", m2="82,5 m²"))["m2"] == 82 or im.parse_ad(ad_page("133901445", m2="82,5 m²"))["m2"] == 83)

# ── 3. small helpers ────────────────────────────────────────────────────────
check("floor_text", [im.floor_text("9", True), im.floor_text("2", False), im.floor_text("T", True), im.floor_text("R", None), im.floor_text("3", None), im.floor_text("??", True), im.floor_text(None, None)]
      == ["9° piano · ascensore", "2° piano · senza ascensore", "Piano terra · ascensore", "Piano rialzato", "3° piano", "Da verificare", "Da verificare"])
check("type_of", [im.type_of("Appartamento", 3), im.type_of("Appartamento", 6), im.type_of("Trilocale", 4), im.type_of("", 2), im.type_of("Attico", 4), im.type_of("Appartamento", None)]
      == ["Trilocale", "6 locali", "Trilocale", "Bilocale", "Attico", "Appartamento"])
pins = {im.pin("Vanchiglia", n) for n in range(133000000, 133000200)}
check("pin: stays within ~150 m of the neighbourhood centre, spreads out", all(abs(p[0] - 45.0706) < 0.0013 and abs(p[1] - 7.6944) < 0.002 for p in pins) and len(pins) > 40, len(pins))
check("pin: deterministic", im.pin("Cavoretto", "133901445") == im.pin("Cavoretto", "133901445"))
check("next_id continues each prefix, c is shared by Gran Madre and Cavoretto", (im.next_id("Vanchiglia", {"v7", "v10", "vt2", "s3"}), im.next_id("Vanchiglietta", {"v7", "vt2"}), im.next_id("Gran Madre", {"c24", "c5"}), im.next_id("San Salvario", set())) == ("v11", "vt3", "c25", "s1"))
check("clean_text drops urls, e-mails, phone numbers, long digit runs", [im.clean_text(x, 99) for x in ("vedi https://x.it", "scrivi a a@b.it", "chiama 011 123 4567", "tel +39 333 1234567")] == ["", "", "", ""])
check("clean_text keeps normal sentences and caps the length", im.clean_text("Bilocale al 3° piano, 62 m².", 99) == "Bilocale al 3° piano, 62 m²." and len(im.clean_text("a" * 500, 50)) == 50)
check("parse_texts takes the last JSON object with 'desc'", im.parse_texts(['ecco: {"x":1}', '{"desc":"ok","outdoorNote":"","naturaNote":""}'])["desc"] == "ok" and im.parse_texts(["niente"]) == {})

# ── 4. examine (one ad page, every rejection path) ──────────────────────────
C = {"id": "133901445", "url": "https://www.immobiliare.it/annunci/133901445/", "hood": "Vanchiglia", "zone": "Vanchiglia", "type": "Trilocale",
     "addr": "corso Belgio 24", "price": 195000, "prices": [195000], "auction": False}
def ex(text, when=None, model=MODEL_JSON, card=C, aid="133901445"):
    pages = {aid: "error" if text == "error" else (text, when or iso(hours=1))}
    return im.examine(Fake(pages, model), card, TODAY, NOW)
l, st, note, tok = ex(ad_page("133901445"))
check("examine: good ad is added", st == "added" and l is not None and tok == 1050, (st, note))
check("examine: facts come from the page", (l["price"], l["m2"], l["floor"], l["type"], l["outdoor"], l["outdoorType"], l["addr"], l["hood"], l["src"], l["top"], l["published"], l["url"])
      == (195000, 75, "9° piano · ascensore", "Trilocale", "Balcone", "balcone", "corso Belgio 24", "Vanchiglia", "immobiliare.it", False, TODAY, C["url"]), l)
check("examine: model texts used when clean", (l["desc"], l["outdoorNote"], l["naturaNote"]) == ("Trilocale luminoso al nono piano.", "Balcone sul fronte strada.", "Il Lungo Po è a 5 minuti a piedi."), l)
check("examine: listing has exactly the site's fields", set(l) == {"id", "hood", "addr", "floor", "lat", "lng", "price", "type", "m2", "outdoor", "outdoorType", "outdoorNote", "naturaNote", "desc", "url", "img", "src", "top", "published"}, set(l))
check("examine: image is the og:image of the page", l["img"] == "https://pwm.im-cdn.it/image/55133901445/m-c.jpg", l["img"])
l2, st2, *_ = ex(ad_page("133901445"), model="boom, no json")
check("examine: model failure -> plain fallback sentence, facts unchanged, still added", st2 == "added" and l2["desc"] == "Trilocale di 75 m² a Vanchiglia." and l2["outdoorNote"] == "" and l2["price"] == 195000, l2)
l3, *_ = ex(ad_page("133901445"), model=json.dumps({"desc": "Chiama 011 123 4567", "outdoorNote": "www.x.it", "naturaNote": "x@y.it"}))
check("examine: phone / url / e-mail in model text never reach the listing", (l3["desc"], l3["outdoorNote"], l3["naturaNote"]) == ("Trilocale di 75 m² a Vanchiglia.", "", ""), l3)
l4, *_ = ex(ad_page("133901445"), card=dict(C, addr="", zone="Vanchiglia"))
check("examine: street-less card -> honest placeholder address", l4["addr"] == "Indirizzo non indicato (Vanchiglia)", l4["addr"])
l5, *_ = ex(ad_page("133901445", terrazzo="Sì"))
check("examine: terrace beats balcony as type", (l5["outdoor"], l5["outdoorType"]) == ("Terrazzo e balcone", "terrazzo"), l5)

def rejected(text, expect, when=None, **kw):
    l, st, note, _ = ex(text, when, **kw)
    check(f"examine rejects: {expect} ({note[:60]})", l is None and st == expect, (st, note))
rejected("error", "unreadable")
rejected(GONE, "gone")
rejected(ad_page("133901445", canonical="199999999"), "mismatch")                                  # a different ad behind the link (the c24 failure)
rejected(ad_page("133901445", contract="Affitto"), "not_for_sale")
rejected(ad_page("133901445"), "stale", when=iso(days=9))
rejected(ad_page("133901445"), "stale", when="garbage")
rejected(ad_page("133901445", price="230.000"), "mismatch")                                          # page price not on the card
rejected(ad_page("133901445", locali="1"), "few_rooms")
rejected(ad_page("133901445", balcone="No", terrazzo="No"), "no_outdoor")
rejected("Pagina senza dati dell'annuncio. canonical: https://www.immobiliare.it/annunci/133901445/", "mismatch")
l6, st6, *_ = ex(ad_page("133901445"), when=iso(days=5))
check("examine: a 5-day-old copy is accepted (limit is 7)", st6 == "added")
l7, st7, *_ = ex(ad_page("133901445", price="195.000"), card=dict(C, prices=[210000, 195000]))
check("examine: card with two prices (old + new) accepts the page price among them", st7 == "added")
try:
    im.examine(Fake({"133901445": "raise"}), C, TODAY, NOW); check("examine: API exception propagates", False)
except RuntimeError:
    check("examine: API exception propagates (discover decides what it means)", True)

# ── 5. discover ─────────────────────────────────────────────────────────────
P1 = [("vanchiglia", "vanchiglia", 1), ("san-salvario", "san-salvario", 1)]
def pages_for(*ids, extra=None):
    d = {"vanchiglia": (search_page(card("133901445", "€ 195.000", "Trilocale corso Belgio 24, Vanchiglia, Torino"),
                                    card("133803572", "€ 215.000", "Trilocale via santa Giulia 37, Vanchiglia, Torino"),
                                    card("133705538", "€ 349.000", "Quadrilocale corso San Maurizio 46, Giardini Reali, Torino"),
                                    card("133940465", "da € 245.850,00", "Appartamento all'asta via Volta 8, Torino"),
                                    card("133666666", "€ 750.000", "Attico via Po 1, Vanchiglia, Torino"),
                                    card("133555555", "€ 150.000", "Bilocale via Po 2, Vanchiglia, Torino")), iso(hours=2)),
         "san-salvario": (search_page(card("133948517", "€ 495.000", "Appartamento via Saluzzo, 44 Torino, San Salvario - Baretti, Torino")), iso(hours=2))}
    for i in ids: d[i] = (ad_page(i, price={"133901445": "195.000", "133803572": "215.000", "133948517": "495.000"}[i]), iso(hours=3))
    d.update(extra or {})
    return d
EXISTING = [{"id": "v7", "url": "https://www.immobiliare.it/annunci/131111111/"}, {"id": "s3", "url": "https://www.wikicasa.it/annuncio/1"}, {"id": "c24", "url": "x"}]
def disc(pages, existing=EXISTING, removed=(), seen=None, **kw):
    fake = Fake(pages)
    return im.discover(fake, existing, removed, seen or {}, today=TODAY, now=NOW, pages=P1, **kw), fake

r, fake = disc(pages_for("133901445", "133803572", "133948517"))
ids = {l["url"].split("/")[-2]: l["id"] for l in r["listings"]}
check("discover: 3 ads added; auction, out-of-zone, over/under-budget never examined", len(r["listings"]) == 3 and sum(1 for c in fake.calls if "annunci" in c["messages"][0]["content"]) == 3, (len(r["listings"]), len(fake.calls)))
check("discover: ids continue the site's numbering per prefix (v7 exists -> v8, v9; s3 exists -> s4)", ids == {"133948517": "s4", "133901445": "v8", "133803572": "v9"}, ids)
check("discover: report says what was read", "2/2 search pages" in r["report"] and "3 added" in r["report"] and "auction 1" in r["report"] and "other_zone 1" in r["report"] and "out_of_budget 2" in r["report"], r["report"])
check("discover: every examined ad is written to the seen rows as 'added'", sorted(x[0] for x in r["seen_rows"]) == ["133803572", "133901445", "133948517"] and all(x[2] == "added" and x[1] == TODAY for x in r["seen_rows"]), r["seen_rows"])
check("discover: tokens counted (2 search pages + 3 ads)", r["tokens"] == 5 * 1050, r["tokens"])
check("discover: newest ad (largest ID) is examined first", [re.search(r"/annunci/(\d+)/", c["messages"][0]["content"]).group(1) for c in fake.calls if "annunci" in c["messages"][0]["content"]] == ["133948517", "133901445", "133803572"])

r, fake = disc(pages_for("133901445", "133803572", "133948517"), max_examine=2)
check("discover: cap of 2 -> the 2 newest examined, 1 waits, and the report says so", len(r["listings"]) == 2 and "1 wait for the next run" in r["report"] and "133803572" not in {x[0] for x in r["seen_rows"]}, r["report"])

r, _ = disc(pages_for("133901445", "133803572", "133948517"), existing=EXISTING + [{"id": "v1", "url": "https://www.immobiliare.it/en/annunci/133901445/?x=1"}])
check("discover: an ad already on the site (matched by ID, whatever the URL form) is skipped", "133901445" not in {l["url"].split("/")[-2] for l in r["listings"]} and "on_site 1" in r["report"], r["report"])
r, _ = disc(pages_for("133901445", "133803572", "133948517"), removed={"immobiliare.it/annunci/133803572"})
check("discover: an ad on the removed list is skipped", "133803572" not in {l["url"].split("/")[-2] for l in r["listings"]} and "removed 1" in r["report"], r["report"])
r, _ = disc(pages_for("133901445", "133803572", "133948517"), seen={"133901445": ["no_outdoor"], "133803572": ["unreadable", "stale", "mismatch"], "133948517": ["unreadable"]})
check("discover: final statuses and 3 failed tries are skipped; 1 failed try is retried", [l["url"].split("/")[-2] for l in r["listings"]] == ["133948517"] and "done_before 2" in r["report"], r["report"])

# a stale search page is not used; the others are
pg = pages_for("133901445", "133803572"); pg["san-salvario"] = (pg["san-salvario"][0], iso(days=3))
r, _ = disc(pg)
check("discover: stale search page ignored and reported, the other page still used", len(r["listings"]) == 2 and any("san-salvario: stale copy" in x for x in r["details"]) and "1/2 search pages" in r["report"], (r["report"], r["details"][:3]))
pg = pages_for("133901445"); pg["san-salvario"] = "error"
r, _ = disc(pg)
check("discover: unreadable search page reported, the other used", any("san-salvario: not readable" in x for x in r["details"]) and "1/2" in r["report"])
pg = pages_for("133901445"); pg["san-salvario"] = (pg["san-salvario"][0], "garbage")
r, _ = disc(pg)
check("discover: search page without a usable retrieval date is not used (age unknown = not live)", any("san-salvario: no retrieval date" in x for x in r["details"]) and "1/2" in r["report"], r["details"][:3])
pg = pages_for("133901445"); pg["san-salvario"] = ("Verifica di sicurezza. Cloudflare.", iso(hours=1))
r, _ = disc(pg)
check("discover: a page that has no ads (block page) is reported, not used", any("no ads found" in x for x in r["details"]))
for name, pg in (("both stale", {k: (v[0], iso(days=2)) for k, v in pages_for().items()}), ("both unreadable", {"vanchiglia": "error", "san-salvario": "error"}), ("API raises", {"vanchiglia": "raise", "san-salvario": "raise"}),
                  ("no ads in either page", {"vanchiglia": (search_page(), iso(hours=1)), "san-salvario": (search_page(), iso(hours=1))})):
    try:
        disc(pg); check(f"discover raises ImmobiliareError when no page is readable ({name})", False)
    except im.ImmobiliareError as e:
        check(f"discover raises ImmobiliareError when no page is readable ({name})", "no immobiliare.it search page" in str(e), str(e))
try:
    disc(pages_for(extra={"133901445": "raise", "133803572": "raise", "133948517": "raise"}))
    check("discover raises when every ad read raises", False)
except im.ImmobiliareError as e:
    check("discover raises when every ad read raises", "all 3 ad reads raised" in str(e), str(e))
r, _ = disc(pages_for("133901445", extra={"133803572": "raise", "133948517": "raise"}))
check("discover: one failing ad does not stop the others (and is not recorded: it is tried again tomorrow)", [l["url"].split("/")[-2] for l in r["listings"]] == ["133901445"] and {x[0] for x in r["seen_rows"]} == {"133901445"}, r["seen_rows"])
r, _ = disc(pages_for("133901445", extra={"133803572": "error", "133948517": (GONE, iso(hours=1))}))
check("discover: unreadable / gone ads are recorded with their status", dict((x[0], x[2]) for x in r["seen_rows"]) == {"133901445": "added", "133803572": "unreadable", "133948517": "gone"}, r["seen_rows"])

# ── 6. seen file ────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "seen.txt")
    check("seen: missing file = empty", im.load_seen(p) == {})
    im.append_seen([("133901445", TODAY, "added", "ok"), ("133803572", TODAY, "unreadable", "page not fetchable\nwith newline")], p)
    im.append_seen([("133803572", "2026-10-29", "unreadable", "again")], p)
    im.append_seen([], p)
    s = im.load_seen(p)
    check("seen: round trip, statuses collected per ID", s == {"133901445": ["added"], "133803572": ["unreadable", "unreadable"]}, s)
    check("seen: header comment written once, notes forced to one line", open(p).read().count("# immobiliare") == 1 and len(open(p).read().splitlines()) == 4)
    check("seen: 'added' is final; 2 failed tries are retried; the 3rd failed try is final", im.is_done(s["133901445"]) and not im.is_done(s["133803572"]) and im.is_done(["unreadable", "stale", "mismatch"]))
    open(p, "a").write("garbage line\n# comment\n\n12\tx\n")
    check("seen: garbage / comments / short lines ignored", im.load_seen(p) == s)
check("the real seen file (if present) parses", isinstance(im.load_seen(), dict))
check("ad_id", (im.ad_id("https://www.immobiliare.it/annunci/133901445/"), im.ad_id("https://www.immobiliare.it/en/annunci/133901445/?a=1"), im.ad_id("https://www.idealista.it/immobile/35754443/"), im.ad_id(None)) == ("133901445", "133901445", None, None))

print("\nFAILURES:", failures)
sys.exit(1 if failures else 0)
