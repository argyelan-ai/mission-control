"""Reset a user's password from the terminal (the way back in when the admin
password is lost).

Run it inside the backend container, as mcuser:

    make reset-password                      # the only admin
    make reset-password EMAIL=you@example.org

which is

    docker compose exec -u mcuser backend python -m scripts.reset_password [--email ...]

The new password is asked for twice (hidden). For scripted use pipe it in
with ``--password-stdin`` — it is never an argv option, so it cannot land in
shell history or ``ps`` output.

Every existing session of that user is signed out (token_version bump, the
same mechanism as logout and the in-app password change): a reset is what you
do when a login may be in the wrong hands.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import TextIO

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import hash_password
from app.models.user import User

# Same minimum as the API (routers/auth.py, UpdateProfileRequest).
MIN_PASSWORD_LENGTH = 6


class ResetError(Exception):
    """A reset that was refused — nothing was changed."""


async def _pick_user(session: AsyncSession, email: str | None) -> User:
    if email:
        user = (await session.exec(select(User).where(User.email == email))).first()
        if not user:
            raise ResetError(f"No user with email {email!r}.")
        return user
    admins = (
        await session.exec(
            select(User).where(User.role == "admin").order_by(User.created_at)
        )
    ).all()
    if not admins:
        raise ResetError("No admin user exists. Open MC in the browser to register one.")
    if len(admins) > 1:
        listing = "\n".join(f"  {a.email}" for a in admins)
        raise ResetError(
            "More than one admin — choose one with --email (make reset-password EMAIL=...):\n"
            + listing
        )
    return admins[0]


async def reset_password(
    session: AsyncSession, *, email: str | None, new_password: str
) -> User:
    """Set ``new_password`` for the user and sign out all their sessions.

    ``email=None`` picks the only admin; refuses to guess between several.
    Raises :class:`ResetError` (and changes nothing) when it cannot proceed.
    """
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ResetError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    user = await _pick_user(session, email)
    user.password_hash = hash_password(new_password)
    # Sign out every existing session (all JWTs carry the old version).
    user.token_version = (user.token_version or 0) + 1
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def read_new_password(*, from_stdin: bool, stdin: TextIO | None = None) -> str:
    if from_stdin:
        return (stdin or sys.stdin).readline().rstrip("\r\n")
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat new password: ")
    if first != second:
        raise ResetError("Passwords do not match.")
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.reset_password",
        description="Reset a user's password and sign out all their sessions.",
    )
    parser.add_argument(
        "--email",
        help="Account to reset. Default: the only admin (refuses when there are several).",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the new password from the first line of stdin instead of prompting.",
    )
    return parser


async def _run(email: str | None, new_password: str) -> User:
    from app.database import engine

    # The shared engine echoes SQL in development mode — that would print the
    # new password hash to the terminal. This is a user-facing tool: quiet.
    engine.sync_engine.echo = False
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await reset_password(session, email=email, new_password=new_password)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        new_password = read_new_password(from_stdin=args.password_stdin)
        user = asyncio.run(_run(args.email or None, new_password))
    except ResetError as exc:
        print(f"Nothing changed: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nNothing changed: cancelled.", file=sys.stderr)
        return 1
    print(f"Password reset for {user.email}. All existing sessions are signed out.")
    if not user.is_active:
        print(
            "Note: this account is deactivated — another admin has to re-activate it "
            "(Settings -> Users).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
