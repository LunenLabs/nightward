/* nightward view — render the blast radius from data.json.
 *
 * SECURITY INVARIANT: captured payloads/diffs are arbitrary, untrusted text.
 * This file renders EVERY value through textContent / createTextNode and NEVER
 * through HTML-string assignment (the unsafe DOM sinks). The guard test
 * `test_app_js_has_no_innerhtml` freezes that invariant by forbidding those
 * sink tokens anywhere in this file.
 */
"use strict";

// ---- tiny DOM helpers (textContent only) ----------------------------------
function el(tag, opts) {
  opts = opts || {};
  const node = document.createElement(tag);
  if (opts.cls) node.className = opts.cls;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.attrs) {
    for (const k in opts.attrs) node.setAttribute(k, String(opts.attrs[k]));
  }
  if (opts.title) node.title = opts.title;
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function $(id) { return document.getElementById(id); }

const KIND_LABEL = {
  NEW: "new — a behavior the baseline has never seen",
  CHANGED: "changed — the captured value differs from the approved baseline",
  REMOVED: "removed — in the baseline but missing this run (could be a skipped test)",
};

// ---- shell-safe commands ----------------------------------------------------
// Behavior names come from test code (possibly agent-written) and these commands
// are pasted into the approver's own shell, so a name is NEVER concatenated raw.
// The generator quotes every name per shell (nightward/shellquote.py, tested
// against real shells) into data.quoted; a name without a safe form there
// (e.g. "100%" in cmd.exe) yields no command at all.
const SHELL_LABEL = { posix: "bash / zsh / sh", powershell: "PowerShell", cmd: "cmd.exe" };
let QUOTED = {};
let SHELL = (typeof navigator !== "undefined" && /Windows/.test(navigator.userAgent || ""))
  ? "powershell" : "posix";

function setQuoting(quoted, shell) {
  QUOTED = quoted || {};
  if (shell && SHELL_LABEL[shell]) SHELL = shell;
}

function cliCommand(verb, names) {
  const args = [];
  for (const n of names) {
    const q = Object.prototype.hasOwnProperty.call(QUOTED, n) ? QUOTED[n][SHELL] : null;
    if (typeof q !== "string") return null;
    args.push(q);
  }
  // a leading "-" would be read as an option; quoting can't prevent that
  const sep = names.some(function (n) { return n.charAt(0) === "-"; }) ? ["--"] : [];
  return ["nightward", verb].concat(sep, args).join(" ");
}

// ---- clipboard copy chip --------------------------------------------------
function copyChip(label, command) {
  if (command == null) {
    const none = el("span", { cls: "copy-pair copy-none" });
    none.appendChild(el("span", {
      cls: "copy-cmd",
      text: label + ": a name here can't be pasted safely into " + SHELL_LABEL[SHELL] +
        " - switch the shell above, or type the name yourself",
    }));
    return none;
  }
  const btn = el("button", { cls: "copy-chip", text: label, attrs: { type: "button" } });
  const cmd = el("code", { cls: "copy-cmd", text: command });
  btn.addEventListener("click", function () {
    const done = function () {
      const prev = btn.textContent;
      btn.textContent = "copied ✓";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(command).then(done, done);
    } else {
      done();
    }
  });
  const wrap = el("span", { cls: "copy-pair" });
  wrap.appendChild(btn);
  wrap.appendChild(cmd);
  return wrap;
}

