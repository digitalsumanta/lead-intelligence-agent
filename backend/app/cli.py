"""Administrative command line for dashboard users.

Run from the backend directory (or with PYTHONPATH=backend):

    python -m app.cli users create --email you@example.com --role admin --name "Your Name"
    python -m app.cli users list
    python -m app.cli users set-role --email rep@example.com --role sales_rep
    python -m app.cli users reset-password --email rep@example.com
    python -m app.cli users deactivate --email rep@example.com
    python -m app.cli users activate --email rep@example.com
    python -m app.cli users sign-out --email rep@example.com

Passwords are prompted for (not echoed) or read from stdin with --password-stdin.
Roles: viewer (read-only), sales_rep, sales_manager (can approve outreach), admin.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.models.entities import USER_ROLES, AppUser


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("New password (min 12 characters): ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match")
    return first


async def _run(args, session_factory=None) -> int:
    from app.services import auth

    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal

    async with session_factory() as db:
        if args.action == "list":
            users = (await db.scalars(select(AppUser).order_by(AppUser.email))).all()
            if not users:
                print("No users yet. Create one with: python -m app.cli users create --email ... --role admin")
            for u in users:
                state = "active" if u.is_active else "deactivated"
                last = u.last_login_at.isoformat(timespec="minutes") if u.last_login_at else "never"
                print(f"{u.email:40} {u.role:14} {state:12} last sign-in: {last}")
            return 0

        if args.action == "create":
            password = _read_password(args)
            try:
                user = await auth.create_user(db, email=args.email, password=password, role=args.role, display_name=args.name)
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
            await db.commit()
            print(f"Created {user.email} ({user.role})")
            return 0

        user = await auth.get_user_by_email(db, args.email)
        if user is None:
            print(f"Error: no user with e-mail {args.email}", file=sys.stderr)
            return 1

        if args.action == "set-role":
            try:
                user.role = auth.validate_role(args.role)
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
            await auth.revoke_user_sessions(db, user.id)  # new role takes effect at next sign-in
        elif args.action == "reset-password":
            try:
                await auth.set_password(db, user, _read_password(args))
            except ValueError as err:
                print(f"Error: {err}", file=sys.stderr)
                return 1
        elif args.action == "deactivate":
            user.is_active = False
            await auth.revoke_user_sessions(db, user.id)
        elif args.action == "activate":
            user.is_active = True
            user.failed_login_count = 0
            user.locked_until = None
        elif args.action == "sign-out":
            await auth.revoke_user_sessions(db, user.id)
        await db.commit()
        print(f"{args.action}: done for {user.email}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Manage dashboard users")
    sub = parser.add_subparsers(dest="group", required=True)
    users = sub.add_parser("users", help="Manage users")
    actions = users.add_subparsers(dest="action", required=True)

    actions.add_parser("list", help="List users")
    create = actions.add_parser("create", help="Create a user")
    create.add_argument("--email", required=True)
    create.add_argument("--role", choices=USER_ROLES, default="viewer")
    create.add_argument("--name", default=None)
    create.add_argument("--password-stdin", action="store_true")

    for name, help_text in (("set-role", "Change a user's role"), ("reset-password", "Set a new password"),
                            ("deactivate", "Block sign-in and end sessions"), ("activate", "Re-enable and unlock"),
                            ("sign-out", "End all of a user's sessions")):
        cmd = actions.add_parser(name, help=help_text)
        cmd.add_argument("--email", required=True)
        if name == "set-role":
            cmd.add_argument("--role", choices=USER_ROLES, required=True)
        if name == "reset-password":
            cmd.add_argument("--password-stdin", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
