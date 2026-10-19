"""`approve` and `reject`: the human decisions on the boundary."""
from __future__ import annotations

import typer
from rich.markup import escape

from ..config import review_depths
from ..core.baseline import Store, change_token
from ..core.diff import NOT_RUN, REMOVED, UNCHANGED
from ..core.lock import store_lock
from ..errors import NightwardError
from ..runner import classify, judge_from_meta, recompute, standing_rejections
from ._common import (
    DEFAULT_DIR,
    _blast_names,
    _check_reviewed,
    _existing_store,
    _fresh_report,
    _print_summary,
    _report_items,
    _require_report,
    _shown,
    _who,
    app,
    console,
    err_console,
    handle_errors,
)
from ._gitignore import _ignore_problems


def _why_removed(b, meta: dict) -> str:
    """Why a REMOVED behavior reads REMOVED, from what the last run recorded.

    A diagnostic for the human who decides, never proof (D29): proving a
    behavior gone leaked four times (exclusion at collection, norecursedirs,
    __test__ / pycollect hooks, hidden dirs), so removal is always explicit.
    """
    if not b.source:
        return "no recorded test (a baseline from before sources existed)"
    if "completed" not in meta:
        return "the last run did not record which tests ran (an older nightward)"
    if b.source not in set(meta["completed"]):
        return (f"its test {b.source} did not run and pass this run (skipped, xfailed, "
                f"failing, renamed, re-parametrized or not collected?)")
    return (f"its test {b.source} ran and passed without capturing it (gone, or moved "
            f"into a test this run did not run)")


def _run_gaps(meta: dict) -> list[str]:
    """What the last run left out: tests whose behaviors may read REMOVED."""
    gaps = [f"{meta[k]} {k}" for k in ("failed", "errors", "skipped", "xfailed", "deselected")
            if meta.get(k)]
    if meta.get("narrowed"):
        gaps.append("narrowed by -k/-m or a test id")
    files = meta.get("uncollected") or []
    if files:
        gaps.append(f"not collected: {', '.join(files[:3])}"
                    f"{f' and {len(files) - 3} more' if len(files) > 3 else ''}")
    if meta.get("filtered"):
        gaps.append(f"{meta['filtered']} test(s) dropped by a hook")
    return gaps


_EXPLICIT_REMOVAL = ("drop intended ones with `nightward approve --remove NAME...` or "
                     "`nightward approve --remove-group G` after `nightward review`")


def _print_kept_removals(held: list[str], baseline, meta: dict) -> None:
    console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline - "
                  f"dropping one is always your explicit decision:", soft_wrap=True)
    by_reason: dict[str, list[str]] = {}
    for n in held:
        by_reason.setdefault(_why_removed(baseline[n], meta), []).append(n)
    for why, which in by_reason.items():
        console.print(f"  - {escape(', '.join(which))}: {escape(why)}", soft_wrap=True)
    gaps = _run_gaps(meta)
    if gaps:
        console.print(f"  the last run left tests out: {escape('; '.join(gaps))}",
                      soft_wrap=True)
    console.print(f"  {escape(_EXPLICIT_REMOVAL)}.", soft_wrap=True)


def _refuse_include_removed(store: Store) -> None:
    """--include-removed used to infer removal proof from the run; that leaked
    four times, so it no longer removes anything (D29)."""
    removed = [it["name"] for it in _report_items(_require_report(store))
               if it.get("kind") == REMOVED]
    now = (f"REMOVED in the last report: {_shown(removed)} - {_EXPLICIT_REMOVAL}" if removed
           else "nothing is REMOVED in the last report")
    raise NightwardError(f"--include-removed no longer drops anything: removing a behavior "
                         f"from the baseline is always an explicit decision. {now}; "
                         f"`nightward approve --all` approves the NEW/CHANGED ones. Nothing "
                         f"was approved")


def _backfill_sources(store: Store, baseline, pending) -> int:
    """Point unchanged baselines at the test that captures them now (D13)."""
    n = 0
    for name, b in baseline.items():
        p = pending.get(name)
        if (p is not None and p.source and p.source != b.source
                and p.fingerprint() == b.fingerprint() and p.group == b.group):
            store.refresh_source(name, p.source)
            n += 1
    return n