// ---- diff rendering (colored +/- lines, header relabeled) -----------------
function renderDiff(container, diffText) {
  const pre = el("pre", { cls: "diff" });
  const lines = (diffText || "").split("\n");
  if (!diffText) {
    pre.appendChild(el("span", { cls: "diff-empty", text: "(no text diff)" }));
    container.appendChild(pre);
    return;
  }
  for (const line of lines) {
    let cls = "diff-ctx", text = line;
    if (line.startsWith("--- ")) { cls = "diff-meta"; text = "baseline (before): " + line.slice(4); }
    else if (line.startsWith("+++ ")) { cls = "diff-meta"; text = "this run (after): " + line.slice(4); }
    else if (line.startsWith("@@")) { cls = "diff-hunk"; }
    else if (line.startsWith("+")) { cls = "diff-add"; }
    else if (line.startsWith("-")) { cls = "diff-del"; }
    // names characters that print the same on both sides (escaped as \uXXXX above it)
    else if (line.startsWith("? ")) { cls = "diff-hint"; }
    pre.appendChild(el("div", { cls: "diff-line " + cls, text: text }));
  }
  container.appendChild(pre);
}

// ---- state machine --------------------------------------------------------
function showEmpty(title, body, command) {
  const box = $("empty");
  clear(box);
  box.hidden = false;
  box.appendChild(el("h2", { text: title }));
  box.appendChild(el("p", { text: body }));
  if (command) box.appendChild(copyChip("copy", command));
}

function renderMeta(meta) {
  const m = $("run-meta");
  clear(m);
  if (!meta) return;
  if (meta.generated) m.appendChild(el("span", { cls: "meta-item", text: "generated: " + meta.generated }));
  if (meta.source) m.appendChild(el("span", { cls: "meta-item", text: "source: " + meta.source }));
  if (meta.judge) m.appendChild(el("span", { cls: "meta-item meta-judge", text: "judge: " + meta.judge }));
}

// Banner states that are neither a pass nor a breach: the verdict can't be trusted.
const UNTRUSTED = {
  stale: {
    title: "Report is stale",
    explain: "The approved baseline or the captured behavior changed after this report was computed, so its verdict no longer applies. Re-run nightward for a fresh blast radius. (`nightward gate` exits 1.)",
  },
  incomplete: {
    title: "Capture incomplete",
    explain: "Nothing captured is unapproved, but some capture tests failed or errored, so their behaviors are missing from this report. Fix them and re-run. (`nightward gate` exits 1.)",
  },
};

function renderBanner(report, state) {
  const b = $("banner");
  clear(b);
  const intact = state === "intact";
  const untrusted = UNTRUSTED[state];
  b.className = "banner " + (untrusted ? "untrusted " + state : state);
  const head = el("div", { cls: "banner-head" });
  head.appendChild(el("span", { cls: "status-dot", attrs: { "aria-hidden": "true" } }));
  head.appendChild(el("span", {
    cls: "banner-state",
    text: untrusted ? untrusted.title : intact ? "Boundary intact" : "Boundary breached",
  }));
  if (state === "breached") head.appendChild(el("span", { cls: "banner-count", text: (report.unapproved || 0) + " unapproved" }));
  b.appendChild(head);
  b.appendChild(el("p", {
    cls: "banner-explain",
    text: untrusted ? untrusted.explain : intact
      ? "No behavior has moved since the approved baseline. (`nightward gate` passes in CI — exit 0.)"
      : "There are " + (report.unapproved || 0) + " unapproved change(s). Review each one: approve it if it was intended, fix the code if it is a regression. (`nightward gate` exits 1.)",
  }));
}

function bannerState(report, meta) {
  if (meta && meta.stale) return "stale";
  if (report.boundary === "intact") return report.incomplete ? "incomplete" : "intact";
  return "breached";
}

function renderWarnings(report, meta) {
  const w = $("warnings");
  clear(w);
  // captured-data exposure warning (always — this page may contain real output)
  const exposure = el("div", { cls: "warn warn-info" });
  exposure.appendChild(el("strong", { text: "Heads-up " }));
  exposure.appendChild(document.createTextNode(
    "This page can contain captured system output. Review for sensitive data before publishing it anywhere public."));
  w.appendChild(exposure);

  if (meta && (meta.skipped || meta.failed || meta.errors)) {
    const parts = [];
    if (meta.skipped) parts.push(meta.skipped + " skipped");
    if (meta.failed) parts.push(meta.failed + " failed");
    if (meta.errors) parts.push(meta.errors + " errored");
    const warn = el("div", { cls: "warn warn-alert" });
    warn.appendChild(el("strong", { text: parts.join(" · ") + " " }));
    warn.appendChild(document.createTextNode(
      "— skipped tests don't capture their behavior (it shows up as a false REMOVED), and failed tests make the blast radius incomplete."));
    w.appendChild(warn);
  }
}

