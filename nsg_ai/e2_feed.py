"""Tier B's numbers, in a shape a C agent can read without a JSON parser.

Phase 1 proved FlexRIC's agent API works with no ns-3 under it: a non-ns-3
agent registered with our nearRT-RIC and our PRODUCTION xApp read seven KPM
counters over real E2AP. What it fed the xApp was `rand()%1024`, confined to
`examples/emulator/agent/sm_kpm.c`. This is what replaces it.

THE SEAM IS A FILE, deliberately. The agent's fillers run inside FlexRIC's
own thread on a millisecond cadence, and the alternative — embedding Python,
or a socket with its own lifecycle and failure modes — buys nothing for a
value that changes once a KPM window. A file is atomic to rename, needs no
protocol, and the agent falls back to its stock random data when it is
missing, so the upstream example still works untouched.

FORMAT, one record per line:

    <measurement name> <imsi> <value>

Blank lines and `#` comments are ignored. A reader that does not recognise a
name skips the line: this is a superset of what any one service model asks
for, because Tier B produces counters the stock emulator's table does not
declare and dropping them here would mean editing two places to add one.

NAMES ARE NOT THE SAME ON BOTH SIDES, and the mapping is the honest part:

    the emulator declares     Tier B produces        note
    RRU.PrbTotDl              RRU.PrbUsedDl          Tot vs Used — the stock
                                                     table's name, our value
    DRB.PdcpSduVolumeDL       QosFlow.PdcpPduVolumeDL
    DRB.UEThpDl               (computed here)        volume over the window,
                                                     NOT the engine's
                                                     bits-over-latency figure
    TB.TotNbrDl.1             TB.TotNbrDl.1          under its own name; the
    TB.ErrTotalNbrDl.1        TB.ErrTotalNbrDl.1     agent's advertised set is
    DRB.BufferSize.Qos        DRB.BufferSize.Qos     extended to declare them
    RRU.PrbTotUl,             —                      Tier B has no uplink.
    DRB.PdcpSduVolumeUL,                             These are NOT published,
    DRB.UEThpUl                                      so the agent keeps its
                                                     random values and the
                                                     gap stays visible
"""
from __future__ import annotations

import os
import tempfile

DEFAULT_PATH = "/tmp/nsg_tier_b_kpm.txt"

# Tier B's name -> the name the stock emulator's table declares. Anything not
# listed is published under its own name, for a service model that asks for it.
ALIAS = {
    "RRU.PrbUsedDl": "RRU.PrbTotDl",
    "QosFlow.PdcpPduVolumeDL": "DRB.PdcpSduVolumeDL",
    "RRU.PrbUsedUl": "RRU.PrbTotUl",
    "QosFlow.PdcpPduVolumeUL": "DRB.PdcpSduVolumeUL",
}

# What Tier B cannot produce. Named so the absence is a decision rather than
# an oversight, and so a reader can tell "no uplink model" from "bug".
#
# THREE OF THESE LEFT THE LIST when the uplink shipped. They were here for a
# reason that stopped being true, which is exactly how a live path and a
# batch path drift apart: the fast tier grew a counter and the surface an
# xApp subscribes to went on saying NO_VALUE for it. Anything added to the
# model has to be added here in the same change or the product does not have
# it, whatever the tests of the model say.
UNSUPPORTED = ("DRB.RlcSduDelayDl",)