def _approve_one(store: Store, name: str, baseline, pending) -> str:
    if name in pending:
        store.approve(name)
        return "approved"
    if name in baseline:
        store.approve_removal(name)
        return "removed"
    raise NightwardError(f"nothing to approve for {name!r}")


APPROVE_NAMES_ARG = typer.Argument(
    None, help="Behavior(s) to approve. One name always applies (a REMOVED one is dropped); "
               "several are approved like --all limited to them and refuse REMOVED ones "
               "(use --remove)", show_default=False)
APPROVE_GROUP_OPT = typer.Option(
    None, "--group", help="Approve the NEW/CHANGED behaviors of this group (repeatable): "
                          "--all limited to the group, rejections and removals kept",
    show_default=False)

REMOVE_GROUP_OPT = typer.Option(
    None, "--remove-group",
    help="Drop every REMOVED behavior of this group (repeatable), like --remove",
    show_default=False)


@app.command()
@handle_errors
def approve(names: list[str] | None = APPROVE_NAMES_ARG,
            group: list[str] | None = APPROVE_GROUP_OPT,
            all_: bool = typer.Option(False, "--all", help="Approve every NEW/CHANGED behavior"),
            # Retired (D29): kept only to refuse with guidance, never shown.
            include_removed: bool = typer.Option(False, "--include-removed", hidden=True),
            remove: bool = typer.Option(
                False, "--remove",
                help="Drop the named REMOVED behaviors from the baseline - removal is always "
                     "this explicit decision. Refuses any name that isn't REMOVED in the last "
                     "(reviewed) report"),
            remove_group: list[str] | None = REMOVE_GROUP_OPT,
            dir: str = typer.Option(DEFAULT_DIR)):
    """Promote pending behavior(s) into the approved baseline."""
    if include_removed:
        _refuse_include_removed(_existing_store(dir))
    if remove or remove_group:
        if all_ or group or (remove_group and names):
            raise NightwardError("--remove NAME... and --remove-group G stand alone: no "
                                 "--all or --group")
        if remove and not names:
            raise NightwardError("--remove needs the behavior names to drop")
    elif sum(map(bool, (all_, names, group))) > 1:
        raise NightwardError("pick one: behavior names, --group or --all (not both)")
    store = _existing_store(dir)
    with store_lock(store.root, "nightward approve"):
        if remove or remove_group:
            _remove(store, names or [], remove_group or [])
        else:
            _approve(store, dir, names, all_, groups=group)


def _remove(store: Store, names: list[str], groups: list[str]) -> None:
    """Drop exactly these REMOVED behaviors: an explicit human decision, so no run
    proof is needed (D25) - but only what a fresh, reviewed report shows as REMOVED."""
    report = _fresh_report(store, "remove")
    removed = {it["name"]: it.get("group") or "(ungrouped)" for it in _report_items(report)
               if it.get("kind") == REMOVED}
    if groups:
        names = sorted(n for n, g in removed.items() if g in groups)
        if not names:
            raise NightwardError(f"no REMOVED behavior in group "
                                 f"{', '.join(map(repr, groups))}; nothing was removed")
    names = list(dict.fromkeys(names))
    wrong = [n for n in names if n not in removed]
    if wrong:
        raise NightwardError(f"not REMOVED in the last report: {_shown(wrong)} - --remove "
                             f"only drops behaviors that stopped being captured; nothing was "
                             f"removed")
    baseline, pending = store.load_baseline(), store.load_pending()
    _check_reviewed(store, names, baseline, pending, "remove")
    rejected = standing_rejections(store, baseline, pending)
    for n in names:
        store.approve_removal(n)
        console.print(f"[green]removed[/green] {escape(n)}")
        if n in rejected and store.clear_rejection(n):
            console.print(f"  [dim]cleared the rejection of {escape(n)}[/dim]")
    console.print(f"[dim]{len(names)} behavior(s) dropped from the baseline - commit the "
                  f"deletions[/dim]")
    _print_summary(recompute(store, judge=judge_from_meta(store)))