function renderCounts(counts) {
  const c = $("counts");
  clear(c);
  c.hidden = false;
  const items = [
    ["unchanged", "unchanged", counts.unchanged],
    ["changed", "changed", counts.changed],
    ["new", "new", counts.new],
    ["removed", "removed", counts.removed],
  ];
  if (counts.judged_same) items.push(["judged", "judged same (AI)", counts.judged_same]);
  for (const [key, label, n] of items) {
    const cell = el("div", { cls: "count count-" + key });
    cell.appendChild(el("span", { cls: "count-n", text: n }));
    cell.appendChild(el("span", { cls: "count-label", text: label }));
    c.appendChild(cell);
  }
}

function allChanges(report) {
  const out = [];
  const br = report.blast_radius || {};
  for (const group of Object.keys(br)) {
    for (const it of br[group]) out.push(it);
  }
  return out;
}

function renderBulk(report) {
  const box = $("bulk");
  clear(box);
  const changes = allChanges(report);
  if (!changes.length) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(el("h3", { text: "Bulk actions" }));
  box.appendChild(copyChip("copy approve-all command", "nightward approve --all"));
  const warn = el("p", { cls: "bulk-warn", text: "approve --all can bury a real regression along with the intended changes. Review the cards first." });
  box.appendChild(warn);
}

let STATE = { changes: [], filters: { kinds: new Set(["NEW", "CHANGED", "REMOVED"]), group: "*", unapprovedOnly: false } };

function renderControls(report) {
  const box = $("controls");
  clear(box);
  const changes = allChanges(report);
  if (!changes.length) { box.hidden = true; return; }
  box.hidden = false;

  // kind toggles
  const kindWrap = el("div", { cls: "control-row" });
  kindWrap.appendChild(el("span", { cls: "control-label", text: "kind:" }));
  ["NEW", "CHANGED", "REMOVED"].forEach(function (k) {
    const id = "kind-" + k;
    const lbl = el("label", { cls: "chk chk-" + k.toLowerCase() });
    const cb = el("input", { attrs: { type: "checkbox", id: id } });
    cb.checked = true;
    cb.addEventListener("change", function () {
      if (cb.checked) STATE.filters.kinds.add(k); else STATE.filters.kinds.delete(k);
      renderGroups(report);
    });
    lbl.appendChild(cb);
    lbl.appendChild(el("span", { text: " " + k }));
    kindWrap.appendChild(lbl);
  });
  box.appendChild(kindWrap);

  // which shell the copy-paste commands are quoted for
  const sWrap = el("div", { cls: "control-row" });
  sWrap.appendChild(el("span", { cls: "control-label", text: "commands for:" }));
  const shellSel = el("select", { cls: "shell-select", attrs: { "aria-label": "shell for copied commands" } });
  Object.keys(SHELL_LABEL).forEach(function (s) {
    const opt = el("option", { text: SHELL_LABEL[s], attrs: { value: s } });
    opt.selected = s === SHELL;
    shellSel.appendChild(opt);
  });
  shellSel.addEventListener("change", function () { SHELL = shellSel.value; renderGroups(report); });
  sWrap.appendChild(shellSel);
  box.appendChild(sWrap);

  // group select
  const groups = Object.keys(report.blast_radius || {});
  if (groups.length > 1) {
    const gWrap = el("div", { cls: "control-row" });
    gWrap.appendChild(el("span", { cls: "control-label", text: "group:" }));
    const sel = el("select", { cls: "group-select" });
    sel.appendChild(el("option", { text: "all", attrs: { value: "*" } }));
    groups.forEach(function (g) { sel.appendChild(el("option", { text: g, attrs: { value: g } })); });
    sel.addEventListener("change", function () { STATE.filters.group = sel.value; renderGroups(report); });
    gWrap.appendChild(sel);
    box.appendChild(gWrap);
  }
}

