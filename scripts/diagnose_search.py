#!/usr/bin/env python3
"""ONE-OFF diagnosis (experiment branch only): why does the daily search return 0 candidates?

Runs the production search prompt with an appended override that asks for a plain-text report instead of JSON,
and records (a) every web-search query, (b) every result (title, url, age), split into known / unknown listing URLs,
(c) the model's verdict for each unknown ad URL. Output: scripts/diagnostics/search-diagnosis.json
Costs one Opus + web-search call.
"""
import datetime, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import anthropic
import update_listings as ul

OVERRIDE = """

## DIAGNOSTIC MODE (overrides "Return ONLY the JSON array")
Do NOT return JSON. Write a plain-text report instead, after running all the searches:
1. For EVERY distinct individual-ad URL in your search results that is NOT in EXISTING LISTINGS, one line:
   URL | address | price | outdoor space | neighbourhood | INCLUDE or EXCLUDE | the concrete reason
   (e.g. over/under budget, no outdoor space, outside the target neighbourhoods, looked sold or under offer,
   not a single ad, page unreadable).
2. Then one line per query: how many usable individual ads it surfaced.
3. Then one sentence: the main reason, if any, why the real production answer would be an empty array.
"""

def main():
    html = open(ul.INDEX_PATH, encoding="utf-8").read()
    existing = ul.extract_existing_listings(html)
    known = {ul.norm_url(l.get("url", "")) for l in existing}
    today = datetime.date.today().isoformat()
    prompt = ul.SEARCH_PROMPT.format(existing_json=json.dumps(
        [{"url": l.get("url", ""), "addr": l.get("addr", "")} for l in existing], ensure_ascii=False), today=today) + OVERRIDE
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": prompt}]
    content, stop = [], None
    for _ in range(ul.MAX_CONTINUATIONS + 1):
        r = client.beta.messages.create(betas=["web-search-2025-03-05"], model="claude-opus-4-5", max_tokens=8192,
                                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 12}],
                                        messages=messages)
        content += list(r.content); stop = r.stop_reason
        if stop != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": list(content)}]
    queries, results, texts = [], [], []
    for b in content:
        t = getattr(b, "type", "")
        if t == "server_tool_use":
            queries.append(getattr(b, "input", None))
        elif t == "web_search_tool_result":
            c = getattr(b, "content", None)
            if isinstance(c, list):
                for x in c:
                    results.append({"title": getattr(x, "title", None), "url": getattr(x, "url", None),
                                    "age": getattr(x, "page_age", None)})
            else:
                results.append({"error": str(getattr(c, "error_code", c))})
        elif t == "text" and getattr(b, "text", None):
            texts.append(b.text)
    for x in results:
        u = x.get("url")
        if u:
            x["ad_shaped"] = ul.ad_url_ok(u)
            x["known"] = ul.norm_url(u) in known
    ads = [x for x in results if x.get("ad_shaped")]
    out = {"date": today, "stop_reason": stop, "queries": queries, "n_results": len(results),
           "n_ad_shaped": len(ads), "n_ad_known": sum(1 for x in ads if x["known"]),
           "n_ad_unknown": sum(1 for x in ads if not x["known"]),
           "results": results, "report": "\n".join(texts)}
    os.makedirs(os.path.join(HERE, "diagnostics"), exist_ok=True)
    with open(os.path.join(HERE, "diagnostics", "search-diagnosis.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"stop={stop} queries={len(queries)} results={len(results)} ad_shaped={len(ads)} "
          f"known={out['n_ad_known']} unknown={out['n_ad_unknown']}")

if __name__ == "__main__":
    main()