def records_from_frame(frame: dict, window_s: float = 1.0) -> list[tuple]:
    """(name, imsi, value) for one Tier B window.

    Per UE, not per cell: E2SM-KPM style 4 reports against a UE id, and a
    cell-level figure handed to a per-UE report is the kind of substitution
    that reads fine and means nothing.
    """
    out = []
    for u in frame.get("ues") or []:
        imsi = u.get("imsi")
        if imsi is None:
            continue
        prb = u.get("prb")
        if prb is not None:
            out.append((ALIAS.get("RRU.PrbUsedDl", "RRU.PrbUsedDl"), imsi, float(prb)))
        # Tier B's own counters, under their own names. The stock emulator's
        # table does not declare these; the agent's advertised set is extended
        # to match, so an xApp can subscribe to what the fast tier actually
        # measures instead of only to what an LTE emulator happened to list.
        for src, name in (("tb", "TB.TotNbrDl.1"),
                          ("tb_err", "TB.ErrTotalNbrDl.1"),
                          ("queued", "DRB.BufferSize.Qos")):
            v = u.get(src)
            if v is not None:
                out.append((name, imsi, float(v)))
        # The uplink, under the names the stock style-4 table already
        # declares, so an xApp subscribing to an ordinary NR counter set gets
        # it without knowing anything about this tier.
        if u.get("prb_ul") is not None:
            out.append((ALIAS["RRU.PrbUsedUl"], imsi, float(u["prb_ul"])))
        for src, name in (("tb_ul", "TB.TotNbrUl"),
                          ("tb_ul_err", "TB.ErrTotalNbrUl")):
            v = u.get(src)
            if v is not None:
                out.append((name, imsi, float(v)))
        ul = u.get("thp_ul_kbps")
        if ul is not None:
            out.append(("DRB.UEThpUl", imsi, float(ul)))
            out.append((ALIAS["QosFlow.PdcpPduVolumeUL"], imsi,
                        float(ul) * 1000.0 * window_s / 8.0))
        # The radio conditions behind those counters. Tier B has computed both
        # since it had a channel and published neither, so an xApp steering on
        # load could not see WHY a UE was doing badly.
        for src, name in (("rsrp_dbm", "RSRP"), ("sinr", "SINR")):
            v = u.get(src)
            if v is not None:
                out.append((name, imsi, float(v)))
        thp = u.get("thp_kbps")
        if thp is not None:
            # The volume-derived rate. The engine's DRB.UEThpDl divides by an
            # RLC delay and is not a throughput; there is no reason to
            # reproduce that here just because the name matches.
            out.append(("DRB.UEThpDl", imsi, float(thp)))
            out.append((ALIAS["QosFlow.PdcpPduVolumeDL"], imsi,
                        float(thp) * 1000.0 * window_s / 8.0))
    return out


def write_feed(frame: dict, path: str = DEFAULT_PATH,
               window_s: float = 1.0) -> int:
    """Publish one window. Atomic: the agent never sees a half-written file.

    Returns the number of records written.
    """
    recs = records_from_frame(frame, window_s)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nsg_kpm_")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("# Tier B KPM feed — name imsi value\n")
            fh.write(f"# {len(recs)} records, window {window_s:g} s\n")
            for name, imsi, value in recs:
                fh.write(f"{name} {imsi} {value:.6g}\n")
        os.replace(tmp, path)          # rename is atomic within a filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(recs)


CTRL_PATH = "/tmp/nsg_tier_b_ctrl.txt"


def drain_controls(path: str = CTRL_PATH) -> list[tuple]:
    """Every control the agent has written since the last call, then empty it.

    Read-then-truncate rather than read-then-delete: the agent holds the path
    open per append, and a file removed underneath it would send the next
    control to an unlinked inode where nothing would ever read it. Truncating
    leaves the same file in place.

    A control that arrives while this is running is either in what we read or
    in what the agent appends after the truncate. It cannot be both and it
    cannot be neither — which is the property worth having, because a control
    silently dropped looks exactly like an xApp whose decision did nothing.
    """
    out = []
    try:
        with open(path, "r+") as fh:
            for line in fh:
                parts = line.split()
                if parts:
                    out.append((parts[0], parts[1:]))
            fh.seek(0)
            fh.truncate()
    except OSError:
        return []
    return out


def read_feed(path: str = DEFAULT_PATH) -> dict:
    """Parse a feed back — the reference the C reader is checked against."""
    out: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) != 3:
                    continue
                try:
                    out[(parts[0], int(parts[1]))] = float(parts[2])
                except ValueError:
                    continue
    except OSError:
        return {}
    return out
