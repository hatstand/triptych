"""Forward the sample confirmations in ./samples to the local SMTP gateway, as TripIt users would.

uv run python scripts/send_samples.py [--from you@example.com] [files...]
"""

import argparse
import smtplib
from email import message_from_bytes, policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="sender", default="me@example.com")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=2525)
    ap.add_argument("files", nargs="*", type=Path)
    args = ap.parse_args()

    files: list[Path] = args.files or sorted((ROOT / "samples").glob("*.eml"))
    with smtplib.SMTP(args.host, args.port) as smtp:
        for f in files:
            msg = message_from_bytes(f.read_bytes(), policy=policy.default)
            smtp.send_message(msg, from_addr=args.sender, to_addrs=["plans@localhost"])
            print(f"sent {f.name}: {msg['subject']}")


if __name__ == "__main__":
    main()
