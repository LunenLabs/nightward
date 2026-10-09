# nightward v0.2 — `nightward view`: static blast-radius dashboard

**Date:** 2026-06-05
**Status:** 5-persona panel review complete (4 GO-WITH-FIXES, 1 NO-GO limited to legal/deployment). Implementation approved.
**Approval gate:** autonomous delegation by the user → panel of 5 virtual personas (P1 solo developer / P2 CI / P9 newcomer / P6 Korean-language / security & risk). See the session log for the detailed review.

## 1. Goal

**See** the regression boundary (blast radius) nightward produces **in a browser.** Pull the README's v1 OUT item "web UI" forward into v0.2 and implement it as a static, self-contained, read-only dashboard. State changes (approve/reject) stay in the CLI — "a system you can *see* through a UI".

## 2. Architecture (GitHub Pages = static hosting is mandatory)

- New CLI: `nightward view` (in the sense of a `build-site` alias). Reads `.nightward/` and generates a **backend-free static site** in an output directory.
- **Security decision (security persona P0):** do **not** inline data into the HTML. The generator copies a static `index.html` + `app.js` + `style.css` and emits the data as a separate `data.json`. The page does `fetch('./data.json')` and **renders only via `textContent`/DOM APIs** (`innerHTML` forbidden). → With zero server-side template injection, the stored-XSS surface disappears. The escaping burden moves to JS `textContent` (the browser guarantees safety).
- `fetch` is blocked by CORS on `file://`, so **local viewing is provided via `--serve` (local HTTP + open browser)** — which lines up exactly with P1's "local immediacy" requirement. On GitHub Pages (https), fetch works normally.

### 2.1 Output layout
```
<out>/
  index.html     # no data. <meta charset> + CSP. links app.js/style.css
  app.js         # fetch('./data.json') → render via textContent
  style.css
  data.json      # { report, meta }  ← the only data file
```

### 2.2 `data.json` schema
```json
{
  "report": { "boundary": "...", "unapproved": N, "counts": {...}, "blast_radius": {...} } | null,
  "meta": {
    "skipped": N, "failed": N,        // run_meta.json
    "baseline_count": N,              // size of store.load_baseline() (to tell no-baseline apart)
    "pending_count": N,
    "source": ".nightward",
    "generated": "ISO8601"            // display only. Tests check the key exists, not its value
  }
}
```

## 3. Screen spec (incorporating persona MUST-FIXes)

### State branches (P9 M1 — first-class citizens)
- **no-report** (`report == null`): neutral guidance + copyable `nightward run example`. No red/broken cards.
- **no-baseline** (`baseline_count == 0`): "No approved baseline yet — use `nightward approve --all` to make current behavior the baseline." Neutral color.
- **intact** (0 changes): green banner + "Identical to the last approved baseline."
- **breached**: red banner + cards.

### Header (P1 — reproducibility)
- Show the `generated` time + `source` path + boundary state at a glance.

### Banner
- intact = green / breached = red + unapproved count. **Plain one-line explanation** (P9 M2): intact = "No behavior changed since the approved baseline", breached = "N unapproved changes — review needed".
- **skipped/failed warning** (P1 · P9): count + an explanation that "skipped behaviors can be mistaken for REMOVED".
- **Data exposure warning banner** (security P1): "This page may contain captured system output. Review before publishing."

### counts strip
- unchanged/changed/new/removed + plain-language subtitles (as before / changed / new / gone).

### blast radius (P1 M2/M4, P9 M3)
- One section per group, **collapsible**. **Filters**: kind · group, "unapproved only" toggle.
- Card: kind badge + name + diff. **REMOVED visually separated/emphasized** (P1). kind legend + tooltips (NEW/CHANGED/REMOVED; REMOVED explicitly notes it may be a skip — P9).
- diff: colored +/− (escaped). Header labels in plain language: approved → "baseline (before)", received → "this run (after)" (P9 M4).
- **Both decisions + bulk** (P9 M3, P1 M2): each card has copyable `nightward approve <name>` **and** `nightward reject <name>`. The group header offers "approve this whole group"; the banner offers a bulk "approve all" command (+ a warning that "approve all buries regressions too").

## 4. Security/encoding enforcement (P6 · security — pinned by tests)
- Every file write uses `encoding="utf-8"`. `<meta charset="utf-8">` in `index.html`.
- CSP meta: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'` — no inline script (which is why app.js is a separate file).
- JS rendering uses only `textContent`/`createTextNode`. `innerHTML`/`insertAdjacentHTML` forbidden.
- `data.json` uses `ensure_ascii=False` (preserves Hangul) + is served as `application/json` (never goes through the HTML parser).
- stdout output uses the existing utf-8-reconfigured console.

## 5. Deployment (P2 — gate and view kept separate)
- `pr.yml` (on PR): **gate job** (run + gate, required, zero dependencies) + **test job** (pytest, required) + **preview job** (view → `upload-artifact`, non-blocking).
- `pages.yml` (on push to main / manual): builds **only the clean-room `example`** → publishes to Pages. `concurrency: pages`, least privilege, `environment: github-pages`.
- **Constraint: never publish real data → publish the synthetic demo only** (P2 · security M3). Only clean-room synthetic data (`scripts/build_demo.py`) is pushed to the demo site.

## 6. Legal / irreversible decisions (security persona → must be reported to the user)
A `.nightward/` store containing real data is never published. The publishing path is limited to **clean-room synthetic data** (`scripts/build_demo.py`) — the actual click to make anything public happens only after explicit maintainer approval.

## 7. Tests (TDD, persona guards)
`tests/test_view.py`:
- `build_site` emits index.html/app.js/style.css/data.json as UTF-8.
- index.html bytes contain `<meta charset="utf-8">` + CSP.
- app.js does not contain `innerHTML` (static guard).
- data.json has the report + meta structure, and Hangul behavior names are preserved as UTF-8 bytes.
- Generates without crashing in all 4 states: no-report / no-baseline / intact / breached.
- Generates without crashing for a report mixing Hangul with characters cp949 cannot encode (emoji).
- CLI `nightward view` integration (subprocess), cp949 stdout without incident.

## 8. v0.2 OUT (later)
LLM-as-judge semantic diff, PR comment bot summary, delta view (vs. the previous baseline), dark mode, search.
