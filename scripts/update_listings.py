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
import anthropic
import json5  # handles JS object syntax: unquoted keys, single-quoted strings

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
- img: use real image URL if available, otherwise empty string ""
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


def call_claude(existing: list[dict]) -> list[dict]:
    """Ask Claude (via API) to find new listings."""
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
    try:
        response = client.beta.messages.create(
            betas=["web-search-2025-03-05"],
            model="claude-opus-4-5",
            max_tokens=4096,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 12
            }],
            messages=[{"role": "user", "content": prompt}]
        )
    except Exception as e:
        print(f"ERROR calling Anthropic API: {type(e).__name__}: {e}", file=sys.stderr)
        # Write a summary of the error to GitHub step summary
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(f"### ❌ API Error\n```\n{type(e).__name__}: {e}\n```\n")
        raise

    print(f"API response received. Stop reason: {response.stop_reason}")

    # Extract the text block from the response
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    text = text.strip()
    if not text or text == "[]":
        return []

    # Strip markdown fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    try:
        result = json.loads(text)
        if not isinstance(result, list):
            print(f"WARNING: Claude returned non-list: {type(result)}", file=sys.stderr)
            return []
        return result
    except json.JSONDecodeError as e:
        print(f"WARNING: Could not parse Claude response as JSON: {e}", file=sys.stderr)
        print(f"Response was: {text[:500]}", file=sys.stderr)
        return []


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
    candidates = call_claude(existing)
    print(f"Claude returned {len(candidates)} candidate listing(s).")

    # Deduplicate
    new_listings = deduplicate(candidates, existing)
    print(f"After deduplication: {len(new_listings)} new listing(s).")

    # Always stamp the check date, so the site shows the script ran
    today = datetime.date.today().isoformat()
    updated_html = update_last_checked(html, today)

    if not new_listings:
        print("Nothing new — only the last-checked date is updated.")
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            f.write(updated_html)
        with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
            f.write("new_count=0\n")
            f.write("summary=no new listings\n")
        return

    # Update index.html
    updated_html = inject_new_listings(updated_html, new_listings)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(updated_html)

    print(f"Updated index.html with {len(new_listings)} new listing(s):")
    for l in new_listings:
        print(f"  • {l.get('hood')} — {l.get('addr')} — €{l.get('price'):,} ({l.get('type')})")

    # Signal count to the workflow
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
        f.write(f"new_count={len(new_listings)}\n")
        summary = "; ".join(
            f"{l.get('hood')} {l.get('type')} €{l.get('price'):,}"
            for l in new_listings
        )
        f.write(f"summary={summary}\n")


if __name__ == "__main__":
    main()
