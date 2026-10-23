"""Print a one-time link that registers a passkey for an address: how the first user signs up, how
someone else is let in, and how a user who has lost every passkey gets back in.

    docker compose exec web python -m triptych.invite you@example.com
"""

import argparse
import asyncio
from datetime import timedelta

from triptych import auth, db
from triptych.config import settings


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("email", help="the address trips are filed under (and that mail is sent from)")
    p.add_argument("--days", type=float, default=auth.INVITE_TTL.days, help="how long the link works")
    args = p.parse_args()
    try:
        token = await auth.create_invite(args.email, timedelta(days=args.days))
    finally:
        await db.close()
    print(f"{settings.public_base_url}/register/{token}")


if __name__ == "__main__":
    asyncio.run(main())