function passesFilter(it) {
  if (!STATE.filters.kinds.has(it.kind)) return false;
  if (STATE.filters.group !== "*" && (it.group || "(ungrouped)") !== STATE.filters.group) return false;
  return true;
}

function renderCard(it) {
  const card = el("article", { cls: "card card-" + it.kind.toLowerCase() });
  const head = el("div", { cls: "card-head" });
  const badge = el("span", { cls: "badge badge-" + it.kind.toLowerCase(), text: it.kind, title: KIND_LABEL[it.kind] || it.kind });
  head.appendChild(badge);
  if (it.judged) {
    head.appendChild(el("span", {
      cls: "badge badge-judged",
      text: "AI-judged",
      title: "An LLM judge ruled this fingerprint mismatch semantically DIFFERENT — verdict by " + (it.judge_model || "unknown model"),
    }));
  }
  if (it.rejected) {
    head.appendChild(el("span", {
      cls: "badge badge-removed",
      text: "rejected",
      title: "This exact payload is a standing rejection" + (it.rejected_by ? " by " + it.rejected_by : "") + " (.nightward/rejected/) - approving it overrides that decision",
    }));
  }
  head.appendChild(el("span", { cls: "card-name", text: it.name }));
  card.appendChild(head);

  if (it.judged) {
    const j = el("p", { cls: "judge-note" });
    j.appendChild(el("strong", { text: (it.judge_model || "judge") + ": " }));
    j.appendChild(document.createTextNode(it.judge_reason || "ruled DIFFERENT"));
    card.appendChild(j);
  }

  renderDiff(card, it.diff);

  const actions = el("div", { cls: "card-actions" });
  actions.appendChild(copyChip("approve (intended change)", cliCommand("approve", [it.name])));
  actions.appendChild(copyChip("reject (regression)", cliCommand("reject", [it.name])));
  card.appendChild(actions);
  return card;
}

// A REMOVED item may be a test that merely didn't run; dropping it from the
// baseline is a per-card decision, never part of a group approval (R2-WEB-03).
function groupApproveNames(items) {
  return items.filter(function (i) { return i.kind !== "REMOVED"; })
    .map(function (i) { return i.name; });
}

function renderGroups(report) {
  const root = $("groups");
  clear(root);
  const br = report.blast_radius || {};
  const groups = Object.keys(br).sort();
  let shown = 0;
  for (const group of groups) {
    if (STATE.filters.group !== "*" && group !== STATE.filters.group) continue;
    const items = br[group].filter(passesFilter);
    if (!items.length) continue;
    shown += items.length;

    const details = el("details", { cls: "group" });
    details.open = true;
    const summary = el("summary", { cls: "group-head" });
    summary.appendChild(el("span", { cls: "group-name", text: group }));
    summary.appendChild(el("span", { cls: "group-count", text: items.length + " item(s)" }));
    const names = groupApproveNames(items);
    if (names.length) {
      const label = names.length === items.length ? "approve this group"
        : "approve " + names.length + " NEW/CHANGED (removals: approve each on its card)";
      summary.appendChild(copyChip(label, cliCommand("approve", names)));
    }
    details.appendChild(summary);

    for (const it of items) details.appendChild(renderCard(it));
    root.appendChild(details);
  }
  if (!shown) root.appendChild(el("p", { cls: "filter-empty", text: "No changes match the current filters." }));
}

