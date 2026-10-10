# Threat model: what this gate does and does not protect against

[nightward](../README.md) reference. Read this before trusting a green gate.


Be precise about the guarantee: **"boundary intact" means no *captured* behavior
changed** — nothing more. Read these four limits before trusting the green light:

1. **Coverage is your instrumentation.** The gate only sees what `behavior()`
   calls capture. An agent can break an uncaptured code path and the boundary
   stays intact. Instrument the behaviors you actually care about, and treat
   the gate as a tripwire on those — not as proof that "nothing broke."
2. **The gate is a convention, not a sandbox.** An agent with shell access can
   run `nightward approve`, copy `pending/` into `baseline/`, delete `behavior()`
   calls, or add scrub rules that mask a change. nightward does not try to
   police the filesystem — instead, every one of those bypasses leaves a
   visible trace in git. The enforcement point is **review**:
   - `baseline/*.approved.json`, the judge ledger (`judge/`), scrub rules, and test
     files are code — review their diffs in every PR;
   - protect your main branch (required human review) and let CI re-run
     `nightward run && nightward gate` from source, so a locally forged
     "intact" can't merge itself (a hand-edited `report.json` or a forged copy
     of the store leaves no trace in git; MCP refuses any store but the one the
     server was started with, and names it as `store` in every result);
   - never wire `approve` into the agent loop or CI (the MCP server
     deliberately doesn't expose it — keep your own glue to the same rule).
3. **A semantic judge can be wrong.** A false-SAME verdict is a hole in the
   gate. That's why judging is opt-in per behavior, the prompt is conservative
   (unsure → DIFFERENT), failures fall back to exact comparison, and every
   ruling is recorded in the committed judge ledger (`.nightward/judge/`) for
   human review. Persona rulings are recomputed every run, so editing their ledger
   entries changes nothing; a model's ruling is replayed from the ledger and marked
   as replayed.
   If a behavior must never be judged leniently, don't mark it `semantic=True`.
   Captured output is untrusted input to an LLM judge (it may quote retrieved
   documents or user text). The prompt inserts each output once, verbatim, in a
   block tagged with a fresh random id the text can't contain, and tells the model
   the blocks are data. That is a mitigation, not a guarantee, which is one more
   reason to review the rulings.
4. **Normalization defines "the same".** Default timestamp/UUID scrubbing, key
   order and JSON key coercion erase some differences by design — see
   [What nightward normalizes](normalization.md).
