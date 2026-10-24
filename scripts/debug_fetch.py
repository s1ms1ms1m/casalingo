#!/usr/bin/env python3
"""ONE-OFF (experiment branch only): what text does the web-fetch tool really return for a listing page,
and does it contain the sold / under-offer / unavailable wording the owner sees in the browser?
Output: scripts/diagnostics/fetch-debug.txt"""
import json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import anthropic
import availability as av

KEYS = re.compile(r"venduto|vendut|in trattativa|trattativa|sotto offerta|riservat|non disponibile|non pi[uù] disponibile|non sembra esserci|scaduto|disponibile|sold", re.I)

def main(urls):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    out = []
    for u in urls:
        resp = client.beta.messages.create(model=av.MODEL, max_tokens=1024, betas=av.BETAS, tools=[av.TOOL],
                                           messages=[{"role": "user", "content": av.PROMPT.replace("__URL__", u)}])
        f = av.fetch_outcome(resp.content)
        blocks = [av.as_dict(b) for b in resp.content]
        text = ""
        meta = {}
        for b in blocks:
            if "fetch" in str(b.get("type", "")) and b.get("type") != "server_tool_use":
                for d in av.walk(b):
                    for k in ("data", "text"):
                        if isinstance(d.get(k), str) and len(d[k]) > len(text):
                            text = d[k]
                    for k in ("retrieved_at", "title", "url"):
                        if isinstance(d.get(k), str):
                            meta[k] = d[k]
        verdict = av.parse_verdict([b.get("text", "") for b in blocks if b.get("type") == "text"]) or {}
        hits = []
        for m in KEYS.finditer(text):
            hits.append(text[max(0, m.start() - 70): m.end() + 70].replace("\n", " "))
        os.makedirs(os.path.join(HERE, "diagnostics"), exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", u.lower().split("//")[-1]).strip("-")
        open(os.path.join(HERE, "diagnostics", f"page-{slug}.txt"), "w", encoding="utf-8").write(text)
        out.append(f"=== {u}\nfetched={f['fetched']} errors={f['errors']} chars={len(text)} meta={json.dumps(meta, ensure_ascii=False)}\n"
                   f"model verdict: {json.dumps(verdict, ensure_ascii=False)}\n"
                   f"keyword hits in fetched text ({len(hits)}):\n" + "\n".join(f"  ... {h} ..." for h in hits[:12]) +
                   f"\nfirst 400 chars: {text[:400]!r}\n")
        print(out[-1][:600])
    os.makedirs(os.path.join(HERE, "diagnostics"), exist_ok=True)
    open(os.path.join(HERE, "diagnostics", "fetch-debug.txt"), "w", encoding="utf-8").write("\n".join(out))

if __name__ == "__main__":
    main(sys.argv[1:])
