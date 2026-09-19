"""Expose the first failure in GitHub Checks without requiring log-blob access."""

import re
import sys
from pathlib import Path


def report(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    failures = re.split(r"\n={10,}\n", text)
    first = next((part for part in failures if part.startswith(("ERROR:", "FAIL:"))), text)
    # Keep the test name and the root exception, not a cascade of secondary errors.
    if len(first) > 2200:
        first = first[:500] + "\n... traceback shortened ...\n" + first[-1600:]
    escaped = first.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error title=Django regression failure::" + escaped)


if __name__ == "__main__":
    report(sys.argv[1])
