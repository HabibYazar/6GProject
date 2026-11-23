# Phase 5 — a real E2AP agent over the fast tier

Phase 1 established the hard part with `tools/e2_spike.sh`: FlexRIC's agent
API runs with **no ns-3 under it**. A non-ns-3 agent registered with our
nearRT-RIC, our *production* `xapp_kpm_rc` reported "Connected E2 nodes = 1"
and read seven KPM counters, latency ~1.1 ms, and building `emu_agent_gnb`
left the RIC and the xApp byte-identical. E2AP, ASN.1, SCTP and subscription
handling all live in FlexRIC; a RAN supplies callbacks.

What that agent feeds the xApp is `rand()%1024`, confined to
`examples/emulator/agent/sm_kpm.c`. Phase 5 replaces it with Tier B.

## What is done and checked

- **`nsg_ai/e2_feed.py`** — Tier B publishes one flat record per
  `(measurement, UE)` per window, written to a temporary and renamed, so a
  reader never sees half a window.
- **`nsg_feed.c/h`** — the agent-side reader. Caches on mtime and size,
  mutex-guarded because the fillers run on FlexRIC's thread, and returns
  false when the feed is absent so the stock random data still works and the
  upstream example runs untouched.
- **`tools/test_e2_feed.py`**, in `verify --fast` — builds the C reader and
  runs it against a feed Python wrote. A format agreed in two languages is a
  format that drifts in one of them.

## DONE — the fast tier's numbers reached our xApp over real E2AP

Run on 2026-09-21 with `SPIKE_SECS=20`:

    E2 nodes the xApp saw : 1
    distinct KPM counters : 7
    KPM ind_msg latency   : 4415 us
    SPIKE PASS

and every value checked against the feed that produced it:

    DRB.UEThpDl    6444 of 6444 values are Tier B's (100%), 33 distinct
    RRU.PrbTotDl   6444 of 6444 values are Tier B's (100%),  5 distinct
    DRB.UEThpUl    6444 values, range 5.0-760.0 — Tier B does NOT publish
                   this one, so it is still the agent's stock rand(), which
                   is what makes the other two a match and not a coincidence

FlexRIC did the ASN.1, the SCTP and the subscription. There is no ns-3 in the
picture. `apply_nsg_feed.py` makes the change in place and is idempotent;
`sm_kpm.c.nsg-orig` and `CMakeLists.txt.nsg-orig` are the untouched originals.

## What remains

The fillers are one line each (`return fill_rnd_int_data();`) and would be
trivial to redirect. They are not the blocker.

The blocker was the UE list, and it is solved. `sm_kpm.c` set

    gnb.amf_ue_ngap_id = 112358132134;

a constant, identical for every UE — an emulator reports one identity and
varies only the numbers, so per-UE values would all have landed on one. The
agent now builds one UE per feed IMSI, keeping `fill_rnd_ue_id_data()` for
every other mandatory field and overwriting only the id.

All three follow-ups are done.

  * **A counter the fast tier cannot produce reports no value**, not rand().
    E2SM-KPM has NO_VALUE_MEAS_VALUE for it. With no feed the stock random
    behaviour is untouched, so the upstream example still runs.
  * **The RAN moves.** `tools/tier_b_serve.py` runs Tier B as a session and
    rewrites the feed once per window, paced to the wall clock. `frames()`
    and `stream()` share one implementation, so the RAN an xApp connects to
    cannot drift from the one a campaign measures.
  * **Tier B's own counters are subscribable.** TB.TotNbrDl.1,
    TB.ErrTotalNbrDl.1 and DRB.BufferSize.Qos are declared in
    fill_kpm_report_style_4 AND filled from the feed — both lists, because
    one without the other is either a name nobody can ask for or a
    subscription with nothing behind it.

Against a live session the xApp reports six counters with values, four
silent, 574 distinct throughput readings from 8,285 to 38,398 kbps, and
handovers accumulating as UEs walk.

## The loop closes: an xApp can steer it, not only read it

`sm_rc.c` already parsed an RC control and answered it; it had nowhere to put
it. It now appends one line per control, the Python side drains it each
window, and the effect appears in the next report.

Measured directly, injecting one QoS-flow modification mid-session:

    blocks per UE before the control   3556
    blocks per UE after                1778     ratio 0.50

which is the quota halving, arriving in the KPM the xApp then reads. Through
the real path, with the agent doing the parsing, the server logged
`control qos_flow qfi=10 -> QCI 10 quota 50%` during a spike run whose
control file had been emptied beforehand — so that line could only have come
from the agent.

## What a next piece of work would be

  * the feed crosses distros by a subprocess per window, which is an artefact
    of this two-distro dev setup and would not exist in one place
  * the RC loop carries one action. The stock E2SM-RC message says WHICH
    QoS flow and WHICH direction and no magnitude, so what the fast tier
    does with it is defined on the Python side (`--quota-step`) and printed
    when it happens. A richer RC style would carry the value itself.
  * the agent advertises one cell; Tier B has six

## Name mapping, which is not one-to-one

    emulator declares        Tier B produces            note
    RRU.PrbTotDl             RRU.PrbUsedDl              stock name, our value
    DRB.PdcpSduVolumeDL      QosFlow.PdcpPduVolumeDL
    DRB.UEThpDl              computed from volume       deliberately NOT the
                                                        engine's bits-over-
                                                        latency definition
    RRU.PrbTotUl             —                          no uplink model; not
    DRB.PdcpSduVolumeUL      —                          published at all, so
    DRB.UEThpUl              —                          the gap stays visible
    DRB.RlcSduDelayDl        —                          rather than becoming
                                                        a plausible zero
