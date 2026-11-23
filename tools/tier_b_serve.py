#!/usr/bin/env python3
"""Run the fast tier as a live RAN and publish it for the E2 agent.

The spike fed the agent a single frozen window. This runs Tier B as a
session — UEs move, queues fill, serving cells change — and rewrites the feed
once per KPM window, so an xApp connected through FlexRIC sees a RAN that
evolves rather than a photograph.

    python3 tools/tier_b_serve.py --scenario "Partition test" --windows 60
    python3 tools/tier_b_serve.py --rate 8        # 8x faster than real time

It publishes to a path the agent reads (`/tmp/nsg_tier_b_kpm.txt` by
default). In the two-distro setup the agent lives in the ENGINE distro and
`/tmp` is not shared, so `--out` takes the path and something else moves the
file across; `--print-only` writes nothing and dumps the records instead.

The pacing is deliberate. Tier B computes a window in about 3.5 ms and a
window is a simulated second, so left alone it would rewrite the feed 280
times a second and an xApp sampling at 1 Hz would see one window in 280. The
default is real time; `--rate` speeds it up for a demo, and says so.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

import e2_feed
import tier_b
import tier_criterion as tc

GREEN, YELLOW, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"


WSL = "/mnt/c/Windows/System32/wsl.exe"


def _push(path: str, distro: str) -> None:
    """Copy the feed into another WSL distro, atomically on the far side."""
    import subprocess
    with open(path, "rb") as fh:
        data = fh.read()
    subprocess.run([WSL, "-d", distro, "-u", "root", "-e", "bash", "-lc",
                    f"cat > {path}.tmp && mv {path}.tmp {path}"],
                   input=data, capture_output=True, timeout=30)


def _pull_controls(path: str, distro: str) -> None:
    """Bring back what the agent wrote, and empty it over there.

    Read and truncate in ONE far-side command. Two commands would lose any
    control the agent appended between them, and a control that vanishes is
    indistinguishable to an xApp author from one their RAN ignored.
    """
    import subprocess
    r = subprocess.run(
        [WSL, "-d", distro, "-u", "root", "-e", "bash", "-lc",
         f"if [ -s {path} ]; then cat {path}; : > {path}; fi"],
        capture_output=True, timeout=30)
    if r.stdout:
        with open(path, "ab") as fh:
            fh.write(r.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default=None,
                    help="saved scenario name prefix; default is the criterion's")
    ap.add_argument("--windows", type=int, default=0, help="0 runs until interrupted")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="simulated seconds per real second (1.0 = real time)")
    ap.add_argument("--out", default=e2_feed.DEFAULT_PATH)
    ap.add_argument("--print-only", action="store_true")
    ap.add_argument("--ctrl", default=e2_feed.CTRL_PATH,
                    help="controls the agent has delivered; drained each window")
    ap.add_argument("--quota-step", type=float, default=0.5,
                    help="what an RC QoS-flow modification DOES. The stock "
                         "E2SM-RC message says which flow and which direction "
                         "and carries no magnitude, so the action is ours: by "
                         "default it multiplies that QCI's slice quota by this "
                         "and floors it at 10%%. Stated here rather than "
                         "implied, because an xApp author needs to know what "
                         "their control will do.")
    ap.add_argument("--push-distro", default=None,
                    help="also push each window into this WSL distro's copy of "
                         "--out. An artefact of the two-distro dev setup, not "
                         "of the product: the agent lives in the engine distro "
                         "and /tmp is per-distro. One subprocess per window, "
                         "which is fine at 1 Hz and would not be at 280.")
    args = ap.parse_args()

    name = args.scenario
    if name is None:
        name = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"][:20]
    _n, cfg, params = tc.load_scenario(name)
    from nsoran_live import _rate_of_imsi_map
    demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
    win = tier_b.window_s(params)

    print(f"scenario: {_n}")
    print(f"  {params['n_gnb']} cells, {params['n_ue']} UEs, window {win:g} s, "
          f"rate {args.rate:g}x")
    print(f"  publishing to {args.out}\n" if not args.print_only
          else "  printing only, nothing published\n")

    def apply_controls() -> int:
        """Act on what the xApp sent. Returns how many were applied."""
        pending = e2_feed.drain_controls(args.ctrl)
        if not pending:
            return 0
        quotas = {}
        for item in (params.get("slice_quotas") or "").split(";"):
            f = item.split(":")
            if len(f) >= 2:
                quotas[int(float(f[0]))] = [float(x) for x in f[1:]]
        for verb, argv in pending:
            if verb == "qos_flow" and argv:
                try:
                    qfi = int(float(argv[0]))
                except ValueError:
                    continue
                cur = quotas.setdefault(qfi, [100.0, 0.0, 0.0])
                cur[0] = max(10.0, cur[0] * args.quota_step)
                print(f"  {YELLOW}control{RESET} qos_flow qfi={qfi} -> "
                      f"QCI {qfi} quota {cur[0]:.0f}%")
            else:
                print(f"  {YELLOW}control{RESET} {verb} {' '.join(argv)} "
                      f"— no action defined, delivered and ignored")
        params["slice_quotas"] = ";".join(
            f"{q}:" + ":".join(f"{v}" for v in vals) for q, vals in quotas.items())
        return len(pending)

    started = time.time()
    sim_t = 0.0
    n = args.windows or None
    try:
        for i, frame in enumerate(tier_b.stream(params, n, demand_bps=demand)):
            sim_t += win
            if args.print_only:
                for rec in e2_feed.records_from_frame(frame, win)[:6]:
                    print(f"  {rec[0]:<22} imsi {rec[1]:<4} {rec[2]:,.1f}")
            else:
                e2_feed.write_feed(frame, args.out, window_s=win)
                if args.push_distro:
                    _push(args.out, args.push_distro)
                    _pull_controls(args.ctrl, args.push_distro)
            apply_controls()
            served = sum(1 for u in frame.get("ues") or []
                         if (u.get("thp_kbps") or 0) > 0)
            print(f"  t={sim_t:7.1f}s  ues={len(frame.get('ue_ids') or []):>3}  "
                  f"served={served:>3}  handovers={frame.get('handovers', 0):>3}",
                  flush=True)
            # Pace to the wall clock. Sleeping the remainder rather than a
            # fixed interval keeps the session honest when a window takes
            # longer than its share.
            due = started + (i + 1) * win / max(args.rate, 1e-6)
            slack = due - time.time()
            if slack > 0:
                time.sleep(slack)
            elif i and i % 20 == 0:
                print(f"  {YELLOW}behind the clock by {-slack:.1f}s{RESET} — "
                      f"the tier is not keeping up at {args.rate:g}x")
    except KeyboardInterrupt:
        print("\ninterrupted")
    print(f"\n{GREEN}published {int(sim_t / win)} windows, "
          f"{sim_t:.0f} simulated seconds{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
