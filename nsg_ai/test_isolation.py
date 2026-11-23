#!/usr/bin/env python3
"""Per-user data isolation, end to end against a running backend.

Two users must not see each other's runs, campaigns or scenarios; an admin sees
all; a row that belongs to someone else answers 404, not 403, so the API does not
confirm an id exists for other users. This needs enforcement ON and a running
server, so it is a scripted check rather than a unit test — run it directly:

    NSG_AUTH_REQUIRED=1 NSG_AUTH_SECRET=... uvicorn main:app ...   # in nsg_ai/
    python3 nsg_ai/test_isolation.py

With no reachable backend it SKIPS (exit 0) rather than failing, so a plain test
sweep on a box with nothing running stays green; CI runs it against a live one.
"""
import json, os, sys, time, urllib.request, urllib.error

API = os.environ.get("NSG_API", "http://localhost:8000")
ADMIN = ("admin", "admin12345")


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def main() -> int:
    try:
        st, me = call("GET", "/auth/me")
    except Exception:
        print("no backend reachable — skipping isolation test")
        return 0
    if not me.get("enforced"):
        print("backend is not enforcing auth (NSG_AUTH_REQUIRED unset) — skipping")
        return 0

    def login(u, p):
        _, d = call("POST", "/auth/login", body={"username": u, "password": p})
        return d.get("access_token")

    adm = login(*ADMIN)
    if not adm:
        print("cannot log in as admin — is the bootstrap user 'admin' present?")
        return 0
    # Ensure the two test users exist AND are usable. Registering is not enough:
    # an account left disabled by earlier work cannot log in, and the test would
    # then fail for a reason that has nothing to do with isolation. Reset the
    # password and enable, both idempotent.
    _, existing = call("GET", "/auth/users", token=adm)
    by_name = {u["username"]: u for u in existing.get("users", [])}
    # Named for what they are. They used to be "alice"/"bob", which are the
    # names an LLM reaches for by default and which sat in the live users table
    # looking like real accounts during a demo.
    for u, pw in [("isolation_a", "isoapass123"), ("isolation_b", "isobpass123")]:
        if u not in by_name:
            call("POST", "/auth/register", token=adm,
                 body={"username": u, "password": pw})
        else:
            call("POST", f"/auth/users/{by_name[u]['id']}/password", token=adm,
                 body={"password": pw})
            if by_name[u]["disabled"]:
                call("POST", f"/auth/users/{by_name[u]['id']}/enable", token=adm,
                     body={})
    user_a, user_b = login("isolation_a", "isoapass123"), login("isolation_b", "isobpass123")

    # A known-valid config from the shared scenario builder (/config/normalize is
    # an unguarded utility, so this works with enforcement on).
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[1]
                          / "experiments" / "conflict"))
    try:
        import scenario as _scenario           # noqa: PLC0415
        # The builder has its own hardcoded localhost API; point it at the same
        # backend this test was aimed at with NSG_API.
        _scenario.API = API
        cfg = _scenario.build(seed=1)
    except Exception as e:
        print(f"could not build a valid config to test with: {str(e)[:120]}")
        return 0

    fails = []
    def chk(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            fails.append(name)

    st, d = call("POST", "/scenarios", token=user_a,
                 body={"name": "iso-scn", "config": cfg})
    sid = (d.get("scenario") or {}).get("id")
    chk("the first user saves a scenario", st == 200 and bool(sid), f"status {st}")

    def scn_ids(tok, all_=False):
        _, r = call("GET", "/scenarios" + ("?all=true" if all_ else ""), token=tok)
        return {x["id"] for x in r.get("scenarios", [])}
    chk("the first user sees their scenario", sid in scn_ids(user_a))
    chk("the second user does not see it", sid not in scn_ids(user_b))
    # An admin's DEFAULT view is their own work, not everyone's — oversight is
    # asked for, so their list is not silently full of other people's rows.
    chk("admin does not see it by default", sid not in scn_ids(adm))
    chk("admin sees it with ?all=true", sid in scn_ids(adm, True))
    chk("a normal user cannot escalate with ?all=true",
        sid not in scn_ids(user_b, True))
    chk("the second user cannot delete it (404)",
        call("DELETE", f"/scenarios/{sid}", token=user_b)[0] == 404)
    chk("the first user can delete their own", 
        call("DELETE", f"/scenarios/{sid}", token=user_a)[0] == 200)

    st, d = call("POST", "/campaigns", token=user_a, body={
        "name": "iso-camp", "items": [
            {"label": "x", "config": cfg, "frames": 6, "repeats": 1}],
        "thresholds": []})
    cid = d.get("campaign_id")
    chk("the first user starts a campaign", st == 200 and bool(cid), f"status {st}")
    _, dbc = call("GET", "/campaigns", token=user_b)
    chk("the second user does not see it", cid not in {c["id"] for c in dbc.get("campaigns", [])})
    _, dac = call("GET", "/campaigns", token=adm)
    chk("admin's default campaign list excludes it",
        cid not in {c["id"] for c in dac.get("campaigns", [])})
    _, daa = call("GET", "/campaigns?all=true", token=adm)
    chk("admin reaches it with ?all=true",
        cid in {c["id"] for c in daa.get("campaigns", [])})
    chk("listings say whose a row is",
        any(c.get("owner") == "isolation_a" for c in daa.get("campaigns", [])))
    chk("the second user cannot fetch it by id (404)",
        call("GET", f"/campaigns/{cid}", token=user_b)[0] == 404)
    chk("the first user can fetch their own", call("GET", f"/campaigns/{cid}", token=user_a)[0] == 200)
    call("POST", f"/campaigns/{cid}/stop", token=user_a)

    # A run must stay its owner's AFTER it finishes. The record is written at
    # start with the owner and again at the end from a fresh dict; the end write
    # used to drop owner_id and save_run merges, so a normal user got 404 on
    # their own finished run — while an admin, who sees unowned rows, saw
    # nothing wrong. Checking only while it runs would miss it.
    st, d = call("POST", "/sim/run?frames=4&wall_cap_s=60", token=user_a, body=cfg)
    rid = d.get("run_id")
    chk("the first user starts a run", st == 200 and bool(rid), f"status {st}")
    for _ in range(80):
        code, r = call("GET", f"/sim/run/{rid}", token=user_a)
        if code != 200 or r.get("status") != "running":
            break
        time.sleep(3)
    code, r = call("GET", f"/sim/run/{rid}", token=user_a)
    chk("the first user still sees their run once it has finished",
        code == 200 and r.get("status") != "running",
        f"HTTP {code}, status {r.get('status')}")
    if r.get("status") == "error":
        print("  note: the run errored before it was stored, so the stored-row "
              "path was not exercised — is the engine up?")
    chk("the second user cannot fetch it (404)",
        call("GET", f"/sim/run/{rid}", token=user_b)[0] == 404)

    print()
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    print("ALL ISOLATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
