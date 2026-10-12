# Document & artifact inputs (`nightward.adapters`)

[nightward](../README.md) reference. Gate PDFs, Word and Excel files, text in any encoding, or raw bytes.


The gate only ever sees JSON, so any file format is one adapter away. Two levels:
`from_file` fingerprints the raw bytes of *anything* (zero dependencies — works
for formats nobody has a parser for), while content adapters extract what matters
so byte-level noise (re-saves, metadata stamps) doesn't breach the gate:

```python
from nightward.adapters import from_file, from_pdf, from_xlsx, from_text

def test_monthly_artifacts(behavior):
    behavior("report.content", from_pdf("out/report.pdf"), group="report")
    behavior("report.artifact", from_file("out/report.pdf"), group="report")
    behavior("export", from_xlsx("out/export.xlsx"), group="data")
    behavior("notice", from_text("out/notice.txt"), group="data")  # utf-8/cp949 auto
```

`from_text` hashes the decoded text with line endings normalized (CRLF, CR -> LF)
and a UTF-8 BOM dropped, so a file written on a Windows laptop and on a Linux CI
runner gates as equal. Content hashes (`text_sha256`, `content_sha256`, `sha256`)
are the gate's view of the content: never scrub them.

`from_pdf` / `from_docx` / `from_xlsx` need `pip install "nightward[docs]"`.
Validated on real-world files (Korean PDF/XLSX/DOCX/HWP/legacy-encoded TXT):
see [the adapter experiment](experiments/2026-06-10-document-input-adapters.md).