function renderLegend() {
  const box = $("legend");
  clear(box);
  box.hidden = false;
  ["NEW", "CHANGED", "REMOVED"].forEach(function (k) {
    const item = el("span", { cls: "legend-item" });
    item.appendChild(el("span", { cls: "badge badge-" + k.toLowerCase(), text: k }));
    item.appendChild(el("span", { cls: "legend-text", text: KIND_LABEL[k] }));
    box.appendChild(item);
  });
}

// Fingerprint mismatches a judge ruled semantically SAME. They are not in the
// boundary, but a wrong SAME is a hole in the gate, so list each one with the
// wording the judge accepted for a human to audit.
function renderJudgedSame(report) {
  const box = $("judged");
  clear(box);
  const items = report.judged_same || [];
  box.hidden = !items.length;
  if (!items.length) return;
  const details = el("details", { cls: "group" });
  details.open = true;
  const summary = el("summary", { cls: "group-head" });
  summary.appendChild(el("span", { cls: "group-name", text: "ruled semantically SAME by the judge — audit the wording" }));
  summary.appendChild(el("span", { cls: "group-count", text: items.length + " item(s)" }));
  details.appendChild(summary);
  for (const it of items) {
    const card = el("article", { cls: "card" });
    const head = el("div", { cls: "card-head" });
    head.appendChild(el("span", {
      cls: "badge badge-judged",
      text: "judged SAME",
      title: "Not in the boundary: an LLM judge ruled this fingerprint mismatch semantically SAME — verdict by " + (it.judge_model || "unknown model"),
    }));
    head.appendChild(el("span", { cls: "card-name", text: it.name }));
    card.appendChild(head);
    const j = el("p", { cls: "judge-note" });
    j.appendChild(el("strong", { text: (it.judge_model || "judge") + ": " }));
    j.appendChild(document.createTextNode(it.judge_reason || "ruled SAME"));
    card.appendChild(j);
    renderDiff(card, it.diff);
    // A wrong SAME is overruled by rejecting it: it becomes unapproved again.
    const actions = el("div", { cls: "card-actions" });
    actions.appendChild(copyChip("reject (overrule the judge)", cliCommand("reject", [it.name])));
    card.appendChild(actions);
    details.appendChild(card);
  }
  box.appendChild(details);
}

// ---- entry ----------------------------------------------------------------
function render(data) {
  setQuoting(data.quoted);
  renderMeta(data.meta);
  const report = data.report;

  if (!report) {
    showEmpty("No run recorded yet",
      "Capture behaviors first, then refresh this page to see the blast radius.",
      "nightward run example");
    return;
  }

  const state = bannerState(report, data.meta);
  renderBanner(report, state);
  renderWarnings(report, data.meta);
  renderJudgedSame(report);

  if (state === "stale") {
    showEmpty("Re-run to refresh this report",
      "The diffs in this report compare inputs that are no longer on disk, so they are not shown.",
      "nightward run .");
    return;
  }

  const changes = allChanges(report);

  if ((data.meta && data.meta.baseline_count === 0) && report.boundary !== "breached") {
    showEmpty("No approved baseline yet",
      "Approve the current behavior as the baseline; every later change will be gated against this boundary.",
      "nightward approve --all");
    return;
  }

  if (report.boundary === "intact" || !changes.length) {
    showEmpty("No behavior changed",
      state === "incomplete"
        ? "Everything captured matches the baseline, but the capture is incomplete. This is NOT a safe place to stop."
        : "Everything matches the last approved baseline. This is a safe place to stop.", null);
    renderCounts(report.counts || {});
    return;
  }

  $("empty").hidden = true;
  renderCounts(report.counts || {});
  renderBulk(report);
  renderControls(report);
  renderLegend();
  renderGroups(report);
}

function fail(msg) {
  showEmpty("Failed to load data", msg, null);
}

fetch("./data.json", { cache: "no-store" })
  .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
  .then(render)
  .catch(function (e) {
    fail("Could not load data.json: " + e.message + " — open this page via `nightward view --serve` (fetch is blocked on file://).");
  });
