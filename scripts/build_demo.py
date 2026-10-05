"""Build the public GitHub Pages demo site from SYNTHETIC, clean-room data.

This is the ONLY thing the Pages workflow publishes. It must never read a real
user `.nightward/` store — everything here is invented sample data, so nothing
sensitive can leak (security-persona MUST-FIX: default publish = clean-room).

It fabricates a realistic "blast radius": an AI tweaks the tax rule, which moves
checkout_total AND a coupled loyalty-points behavior, plus a new endpoint and a
disappeared one — exactly the cascade nightward exists to surface.

    python scripts/build_demo.py [out_dir]      # default: demo-site
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, canonical_json
from nightward.view import build_site

# --- synthetic baseline (the "approved" boundary) --------------------------
BASELINE = [
    Behavior("checkout_total", {"subtotal": 25.50, "tax": 2.55, "total": 28.05}, group="billing"),
    Behavior("loyalty_points", {"earned": 28, "tier": "silver"}, group="billing"),
    Behavior("user_login", {"status": "ok", "session": "<scrubbed>", "mfa": False}, group="auth"),
    Behavior("password_policy", {"min_len": 8, "needs_symbol": True}, group="auth"),
    Behavior("order_search", {"results": 12, "sort": "relevance"}, group="search"),
]

# --- this run's observed behaviors (after an AI "fixed" the tax rule) -------
PENDING = [
    # CHANGED: tax rule moved -> total moved (the intended-ish fix)
    Behavior("checkout_total", {"subtotal": 25.50, "tax": 2.81, "total": 28.31}, group="billing"),
    # CHANGED: silently coupled — points were derived from total. Classic side effect.
    Behavior("loyalty_points", {"earned": 28, "tier": "bronze"}, group="billing"),
    # UNCHANGED
    Behavior("user_login", {"status": "ok", "session": "<scrubbed>", "mfa": False}, group="auth"),
    # CHANGED: an unrelated-looking auth tweak rode along
    Behavior("password_policy", {"min_len": 10, "needs_symbol": True}, group="auth"),
    # NEW: a brand-new behavior appeared
    Behavior("checkout_total_with_coupon", {"subtotal": 25.50, "discount": 5.0, "total": 23.31},
             group="billing"),
    # order_search (search) is GONE this run -> REMOVED (could be a real regression)
]


def main(out_dir: str = "demo-site") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tw = Path(tmp) / ".nightward"
        store = Store(tw)
        store.ensure()
        for b in BASELINE:
            store._file(store.baseline_dir, b.name, "approved").write_text(
                canonical_json(b.to_dict()), encoding="utf-8"
            )
        for b in PENDING:
            store.write_pending(b)
        store.write_run_meta({"skipped": 1, "failed": 0})

        # recompute the report the same way the CLI does
        from nightward.runner import recompute
        report = recompute(store)

        out = build_site(tw, out_dir)
        print(f"demo site -> {out}  (boundary: {report['boundary']}, "
              f"unapproved: {report['unapproved']})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-site")
