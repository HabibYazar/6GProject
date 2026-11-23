"""Application-level authentication — local, on this box's own Postgres.

Chosen over a cloud identity provider on purpose. The eventual customer is a
telecom operator running a RIC test appliance on-prem, often air-gapped; an auth
flow that phones home to Google or Auth0 is a non-starter for that buyer, and it
contradicts the "keep it local" requirement. So identities live in the same
Postgres the runs and campaigns already use, and nothing leaves the machine.

The token is a plain JWT signed with a secret from the environment (HS256). That
is the right weight for a single service. When a customer needs SSO or LDAP, the
answer is to put a self-hosted IdP (Keycloak) in front and have it issue OIDC
tokens — and the ONLY thing that changes here is `decode_token`: swap the shared
secret for the IdP's public keys (RS256 + JWKS). Everything downstream of it —
the dependency, the user lookup, the route guards — is unchanged. That seam is
why this file keeps verification in one function.

Enforcement is a deployment switch (`NSG_AUTH_REQUIRED`). It defaults OFF so the
dev box, the QA tools and the current frontend keep working unchanged; a
deployment that turns it on gets every mutating and data route guarded. The
mechanism is real and tested either way — the switch only decides whether an
un-authenticated request is refused or waved through as anonymous.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

import store

# ── Configuration ────────────────────────────────────────────────────────────

_ALGO = "HS256"
_ISSUER = "nsg-platform"
TOKEN_TTL_HOURS = int(os.environ.get("NSG_AUTH_TOKEN_TTL_HOURS", "12"))


def auth_required() -> bool:
    """Is authentication enforced on this deployment? Off unless explicitly on."""
    return os.environ.get("NSG_AUTH_REQUIRED", "0").strip().lower() in ("1", "true", "yes")


def _secret() -> str:
    """The HS256 signing secret.

    From NSG_AUTH_SECRET, which belongs in .env alongside the database URL. If it
    is unset AND enforcement is off, a random per-process secret is used so dev
    tokens work within one run but never persist — a token cannot outlive the
    process that issued it, which is the safe failure. If enforcement is ON and
    no secret is set, that is a misconfiguration worth refusing loudly rather
    than silently signing with a value an attacker could guess.
    """
    s = os.environ.get("NSG_AUTH_SECRET", "").strip()
    if s:
        return s
    if auth_required():
        raise RuntimeError(
            "NSG_AUTH_REQUIRED is on but NSG_AUTH_SECRET is unset — refusing to "
            "sign tokens with a default. Set NSG_AUTH_SECRET in .env.")
    global _EPHEMERAL
    try:
        return _EPHEMERAL
    except NameError:
        _EPHEMERAL = secrets.token_urlsafe(32)
        return _EPHEMERAL


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── Tokens ───────────────────────────────────────────────────────────────────

def create_token(user_id: str, username: str, role: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "preferred_username": username,   # OIDC claim name, so a later IdP lines up
        "role": role,
        "iss": _ISSUER,
        "iat": now,
        "exp": now + dt.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def decode_token(token: str) -> dict:
    """THE verification seam. To move to a Keycloak/OIDC deployment, this is the
    only function that changes: verify RS256 against the IdP's JWKS and accept its
    issuer instead of the shared secret. Callers get the same claims dict back."""
    return jwt.decode(token, _secret(), algorithms=[_ALGO], issuer=_ISSUER)


# ── FastAPI dependencies ─────────────────────────────────────────────────────

class Principal:
    """Who is making this request. `anonymous` is a real state, not None: when
    enforcement is off, routes still run and attribute their work to nobody."""
    def __init__(self, user_id: str | None, username: str, role: str,
                 anonymous: bool = False):
        self.user_id = user_id
        self.username = username
        self.role = role
        self.anonymous = anonymous

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


ANONYMOUS = Principal(user_id=None, username="anonymous", role="anonymous",
                      anonymous=True)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def current_principal(request: Request) -> Principal:
    """Resolve the caller. Never raises: a bad or missing token is anonymous, and
    whether anonymous is ALLOWED is decided by the route guard, not here — so an
    endpoint that is fine for anyone and one that needs a login share this."""
    token = _bearer(request)
    if not token:
        return ANONYMOUS
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        return ANONYMOUS
    user = store.get_user(claims.get("sub") or "")
    if user is None or user.get("disabled"):
        return ANONYMOUS   # token for a deleted or disabled account is worthless
    return Principal(user_id=user["id"], username=user["username"],
                     role=user["role"])


def require_user(principal: Principal = Depends(current_principal)) -> Principal:
    """Guard for a route that needs a logged-in user WHEN enforcement is on.

    With enforcement off, an anonymous caller is allowed through so today's tools
    and frontend keep working; the endpoint simply attributes its work to nobody.
    With enforcement on, anonymous is 401.
    """
    if principal.anonymous and auth_required():
        raise HTTPException(status_code=401, detail="authentication required")
    return principal


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Guard for administrative routes. Admin is always enforced when a real user
    is present; with enforcement off and no user, it is allowed (single-tenant dev
    box has no admin to distinguish). With enforcement on, anonymous is 401 and a
    non-admin user is 403."""
    if principal.anonymous:
        if auth_required():
            raise HTTPException(status_code=401, detail="authentication required")
        return principal
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="administrator role required")
    return principal


# ── WebSocket ────────────────────────────────────────────────────────────────

async def ws_principal(websocket) -> Principal:
    """Resolve the caller on a WebSocket, from `?token=` or a bearer header.

    A browser cannot set headers on a WebSocket handshake, so the token has to
    ride the query string — the standard workaround, and the reason it is
    accepted here at all. It is a real trade-off: a URL can end up in a proxy or
    access log in a way an Authorization header does not. On loopback, to an
    on-prem appliance, that is acceptable; behind any shared ingress it is not,
    and the answer there is a short-lived token rather than pretending the risk
    is absent. The header form is still accepted first for non-browser clients
    (a customer's xApp can set one), so they are not forced into the query.

    Never raises: `require_ws` decides whether anonymous is allowed, exactly as
    the HTTP guards do, so the two cannot drift apart.
    """
    token = None
    header = (websocket.headers.get("authorization") or "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return ANONYMOUS
    try:
        claims = decode_token(token)
    except jwt.PyJWTError:
        return ANONYMOUS
    user = store.get_user(claims.get("sub") or "")
    if user is None or user.get("disabled"):
        return ANONYMOUS
    return Principal(user_id=user["id"], username=user["username"],
                     role=user["role"])


async def require_ws(websocket) -> Principal | None:
    """Guard a WebSocket. Returns the principal, or None after closing it.

    Closes with 1008 (policy violation) and a reason the client can read, rather
    than accepting and then going silent — a connection that opens and produces
    nothing is the hardest kind of failure to diagnose from the other end.
    """
    principal = await ws_principal(websocket)
    if principal.anonymous and auth_required():
        await websocket.close(code=1008, reason="authentication required")
        return None
    return principal
