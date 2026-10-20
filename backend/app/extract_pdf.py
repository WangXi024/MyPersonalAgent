"""Bounded PDF text extraction in a disposable process; never executes document content."""

import json
import sys

from pypdf import PdfReader


def main():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    except (ImportError, ValueError, OSError):
        pass
    try:
        reader = PdfReader(sys.argv[1])
        text = "\n".join((page.extract_text() or "")[:8000] for page in reader.pages[:20])[:32000]
        print(json.dumps({"text": text, "truncated": len(reader.pages) > 20 or len(text) == 32000}))
    except Exception:
        print(json.dumps({"text": "", "status": "unavailable"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