def _group_names(store: Store, baseline, pending, judge,
                 groups: list[str]) -> tuple[list[str], list[str]]:
    """(names to approve, REMOVED names left out) for `approve --group`.

    The group's unapproved behaviors, as `review --group` scopes them (standing
    rejections included, so they are kept; not-run ones excluded), minus
    removals (as with --all; --remove-group drops those). The command line stays
    short however big the group is (R3-DATA-06): 1,440 names don't fit in
    Windows' 32K command line, and neither does the dashboard's group chip.
    """
    known = {b.group or "(ungrouped)" for b in (*baseline.values(), *pending.values())}
    unknown = [g for g in groups if g not in known]
    if unknown:
        raise NightwardError(f"no behavior in group {', '.join(map(repr, unknown))}; "
                             f"nothing was approved")
    changes = [c for c in classify(store, baseline, pending, judge=judge, with_diff=False)
               if c.kind not in (UNCHANGED, NOT_RUN) and (c.group or "(ungrouped)") in groups]
    left_out = [c.name for c in changes if c.kind == REMOVED]
    return [c.name for c in changes if c.name not in left_out], left_out


def _approve(store: Store, dir: str, names: list[str] | None, all_: bool,
             groups: list[str] | None = None) -> None:
    baseline = store.load_baseline()
    pending = store.load_pending()
    if not baseline and not pending:
        raise NightwardError(f"nothing captured in {dir!r} yet - run `nightward run` first")
    # Approve only what the human saw, on a report that still describes the
    # store (D10, D19).
    report = _fresh_report(store, "approve")
    # Reuse the last run's judge (cached verdicts): --all then approves exactly
    # what the report lists as unapproved, and judged-SAME behaviors don't flip
    # back to CHANGED the moment something else is approved. Approving a
    # judged-SAME rewording explicitly by name still re-anchors it.
    judge = judge_from_meta(store)
    held: list[str] = []
    if groups:
        names, held = _group_names(store, baseline, pending, judge, groups)
    # One explicit name is a human override; a group is a bulk approval at any size.
    single = bool(names) and len(names) == 1 and not groups

    kept_rejected: list[str] = []
    rejected = standing_rejections(store, baseline, pending)
    if all_:
        changes = [c for c in classify(store, baseline, pending, judge=judge, with_diff=False)
                   if c.kind not in (UNCHANGED, NOT_RUN)]
        # A bulk approval never drops a behavior (D29): a test that didn't run,
        # or a capture that moved into one, reads REMOVED exactly like a real
        # removal, and no run can tell them apart in general.
        held = [c.name for c in changes if c.kind == REMOVED]
        # A confirmed regression must never ride along with a bulk approval.
        kept_rejected = [c.name for c in changes if c.name in rejected and c.name not in held]
        targets = [c.name for c in changes if c.name not in held and c.name not in rejected]
    elif names or groups:
        # Several names (or --group, the dashboard's group chip) are a bulk
        # approval limited to them: removals and standing rejections stay, as
        # with --all; one explicit name overrides (R2-WEB-03).
        names = list(dict.fromkeys(names))
        unknown = [n for n in names if n not in pending and n not in baseline]
        if unknown:
            raise NightwardError(f"no pending or baseline behavior named "
                                 f"{', '.join(map(repr, unknown))}; nothing was approved")
        listed = {it["name"] for it in _report_items(report)}
        skipped = [n for n in names if n in {it["name"] for it in report.get("not_run") or []}]
        if skipped:
            raise NightwardError(f"{_shown(skipped)}: not checked by the last run (its test "
                                 f"was deselected) - nothing to approve; run without the "
                                 f"-k/-m selection first")
        unchanged = [n for n in names if n not in listed]
        if unchanged:
            raise NightwardError(f"{_shown(unchanged)}: unchanged in the last report - "
                                 f"nothing to approve; nothing was approved")
        removed = [n for n in names if n not in pending] if not single and not groups else []
        if removed:
            raise NightwardError(f"{_shown(removed)}: REMOVED - approving several names never "
                                 f"drops a behavior from the baseline (removal is always an "
                                 f"explicit decision); {_EXPLICIT_REMOVAL}, or approve one "
                                 f"name on its own. Nothing was approved")
        if single:
            targets = names
        else:
            kept_rejected = [n for n in names if n in rejected and n not in held]
            targets = [n for n in names if n not in held and n not in rejected]
    else:
        raise NightwardError("specify a behavior name, --group or --all")
    kept_exhaustive: list[str] = []
    if all_ or groups:
        # An exhaustive group is approved one name at a time, after reading it
        # (docs/review-depth.md); a bulk approval leaves it, like a rejection.
        depths = review_depths(store.root)
        kept_exhaustive = [n for n in targets if depths.of(
            (pending.get(n) or baseline[n]).group) == "exhaustive"]
        targets = [n for n in targets if n not in kept_exhaustive]
    _check_reviewed(store, targets, baseline, pending, "approve")
    if all_:
        refreshed = _backfill_sources(store, baseline, pending)
        if refreshed:
            console.print(f"[dim]refreshed the recorded test of {refreshed} unchanged "
                          f"behavior(s)[/dim]")
    if not targets and not held and not kept_rejected and not kept_exhaustive:
        console.print("nothing to approve in that group" if groups
                      else "nothing to approve - boundary already intact")
        return

    for n in targets:
        if n in rejected:   # only an explicit single name gets here (R3-FIN-03)
            by = f" by {rejected[n]}" if rejected[n] else ""
            console.print(f"[yellow]overrides the rejection{escape(by)}[/yellow] of "
                          f"{escape(n)} (.nightward/rejected/{escape(n)}.rejected.json)",
                          soft_wrap=True)
        verb = _approve_one(store, n, baseline, pending)
        # The short fingerprint ties the approval to the reviewed content.
        what = f" ({pending[n].fingerprint()[:8]})" if n in pending else ""
        console.print(f"[green]{verb}[/green] {escape(n)}{what}")
        if n in rejected and store.clear_rejection(n):
            console.print(f"  [dim]cleared the rejection of {escape(n)} - commit its "
                          f"deletion with the baseline[/dim]", soft_wrap=True)
    if kept_rejected:
        console.print(f"[yellow]kept (rejected)[/yellow] {len(kept_rejected)} behavior(s) "
                      f"rejected as regressions: {escape(', '.join(kept_rejected))}\n  fix the "
                      f"code, or override with `nightward approve <name>`.", soft_wrap=True)
    if kept_exhaustive:
        console.print(f"[yellow]kept (exhaustive)[/yellow] {len(kept_exhaustive)} behavior(s) "
                      f"in a group reviewed exhaustively: {escape(_shown(kept_exhaustive))}\n"
                      f"  approve each by name after reading it: `nightward approve <name>`.",
                      soft_wrap=True)
    if held:
        _print_kept_removals(held, baseline, store.load_run_meta())
    _print_summary(recompute(store, judge=judge))


