#!/usr/bin/env python3
"""User administration from the command line.

The platform has no self-service signup by design — access is granted, not
requested. That makes a local escape hatch mandatory rather than a convenience:
if the only administrator forgets their password there is otherwise no way back
in short of hand-editing Postgres, and an operator locked out of an on-prem
appliance has no support desk to call.

It talks to the database directly, not through the API, so it works when the
backend is down and does not need a token to bootstrap the first account.

    python3 tools/nsg_user.py list
    python3 tools/nsg_user.py create <username> [--admin]
    python3 tools/nsg_user.py passwd <username>
    python3 tools/nsg_user.py rename <username> <new_username>
    python3 tools/nsg_user.py role <username> admin|user
    python3 tools/nsg_user.py disable <username>
    python3 tools/nsg_user.py enable  <username>
    python3 tools/nsg_user.py delete  <username>

Passwords are read from a prompt, never from an argument: a password on the
command line lands in shell history and in the process list.
"""
from __future__ import annotations

import argparse
import getpass
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "nsg_ai"))

import auth  # noqa: E402
import store  # noqa: E402
from db import session_scope  # noqa: E402
from models import User  # noqa: E402

MIN_PASSWORD = 8


def _ask_password(confirm: bool = True) -> str:
    while True:
        p1 = getpass.getpass("password: ")
        if len(p1) < MIN_PASSWORD:
            print(f"  too short — at least {MIN_PASSWORD} characters")
            continue
        if not confirm:
            return p1
        if p1 != getpass.getpass("confirm : "):
            print("  passwords do not match")
            continue
        return p1


def _require(username: str) -> dict:
    u = store.get_user_by_username(username)
    if u is None:
        sys.exit(f"no such user: {username}")
    return u


def cmd_list(_args) -> int:
    users = store.list_users()
    if not users:
        print("no users — the first login through the UI will create the admin")
        return 0
    print(f"{'username':<20} {'role':<8} {'status':<10} last login")
    print("-" * 62)
    for u in users:
        print(f"{u['username']:<20} {u['role']:<8} "
              f"{'disabled' if u['disabled'] else 'active':<10} "
              f"{u.get('last_login_at') or '-'}")
    return 0


def cmd_create(args) -> int:
    if store.get_user_by_username(args.username):
        sys.exit(f"username already taken: {args.username}")
    pw = _ask_password()
    role = "admin" if args.admin else "user"
    store.create_user(uuid.uuid4().hex[:16], args.username,
                      auth.hash_password(pw), role)
    print(f"created {args.username} ({role})")
    return 0


def cmd_passwd(args) -> int:
    u = _require(args.username)
    pw = _ask_password()
    with session_scope() as s:
        s.get(User, u["id"]).password_hash = auth.hash_password(pw)
    print(f"password changed for {args.username}")
    print("Note: existing tokens stay valid until they expire "
          f"({auth.TOKEN_TTL_HOURS}h). Disable and re-enable to cut them off now.")
    return 0


def cmd_rename(args) -> int:
    """Rename a login.

    Safe for their work: scenarios, campaigns, runs and ai_interactions are all
    owned by `owner_id`, so the name is a label and nothing follows it.
    """
    u = _require(args.username)
    new = (args.new_username or "").strip()
    if not new or len(new) > 64:
        sys.exit("username must be 1-64 chars")
    if new == args.username:
        print(f"{args.username} already has that name")
        return 0
    if store.get_user_by_username(new):
        sys.exit(f"refusing: '{new}' is already taken")
    store.set_user_username(u["id"], new)
    print(f"{args.username} renamed to {new}")
    print("Note: tokens already issued carry the OLD name until they expire "
          f"({auth.TOKEN_TTL_HOURS}h).")
    return 0


def cmd_role(args) -> int:
    if args.role not in ("admin", "user"):
        sys.exit("role must be 'admin' or 'user'")
    u = _require(args.username)
    if u["role"] == "admin" and args.role == "user":
        admins = [x for x in store.list_users()
                  if x["role"] == "admin" and not x["disabled"]]
        if len(admins) <= 1:
            sys.exit("refusing: this is the last active administrator")
    with session_scope() as s:
        s.get(User, u["id"]).role = args.role
    print(f"{args.username} is now {args.role}")
    return 0


def _set_disabled(username: str, disabled: bool) -> int:
    u = _require(username)
    if disabled and u["role"] == "admin":
        admins = [x for x in store.list_users()
                  if x["role"] == "admin" and not x["disabled"]]
        if len(admins) <= 1:
            sys.exit("refusing: this is the last active administrator")
    store.set_user_disabled(u["id"], disabled)
    print(f"{username} {'disabled' if disabled else 'enabled'}")
    return 0


def cmd_disable(args) -> int:
    return _set_disabled(args.username, True)


def cmd_enable(args) -> int:
    return _set_disabled(args.username, False)


def cmd_delete(args) -> int:
    u = _require(args.username)
    if u["role"] == "admin":
        admins = [x for x in store.list_users()
                  if x["role"] == "admin" and not x["disabled"]]
        if len(admins) <= 1:
            sys.exit("refusing: this is the last active administrator")
    # Their runs and campaigns are NOT deleted — they are evidence, and an
    # experiment does not stop being valid because the person left. The rows
    # keep an owner_id no account answers to, which only an admin can then see.
    with session_scope() as s:
        s.delete(s.get(User, u["id"]))
    print(f"deleted {args.username} (their runs and campaigns are kept)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("create"); p.add_argument("username")
    p.add_argument("--admin", action="store_true"); p.set_defaults(fn=cmd_create)

    p = sub.add_parser("passwd"); p.add_argument("username"); p.set_defaults(fn=cmd_passwd)

    p = sub.add_parser("rename"); p.add_argument("username")
    p.add_argument("new_username"); p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("role"); p.add_argument("username"); p.add_argument("role")
    p.set_defaults(fn=cmd_role)

    for name, fn in (("disable", cmd_disable), ("enable", cmd_enable),
                     ("delete", cmd_delete)):
        p = sub.add_parser(name); p.add_argument("username"); p.set_defaults(fn=fn)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
