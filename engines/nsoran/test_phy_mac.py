"""PHY / MAC on the mmWave DATA PATH — noise figures, UE power, CQI, SINR
estimation, MCS pinning, scheduler direction, random access, error model.

Deliberately the MmWave* classes, not their Lte* namesakes. In this engine every
UE is dual-connected and ALL user traffic rides the mmWave gNBs, so LteUePhy /
LteEnbPhy knobs would only touch the idle LTE anchor. That same test is why
there is no ICIC/FFR here at all: mmwave-helper.cc disables FFR on the mmWave
path outright ("not used in mmwave"), leaving it live only on the anchor cell
that carries no data — it would have been a whole feature's worth of dead lever.

Everything below is plain ns-3 attribute pass-through, so ns-3 rejecting a value
aborts the run before it starts: a healthy run IS the check that the value was
accepted. Where an effect should also be visible, it is asserted.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0,25.0;2000.0,1700.0,25.0;2500.0,1700.0,25.0"
UP = ";".join(f"{x},{y},1.5" for x, y in
              [(1500, 1350), (1600, 1300), (1900, 1330),
               (2100, 1300), (2400, 1350), (2500, 1300)])
SL = ";".join(["9,50"] * 6)


def run(label, **over):
    body = {"sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
            "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
            "ue_slices": SL, "shadowing": "1"}
    body.update(over)
    req = urllib.request.Request("http://localhost:8090/run",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    m = r.get("measurements", {})

    def mean(k):
        v = m.get(k, {}).get("mean")
        return float(v) if v is not None else None

    thp, prb, ul = mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl"), mean("DRB.PdcpSduBitRateUl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE  "
          f"PRB={prb if prb is None else round(prb, 2)}  "
          f"UL={ul if ul is None else round(ul, 1)} kbps")
    return {"status": r.get("status"), "thp": thp, "prb": prb, "ul": ul}


print("mmWave data-path PHY / MAC\n")
base = run("A  defaults")
nf = run("B  UE noise figure 5 -> 13 dB", ue_nf_db="13")
mcs = run("C  DL MCS pinned to 4 (AMC removed)", fixed_mcs_dl="4")
mcs_hi = run("D  DL MCS pinned to 20", fixed_mcs_dl="20")
noerr = run("E  data error model OFF", data_err_model="0")
dlonly = run("F  scheduler DL-only", sched_dl_only="1")
misc = run("G  CQI 40 ms, SINR est 800 us, n310=6, RACH 32/20/5, 4 sym/slot",
           cqi_period_ms="40", cqi_timer_ms="300", sinr_period_us="800",
           ue_sinr_period_us="12.8", ue_n310="6", ra_preambles="32",
           ra_trans_max="20", ra_resp_window="5", sym_per_slot="4",
           ue_tx_dbm="23", gnb_nf_db="7", ue_outage_db="-8")

print()
shipped = (base, nf, mcs, mcs_hi, dlonly, misc)
ok_status = all(r["status"] == "ok" for r in shipped)
bad = [i for i, r in enumerate(shipped) if r["status"] != "ok"]


def rel(a, b, key="thp"):
    if None in (a[key], b[key]) or not a[key]:
        return None
    return (b[key] - a[key]) / a[key]


# A worse receiver is asserted on PRB, not throughput. The UEs are offered more
# than the link delivers, so the scheduler answers a degraded link by spending
# MORE RESOURCES for the same delivered bits — throughput barely moves (-1.5%
# measured) while PRB jumps. Measuring throughput here reads "no effect" on a
# lever that is plainly working; the cost just shows up in a different column.
nf_prb = rel(base, nf, "prb")
mcs_d = rel(mcs, mcs_hi)
ok_nf = nf_prb is not None and nf_prb > 0.15
ok_mcs = mcs_d is not None and mcs_d > 0.20
ok_dl = (dlonly["ul"] is not None and base["ul"] is not None
         and dlonly["ul"] < base["ul"] * 0.5)
# Upstream defect, asserted so a fix surfaces instead of going unnoticed:
# disabling the data error model null-derefs at t=0.1 s, which is why there is
# no schema field for it.
ok_err_broken = noerr["status"] != "ok"

print(f"  [{'PASS' if ok_status else 'FAIL'}] every SHIPPED variant runs healthy"
      + (f" - bad: {bad}" if bad else "")
      + "  (a rejected attribute aborts ns-3 before it starts)")
print(f"  [{'PASS' if ok_nf else 'FAIL'}] a worse UE receiver costs resources "
      f"(PRB {base['prb']:.1f} -> {nf['prb']:.1f}, {100 * nf_prb:+.1f}%; throughput "
      f"only {100 * (rel(base, nf) or 0):+.1f}% — the scheduler pays in PRB)"
      if nf_prb is not None else "  [FAIL] noise figure: NO DATA")
print(f"  [{'PASS' if ok_mcs else 'FAIL'}] pinning the MCS makes it decide throughput "
      f"(MCS 4 {mcs['thp'] / 1000:.2f} -> MCS 20 {mcs_hi['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * mcs_d:+.1f}%)" if mcs_d is not None else "  [FAIL] MCS: NO DATA")
print(f"  [{'PASS' if ok_dl else 'FAIL'}] DL-only scheduling suppresses the uplink "
      f"({base['ul']:.0f} -> {dlonly['ul']:.0f} kbps) — this MAC's real DL/UL control"
      if None not in (base["ul"], dlonly["ul"]) else "  [FAIL] DL-only: NO UL DATA")
print(f"  [{'PASS' if ok_err_broken else 'FAIL'}] disabling the data error model is "
      f"still broken upstream (status={noerr['status']}), so the schema is right "
      f"not to expose it")
ok = ok_status and ok_nf and ok_mcs and ok_dl and ok_err_broken
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
