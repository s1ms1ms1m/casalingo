#!/usr/bin/env node
/*
 * Netlify "ignore" command. Exit 0 = SKIP the build, anything else = BUILD.
 * (Netlify docs: "An exit code of 1 indicates the contents have changed and the build process continues as
 * usual. An exit code of 0 indicates that there are no changes and the build should stop.")
 *
 * Compares the last deployed commit ($CACHED_COMMIT_REF) with the one being built ($COMMIT_REF) and skips
 * only when the published site could not look different:
 *   - changes only under scripts/ or .github/ (never published), or .gitignore, and/or
 *   - index.html differs only by (a) the `const lastChecked = '...';` date and (b) the daily bookkeeping in
 *     `const linkStatus = {...};` (failure counters), as long as the SET of listings marked "gone" is the same.
 *     The page reads only linkStatus[id].s === 'gone'; if the page ever reads more of it, extend normalize().
 * Fail-safe: a missing/odd env var, a git error (e.g. shallow clone without the cached commit), unparsable
 * state, or any other file changing -> exit 1 (build). This script can only ever err towards building.
 *
 * Trade-off: the on-page "Ultimo controllo / Ultimo aggiornamento" date only moves when a real deploy happens.
 */
const { execFileSync } = require('child_process');

const SKIP_PREFIXES = ['scripts/', '.github/'];
const SKIP_FILES = ['.gitignore'];
const REF = /^[0-9a-f]{7,64}$/i;

const git = (...args) => execFileSync('git', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });

function normalize(html) {
  html = html.replace(/\r\n/g, '\n');
  html = html.replace(/^const lastChecked\s*=\s*'[^'\n]*';/gm, "const lastChecked = '<date>';");
  html = html.replace(/^(const linkStatus\s*=\s*)(\{.*\})(;)/gm, (whole, pre, json, post) => {
    let state;
    try { state = JSON.parse(json); } catch (e) { return whole; }          // unparsable -> left as is -> compared as text
    if (!state || typeof state !== 'object' || Array.isArray(state)) return whole;
    const gone = Object.keys(state).filter(id => state[id] && state[id].s === 'gone').sort();
    return pre + 'GONE:' + JSON.stringify(gone) + post;
  });
  return html;
}

function decide(env) {
  const base = env.CACHED_COMMIT_REF, head = env.COMMIT_REF;
  if (!REF.test(base || '') || !REF.test(head || '')) return [1, 'missing/odd commit refs -> build'];
  const files = git('diff', '--name-only', '-z', base, head).split('\0').filter(Boolean);
  if (files.length === 0) return [0, 'no file changes -> skip'];
  for (const f of files) {
    if (SKIP_FILES.includes(f) || SKIP_PREFIXES.some(p => f.startsWith(p))) continue;
    if (f === 'index.html') {
      if (normalize(git('show', `${base}:index.html`)) === normalize(git('show', `${head}:index.html`))) continue;
      return [1, 'index.html changed beyond the date / link bookkeeping -> build'];
    }
    return [1, `published or config file changed (${f}) -> build`];
  }
  return [0, `only non-published files and/or date-only changes (${files.join(', ')}) -> skip`];
}

if (require.main === module) {
  let code = 1, why;
  try { [code, why] = decide(process.env); } catch (e) { code = 1; why = 'error, building to be safe: ' + String(e.message).split('\n')[0]; }
  console.log('[netlify-ignore] ' + why);
  process.exit(code);
}
module.exports = { decide, normalize };
