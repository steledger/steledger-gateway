#!/usr/bin/env python3
"""Rebuild site/llms-full.txt: the four docs pages, concatenated.

The corpus is what an agent reads in one fetch, so it must never drift from the
pages themselves. Run after editing anything in site/docs/:

    python3 scripts/gen_llms_full.py
"""
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"
PAGES = ("about", "quickstart", "nvs", "mcp")
HEADER = "# Steledger — full documentation corpus\n# Source: https://api.steledger.com/llms.txt\n\n"


def build() -> str:
    return HEADER + "".join(
        (SITE / "docs" / f"{p}.md").read_text().rstrip("\n") + "\n\n---\n\n" for p in PAGES
    )


if __name__ == "__main__":
    (SITE / "llms-full.txt").write_text(build())
    print(f"wrote {SITE / 'llms-full.txt'}")
