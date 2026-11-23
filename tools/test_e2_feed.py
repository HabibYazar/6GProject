#!/usr/bin/env python3
"""The seam between Tier B and a real E2AP agent, checked from both sides.

Phase 1 proved FlexRIC's agent API runs with no ns-3 under it: a non-ns-3
agent registered with our nearRT-RIC and our production xApp read seven KPM
counters over real E2AP. What it fed them was rand()%1024. Phase 5 replaces
that, and this is the contract in the middle — a flat file Tier B rewrites
atomically once a window and the agent's C reader parses.

It builds the C reader and runs it against a feed Python wrote, because a
format agreed in two languages is a format that drifts in one of them.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "nsg_ai"))

import e2_feed

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(name)


FRAME = {"ues": [
    {"imsi": 1, "prb": 14.0, "thp_kbps": 22949.6},
    {"imsi": 2, "prb": 9.0, "thp_kbps": 100.0},
    {"imsi": 7, "prb": 0.0, "thp_kbps": 0.0},
]}

print("the Python side")
tmpdir = tempfile.mkdtemp(prefix="nsg_e2feed_")
path = os.path.join(tmpdir, "kpm.txt")
n = e2_feed.write_feed(FRAME, path, window_s=1.0)
check("a frame becomes per-UE records", n == 9, f"{n} records for 3 UEs")
back = e2_feed.read_feed(path)
check("it round-trips", back.get(("RRU.PrbTotDl", 1)) == 14.0
      and back.get(("DRB.UEThpDl", 2)) == 100.0)
check("a UE with nothing still appears",
      ("RRU.PrbTotDl", 7) in back and back[("RRU.PrbTotDl", 7)] == 0.0,
      "a missing UE and an idle one are different, and the agent cannot tell "
      "them apart if one is simply absent")
check("the uplink counters are NOT published",
      not any(k[0] in e2_feed.UNSUPPORTED for k in back),
      "Tier B has no uplink model; publishing zeros would read as a measurement")
check("the write is atomic",
      not any(f.startswith(".nsg_kpm_") for f in os.listdir(tmpdir)),
      "no temporary left behind, so the agent never sees a half-written file")

print("\nthe control channel, the other direction")
cpath = os.path.join(tmpdir, "ctrl.txt")
open(cpath, "w").write("qos_flow 9 0\nqos_flow 3 1\n")
got = e2_feed.drain_controls(cpath)
check("controls come back parsed", got == [("qos_flow", ["9", "0"]),
                                           ("qos_flow", ["3", "1"])])
check("and the file is emptied, not deleted",
      os.path.exists(cpath) and os.path.getsize(cpath) == 0,
      "the agent holds the path open per append; a file removed underneath "
      "it would send the next control to an unlinked inode")
check("a second drain returns nothing", e2_feed.drain_controls(cpath) == [])
check("a missing control file is not an error",
      e2_feed.drain_controls(os.path.join(tmpdir, "nope")) == [])

print("\nthe C side, built and run against that file")
src = os.path.join(ROOT, "engines", "flexric")
prog = os.path.join(tmpdir, "reader")
drv = os.path.join(tmpdir, "drv.c")
with open(drv, "w") as fh:
    fh.write('''
#include "nsg_feed.h"
#include <stdio.h>
int main(int argc, char** argv) {
  nsg_feed_set_path(argv[1]);
  printf("n=%d\\n", nsg_feed_size());
  double v;
  if (nsg_feed_get("RRU.PrbTotDl", 1, &v)) printf("prb1=%g\\n", v);
  if (nsg_feed_get("DRB.UEThpDl", 2, &v)) printf("thp2=%g\\n", v);
  printf("absent=%d\\n", nsg_feed_get("NoSuchKey", 1, &v) ? 1 : 0);
  printf("ues=%d\\n", nsg_feed_ue_count());
  printf("ue0=%llu\\n", (unsigned long long)nsg_feed_ue_at(0));
  printf("ue2=%llu\\n", (unsigned long long)nsg_feed_ue_at(2));
  printf("ueoob=%llu\\n", (unsigned long long)nsg_feed_ue_at(99));
  nsg_ctrl_set_path(argv[2]);
  printf("ctrl1=%d\\n", nsg_ctrl_append("qos_flow 9 0") ? 1 : 0);
  printf("ctrl2=%d\\n", nsg_ctrl_append("qos_flow 3 1") ? 1 : 0);
  printf("ctrlnull=%d\\n", nsg_ctrl_append(0) ? 1 : 0);
  nsg_feed_set_path("/tmp/nsg-definitely-absent");
  printf("gone=%d\\n", nsg_feed_size());
  return 0;
}
''')
cc = subprocess.run(["gcc", "-I", src, "-o", prog, drv,
                     os.path.join(src, "nsg_feed.c"), "-lpthread"],
                    capture_output=True, text=True)
check("the reader compiles", cc.returncode == 0, cc.stderr.strip()[:160])
if cc.returncode == 0:
    cout = os.path.join(tmpdir, "from_c.txt")
    out = subprocess.run([prog, path, cout], capture_output=True, text=True).stdout
    got = dict(l.split("=") for l in out.strip().splitlines() if "=" in l)
    check("C sees every record Python wrote", got.get("n") == str(n),
          f"C {got.get('n')} vs Python {n}")
    check("C reads the same values Python wrote",
          got.get("prb1") == "14" and got.get("thp2") == "100",
          f"prb1={got.get('prb1')} thp2={got.get('thp2')}")
    check("an unknown key is refused, not invented", got.get("absent") == "0")
    check("the agent can enumerate the feed's UEs",
          got.get("ues") == "3" and got.get("ue0") == "1" and got.get("ue2") == "7",
          "ascending and distinct — the agent's UE list has to come from here "
          "because sm_kpm.c gives every UE the same amf_ue_ngap_id")
    check("an index past the end is 0, not memory",
          got.get("ueoob") == "0")
    check("C appends controls and refuses a null",
          got.get("ctrl1") == "1" and got.get("ctrl2") == "1"
          and got.get("ctrlnull") == "0")
    check("Python reads back what C wrote",
          e2_feed.drain_controls(cout) == [("qos_flow", ["9", "0"]),
                                           ("qos_flow", ["3", "1"])],
          "the loop closes in both languages, not just one")
    check("a missing feed reads as empty, not as stale",
          got.get("gone") == "0",
          "the agent falls back to its own data, so the upstream example "
          "still runs with nothing installed")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    raise SystemExit(1)
print("all passed")