@app.command()
@handle_errors
def reject(name: str, dir: str = typer.Option(DEFAULT_DIR)):
    """Confirm a change you reviewed as a real regression. The boundary stays breached.

    Works on any unapproved change and on a judged-SAME ruling (a rejection
    overrules the judge). `approve --all` skips it while the same payload is
    pending; an explicit `approve <name>` overrides and clears the rejection.
    """
    store = _existing_store(dir)
    with store_lock(store.root, "nightward reject"):
        report = _fresh_report(store, f"reject {name!r}")
        listed = {it["name"] for it in _report_items(report)}
        if name not in listed:
            # A slip of the name must not "reject" a correct behavior (R3-FIN-07).
            open_ = sorted(_blast_names(report))
            raise NightwardError(
                f"{name!r} is not an unapproved change (or a judged-SAME ruling) in the "
                f"last report - nothing to reject. "
                + (f"Unapproved: {_shown(open_, 10)}" if open_ else "Nothing is unapproved."))
        baseline, pending = store.load_baseline(), store.load_pending()
        # Reject the capture the human saw, not whatever is pending now (R3-FIN-02).
        _check_reviewed(store, [name], baseline, pending, "reject")
        store.mark_rejected(name, by=_who(),
                            token=change_token(baseline.get(name), pending.get(name)))
        report = recompute(store, judge=judge_from_meta(store), baseline=baseline,
                           pending=pending)
    console.print(f"[red]rejected[/red] {escape(name)} - the boundary is "
                  f"{escape(report['boundary'])} ({report['unapproved']} unapproved) until the "
                  f"code changes. Fix it and re-run `nightward run`.", soft_wrap=True)
    ignored = [p for p in _ignore_problems(dir) if p.startswith("rejections")]
    if ignored:
        err_console.print(f"[yellow]warning:[/yellow] {escape(ignored[0])}", soft_wrap=True)
    else:
        console.print(f"[dim]commit {escape(store.rejected_dir.as_posix())}/ with your "
                      f"change: a rejection protects every clone that has it[/dim]",
                      soft_wrap=True)

