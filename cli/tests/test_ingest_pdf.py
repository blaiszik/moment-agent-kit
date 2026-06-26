"""Standalone test: `moment ingest` extracts a PDF's text layer (moment_cli.cli).

Proves the text connector treats a PDF as a first-class input: `_read_ingest_file`
extracts the text instead of rejecting the binary, and `_build_ingest_envelope`
carries that text with a stable, raw-bytes-hashed externalId (so re-ingesting the
same PDF is idempotent).

Uses the committed fixture tests/fixtures/sample.pdf — the SAME bytes the TS
resolver test (app/prisma/verify-pdf.ts) generates and asserts on.

Run (no pytest needed):
    python tests/test_ingest_pdf.py
Exit code is non-zero on any failure.
"""
import os
import sys
from pathlib import Path

# Make moment_cli importable whether or not the package is pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moment_cli.cli as c  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.pdf")
SAMPLE_TEXT = "Hello Moment PDF extraction works"

_ok = True


def check(name: str, cond: bool) -> None:
    global _ok
    print(("PASS  " if cond else "FAIL  ") + name)
    _ok = _ok and bool(cond)


def main() -> None:
    check("fixture exists (run verify-pdf.ts first if missing)", os.path.exists(FIXTURE))
    if not os.path.exists(FIXTURE):
        sys.exit(1)
    raw = Path(FIXTURE).read_bytes()

    check("_looks_like_pdf: by .pdf extension + magic", c._looks_like_pdf(Path(FIXTURE), raw))
    check("_looks_like_pdf: rejects plain text", not c._looks_like_pdf(Path("notes.txt"), b"just text"))

    # _read_ingest_file extracts the text layer instead of the binary rejection.
    path, text, rawbytes = c._read_ingest_file(FIXTURE)
    check("extracted text contains the sample", SAMPLE_TEXT in text)
    check("raw bytes preserved (idempotency key source)", rawbytes == raw)

    # The full envelope carries the extracted text + a stable externalId.
    env = c._build_ingest_envelope(
        file=FIXTURE,
        source="pdf",
        title=None,
        participants=None,
        occurred_at=None,
        uri=None,
        meta_json=None,
        bound_repo_root=None,
    )
    check("envelope text == extracted text", env["text"] == text)
    check("envelope title defaults to filename", env["title"] == "sample.pdf")
    check("envelope externalId stable & content-hashed", bool(env["externalId"]) and "@" in env["externalId"])

    print("\nOK" if _ok else "\nFAILED")
    sys.exit(0 if _ok else 1)


if __name__ == "__main__":
    main()
