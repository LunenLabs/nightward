# Experiment: can the gate handle heterogeneous document-format inputs?

> **Date**: 2026-06-10
> **Input**: 5 real documents in 5 formats, collected from a real-world usage environment
> (PDF 183KB · XLSX 92KB · DOCX 145KB · **HWP** 207KB · TXT 678B cp949).
> The original files and captures exist only in the gitignored `live-experiment/docs-input/` (never committed).
> **Question**: "Can it handle diverse input data such as PDF/XLSX/images?"

## Method

The core was not touched. A ~70-line adapter prototype (`adapters.py`) converts each format into a
stable JSON payload; everything above it is the existing pipeline unchanged:

| Adapter | payload | Strategy |
|---|---|---|
| `from_file` | sha256 + size | **Every format** (including HWP, which has no parser) — artifact gate |
| `from_pdf` | pages + extracted-text length/hash | content gate (robust to byte noise) |
| `from_docx` | paragraph/table counts + text hash | content gate |
| `from_xlsx` | rows/cols per sheet + cell-value hash | content gate |
| `from_text` | encoding detection (utf-8→cp949) + text hash | handles legacy Korean encodings |

## Results

| Step | Result |
|---|---|
| Capture/approve (6 behaviors, 5 formats) | ✅ All succeeded — Hangul file names usable directly as behavior names |
| Unchanged re-run (FP) | ✅ **0** — 6/6 unchanged, including the cp949 TXT |
| Tamper with 1 XLSX cell (TP) | ✅ Exactly **1** breach, on that behavior only; the other 4 formats intact |
| `doctor` diagnosis | ✅ Pinpointed the drifting field (`content_sha256`) + warned "do not scrub if this is a regression" |
| Re-save the PDF (same content, only bytes changed) | ✅ Only the artifact gate breached; **the content gate stayed intact** — separates metadata noise from real changes |

## Conclusions

1. **It works — with no core changes.** nightward gates JSON, so supporting a format essentially means
   "converting it to stable JSON", which is the job of a thin adapter. Even a format with no parser (HWP)
   is gated today via the `from_file` hash.
2. **The two-tier artifact vs. content strategy is the key design.** A byte hash works for everything but
   produces false positives on re-save noise; content extraction is robust but needs a per-format parser.
   Capturing both lets you report "the file changed" separately from "the content changed".
3. **Legacy encodings (cp949) are handled in the adapter layer** — the core is already Unicode-safe.
4. **Productization recommendation**: confirmed it is worth promoting to a `nightward.adapters` module.
   `from_file` is stdlib-only and can ship with the core; pdf/docx/xlsx go in an optional extra
   (`nightward[docs]`). Images (perceptual hash), however, raise a boundary question with visual-regression
   tools, so wait until demand is confirmed.
