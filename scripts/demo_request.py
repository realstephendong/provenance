#!/usr/bin/env python3
"""One canned POST /context, for checking the service by hand.

    python scripts/demo_request.py
    python scripts/demo_request.py --raw
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provenance import config                      # noqa: E402
from provenance.cli.main import render             # noqa: E402
from provenance.models import ContextResponse      # noqa: E402

FILE = "webhooks/delivery.py"
START, END = 20, 40


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="store_true", help="print the raw JSON response")
    parser.add_argument("--repo", default=str(config.SEED_REPO))
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    source = (repo / FILE)
    if not source.is_file():
        sys.exit(f"{source} not found -- run `make seed` first")
    code = "".join(source.read_text().splitlines(keepends=True)[START - 1:END])

    resp = httpx.post(f"{config.SERVICE_URL}/context", timeout=120, json={
        "code": code, "file_path": FILE, "repo_root": str(repo),
        "line_start": START, "line_end": END, "language": "python",
    })
    resp.raise_for_status()

    if args.raw:
        print(json.dumps(resp.json(), indent=2))
    else:
        print(render(ContextResponse.model_validate(resp.json()), f"{FILE}:{START}-{END}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
