#!/usr/bin/env python3
"""Local auth: password hashing, tokens, the bootstrap rule, and the guard.

Unit-level where it can be (hashing, token round-trip, the enforcement switch's
effect on the two dependencies) so it needs no running server or database — the
guards are pure functions of a Principal and the NSG_AUTH_REQUIRED env var. The
one thing that needs the DB (the bootstrap-first-user rule) is exercised by the
end-to-end curl checks in the session, not here, to keep this runnable anywhere.

Run: cd nsg_ai && ../venv/bin/python test_auth.py
"""
from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("=" * 60)
    print("LOCAL AUTH")
    print("=" * 60)

    # Enforcement OFF for the first half. Set explicitly rather than popped:
    # importing `auth` pulls in db.py, which loads .env — and .env now carries
    # NSG_AUTH_REQUIRED=1, so a popped variable comes straight back. _load_env_file
    # does not override a value the environment already has, which is what makes
    # setting it here work.
    os.environ["NSG_AUTH_REQUIRED"] = "0"
    os.environ["NSG_AUTH_SECRET"] = "test-secret"
    import auth  # noqa: E402
    from fastapi import HTTPException  # noqa: E402

    # ── passwords ────────────────────────────────────────────────────────────
    h = auth.hash_password("correct horse")
    check("a hash is not the plaintext", h != "correct horse")
    check("the right password verifies", auth.verify_password("correct horse", h))
    check("a wrong password does not", not auth.verify_password("Correct Horse", h))
    check("a garbage hash fails closed", not auth.verify_password("x", "not-a-hash"))

    # ── tokens ───────────────────────────────────────────────────────────────
    tok = auth.create_token("u1", "an_admin", "admin")
    claims = auth.decode_token(tok)
    check("a token round-trips its subject", claims.get("sub") == "u1")
    check("the token carries the OIDC username claim",
          claims.get("preferred_username") == "an_admin", claims.get("preferred_username"))
    check("the token carries the role", claims.get("role") == "admin")

    import jwt  # noqa: E402
    tampered = tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")
    try:
        auth.decode_token(tampered)
        check("a tampered token is rejected", False, "decoded")
    except jwt.PyJWTError:
        check("a tampered token is rejected", True)

    other = jwt.encode({"sub": "x", "iss": "nsg-platform"}, "WRONG-secret",
                       algorithm="HS256")
    try:
        auth.decode_token(other)
        check("a token signed with another secret is rejected", False)
    except jwt.PyJWTError:
        check("a token signed with another secret is rejected", True)

    # ── the guard, enforcement OFF ───────────────────────────────────────────
    anon = auth.ANONYMOUS
    user = auth.Principal("u2", "a_user", "user")
    admin = auth.Principal("u1", "an_admin", "admin")

    check("enforcement follows the env switch (off)", not auth.auth_required())
    check("off: anonymous passes require_user", auth.require_user(anon) is anon)
    check("off: anonymous passes require_admin", auth.require_admin(anon) is anon)
    # A real non-admin user is still refused admin, switch or no switch.
    try:
        auth.require_admin(user)
        check("off: a non-admin user is still refused admin", False)
    except HTTPException as e:
        check("off: a non-admin user is still refused admin", e.status_code == 403)

    # ── the guard, enforcement ON ────────────────────────────────────────────
    os.environ["NSG_AUTH_REQUIRED"] = "1"
    check("enforcement reads the env switch", auth.auth_required())
    try:
        auth.require_user(anon)
        check("on: anonymous is 401 at require_user", False)
    except HTTPException as e:
        check("on: anonymous is 401 at require_user", e.status_code == 401)
    check("on: a logged-in user passes require_user",
          auth.require_user(user) is user)
    try:
        auth.require_admin(user)
        check("on: a non-admin is 403 at require_admin", False)
    except HTTPException as e:
        check("on: a non-admin is 403 at require_admin", e.status_code == 403)
    check("on: an admin passes require_admin", auth.require_admin(admin) is admin)

    # A secret is mandatory once enforcement is on.
    os.environ.pop("NSG_AUTH_SECRET", None)
    try:
        auth._secret()
        check("on: refuses to sign without a secret", False)
    except RuntimeError:
        check("on: refuses to sign without a secret", True)

    os.environ["NSG_AUTH_REQUIRED"] = "0"
    os.environ["NSG_AUTH_SECRET"] = "test-secret"

    # ── the change-own-password rules, as pure checks on the endpoint's
    #    contract. The endpoint itself needs a server; what is asserted here is
    #    the reasoning it encodes, so a future refactor cannot quietly loosen it.
    import inspect  # noqa: PLC0415
    import pathlib as _pl  # noqa: PLC0415
    src = (_pl.Path(__file__).parent / "main.py").read_text()
    body = src[src.index("def auth_change_own_password"):]
    body = body[:body.index("\n@app.")]
    check("changing a password verifies the CURRENT one",
          "verify_password" in body)
    check("it refuses a new password shorter than 8",
          "at least 8 chars" in body)
    check("it refuses reusing the current password",
          "must differ from the current one" in body)
    # What matters is the SHAPE of the request: the endpoint can only be given a
    # current password and a new one, so there is no field a non-secret (an email
    # address, a username) could arrive in and be treated as proof. Grepping the
    # function text for "email" was the first version of this check and it failed
    # on the docstring explaining why email is not accepted.
    model = src[src.index("class ChangePasswordRequest"):]
    model = model[:model.index("\n\n\n")]
    fields = {l.split(":")[0].strip() for l in model.splitlines()
              if ":" in l and not l.strip().startswith(("class", "\"\"\""))}
    check("the request accepts nothing but the two passwords",
          fields == {"current_password", "new_password"}, str(sorted(fields)))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {'; '.join(FAILS)}")
        return 1
    print("ALL AUTH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
