"""Run squawk over dbmate migrations with dbmate's own semantics.

* Only the `-- migrate:up` section is linted; `down` sections are dev-only and exist to drop things.
* dbmate wraps each file in a transaction unless its up line says `transaction:false`, so squawk is
  told `--assume-in-transaction` per file accordingly (which also decides whether CONCURRENTLY is
  allowed and whether statements must be individually re-runnable).

    uv run python scripts/lint_migrations.py [files...]
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UP = re.compile(r"^--\s*migrate:up\b(.*)$", re.MULTILINE)
DOWN = re.compile(r"^--\s*migrate:down\b", re.MULTILINE)


def up_section(sql: str) -> tuple[str, bool]:
    """(up SQL padded to keep line numbers, runs-in-transaction)."""
    m = UP.search(sql)
    if m is None:
        raise SystemExit("missing '-- migrate:up'")
    in_tx = "transaction:false" not in m.group(1)
    down = DOWN.search(sql, m.end())
    body = sql[: down.start()] if down else sql
    return body, in_tx


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]] or sorted((ROOT / "db" / "migrations").glob("*.sql"))
    failed = False
    for f in files:
        body, in_tx = up_section(f.read_text())
        cmd = ["squawk", "--pg-version=18.0", f"--stdin-filepath={f}"]
        cmd.append("--assume-in-transaction" if in_tx else "--no-assume-in-transaction")
        proc = subprocess.run(cmd, input=body, text=True)
        failed |= proc.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
