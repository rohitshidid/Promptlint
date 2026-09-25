"""Admin commands (no dashboard needed):

python -m app.cli migrate                       # alembic upgrade head
python -m app.cli users                         # list accounts
python -m app.cli set-plan you@example.com dev  # change a plan (free | dev | pro)
python -m app.cli disable you@example.com       # block an account and its keys
python -m app.cli enable you@example.com
python -m app.cli prune                         # delete events older than the retention window
"""

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.config import load_config
from app.db import ApiKey, User, make_engine, make_sessionmaker, prune
from app.main import run_migrations
from app.settings import get_settings


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.cmd == "migrate":
        await asyncio.to_thread(run_migrations, settings.database_url)
        print("migrated")
        return 0
    engine = make_engine(settings.database_url)
    maker = make_sessionmaker(engine)
    try:
        async with maker() as s:
            if args.cmd == "users":
                keys = func.count(ApiKey.id).filter(ApiKey.revoked_at.is_(None))
                rows = (
                    await s.execute(select(User, keys).outerjoin(ApiKey).group_by(User.id).order_by(User.id))
                ).all()
                for user, n in rows:
                    flag = " (disabled)" if user.disabled else ""
                    print(
                        f"{user.id:5d}  {user.email:40s} {user.plan:6s} {n} key(s)  {user.created_at:%Y-%m-%d}{flag}"
                    )
                return 0
            if args.cmd == "prune":
                stored, events = await prune(
                    s,
                    event_days=settings.event_retention_days,
                    stored_days=settings.stored_prompt_retention_days,
                )
                print(f"deleted {events} events and {stored} stored prompts")
                return 0
            user = await s.scalar(select(User).where(User.email == args.email.strip().lower()))
            if user is None:
                print(f"no user {args.email}", file=sys.stderr)
                return 1
            if args.cmd == "set-plan":
                plans = load_config(settings.config_dir).plans.plans
                if args.plan not in plans:
                    print(f"unknown plan {args.plan!r}; choose from {', '.join(plans)}", file=sys.stderr)
                    return 1
                user.plan = args.plan
            else:
                user.disabled = args.cmd == "disable"
            await s.commit()
            print(f"{user.email}: plan={user.plan} disabled={user.disabled}")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("users")
    sub.add_parser("prune")
    p = sub.add_parser("set-plan")
    p.add_argument("email")
    p.add_argument("plan")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("email")
    sys.exit(asyncio.run(_run(ap.parse_args())))


if __name__ == "__main__":
    main()
