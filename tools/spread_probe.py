"""Where does the engine's SINR spread come from? A probe, not a model.

Tier B's block error rate is the chance the channel is under the MCS the
scheduler already chose, and that needs a SPREAD. Tier B predicts a flat
5.57 dB — Rayleigh — and the engine's own per-transmission histogram
(L1M.RS-SINR.Bin*) measures 5.2 to 8.1 across the breadth cases.

This asks whether a STEERING NEIGHBOUR BEAM is the missing term. Every slot a
cell points its panel at whichever of its UEs it is serving, so the gain it
lands on someone else's UE swings by tens of dB as it rotates. Per sub-step
each cell serves one of its own round-robin, the gain on a victim follows the
3GPP pattern at the angle between them, and fading is redrawn per link.

MEASURED, AND NOT INTEGRATED:

    case          engine sd   flat 5.57   sub-sampled
    baseline            8.1        5.57          8.07
    buildings           7.3        5.57          8.50
    mobility            7.0        5.57          8.07
    slice_quota         6.0        5.57          8.07
    narrow_band         2.9        5.57          8.23
    cell_off            5.2        5.57          8.34
    mean |error|                   1.53          2.14

It lands baseline within 0.03 dB, which is strong evidence that a steering
beam is what widens the spread where cells are busy. But it produces the same
8.2 dB everywhere and the engine ranges over 5 dB, so overall it is worse
than the flat value and shipping it would repeat a mistake this project has
now made twice: keeping physics that measures worse because it ought to work.

THE DUTY CYCLE WAS TRIED AND IT IS NOT THE ANSWER. The engine's DU reports
say its cells use 0.08 to 0.25 of their symbols, so a neighbour is silent most
of the time. Feeding that back as a per-sub-step silence probability makes the
spread WORSE — mean error 2.86 dB against 2.07 always-on and 1.53 for the flat
value — because an intermittent interferer is MORE variable than a constant
one, and the engine's quiet cases are the ones with LOW variance. Buildings
went to 13.7 dB against a measured 7.3.

A FIFTH: the SPATIAL spread of SINR across a cell's UEs, which Tier B knows,
against the TEMPORAL spread of one UE, which it does not. r = +0.34 on six
cases, and the three that share a geometry have identical spatial spread
(8.91 dB) against temporal spreads of 5.98, 7.00 and 8.13 — so the spatial
figure carries no information about the difference. Geometry does not stand
in for time.

WHAT THE FIVE FAILURES BOUND. narrow_band measures 2.9 dB, and nothing about
interference can take the spread BELOW the signal's own: if the serving link
were Rayleigh the floor would be 5.57. So in that case the signal is not
Rayleigh — it is Rician, with a dominant component. Any spread model has to
get the serving link's fading distribution right FIRST, per link, and only
then worry about the interference. Applying Rician by a geometric LOS test
was tried too (the commit before this one) and failed in the other direction,
calling 36/36 links line-of-sight under buildings where the engine's spread
says otherwise.

WHERE THIS LEAVES IT. The one approach that WORKS is the one in this file:
sub-sample inside the window and let the spread emerge, which lands the
baseline within 0.03 dB. That means stepping through the window — which is
what the slow tier does and what the fast tier exists to avoid. Every other
quantity in the window model averaged cleanly into closed form; the VARIANCE
of a fast-varying channel did not, and five attempts at a closed form all
measured worse than a flat Rayleigh.

So this may be irreducibly time-domain, and that is a boundary rather than a
bug. The mechanism around it is validated — fed the engine's own per-case
spread, the block error rate lands at 0.95 — so what the fast tier is missing
is one input, not a model.

Run it with the breadth cases already measured:  python3 tools/spread_probe.py
"""
import sys, os, json, math, copy, statistics, collections, random
sys.path.insert(0,'/home/chancelor/nsg_ai_platform/nsg_ai'); sys.path.insert(0,'/home/chancelor/nsg_ai_platform/tools')
os.chdir('/home/chancelor/nsg_ai_platform')
import tier_b, tier_criterion as tc, tier_breadth as tbr, window_model as wm
from main import nsoran_params

SUBSTEPS = 64
def beam_gain_db(dtheta_deg, peak_db, n_cols=8):
    """3GPP TR 38.901 Table 7.3-1 shape applied to the array: -12*(x/hpbw)^2,
    floored at the front-to-back ratio. An 8-column panel is ~102/8 = 12.8 deg."""
    hpbw = 102.0 / max(1, n_cols)
    return peak_db - min(12.0 * (dtheta_deg / hpbw) ** 2, 25.0)

def ang(a, b):
    return math.degrees(math.atan2(b[1]-a[1], b[0]-a[0]))

def run(name, base):
    cfg = tbr.CASES[name](copy.deepcopy(base)); p = nsoran_params(cfg)
    gnbs = tier_b._parse_points(p.get("gnb_positions",""))
    ues  = tier_b._parse_points(p.get("ue_positions",""))
    pw   = [float(v) for v in (p.get("gnb_powers") or "").split(";") if v]
    while len(pw) < len(gnbs): pw.append(pw[-1] if pw else 40.0)
    fghz = float(p.get("freq_hz",3.5e9))/1e9
    noise = 10 ** (__import__("ns3_bridge")._noise_floor_dbm(float(p.get("bw_hz",50e6)))/10)
    boxes = tier_b._parse_buildings(p.get("buildings",""))
    peak = wm.array_gain_db(8,8)

    # static path loss per (cell, ue), then who serves whom
    pl = [[tier_b._rx_dbm(pw[j], g, u, fghz, (j,i), boxes)
           for j,g in enumerate(gnbs)] for i,u in enumerate(ues)]
    serve = [max(range(len(gnbs)), key=lambda j: pl[i][j]+peak) for i in range(len(ues))]
    mine = collections.defaultdict(list)
    for i,c in enumerate(serve): mine[c].append(i)

    rng = random.Random(7)
    sds = []
    for i,u in enumerate(ues):
        s_cell = serve[i]
        samples = []
        for t in range(SUBSTEPS):
            # serving link: full gain, Rayleigh fading
            fade = lambda: 10*math.log10(max(rng.expovariate(1.0), 1e-9))
            sig = 10 ** ((pl[i][s_cell] + peak + fade())/10)
            interf = 0.0
            for j,g in enumerate(gnbs):
                if j == s_cell or not mine[j]:
                    continue
                victim_of_j = mine[j][t % len(mine[j])]      # who it serves now
                d = abs(((ang(g, ues[victim_of_j]) - ang(g, u)) + 180) % 360 - 180)
                interf += 10 ** ((pl[i][j] + beam_gain_db(d, peak) + fade())/10)
            samples.append(10*math.log10(sig/(noise+interf)))
        sds.append(statistics.pstdev(samples))
    return statistics.fmean(sds)

crit=json.load(open('tools/tier_criterion.json'))["scenario"]
_n, base, _p = tc.load_scenario(crit[:20])
eng={"baseline":8.1,"buildings":7.3,"mobility":7.0,"slice_quota":6.0,"narrow_band":2.9,"cell_off":5.2}
print(f"{'case':<13}{'engine sd':>10}{'flat 5.57':>11}{'sub-sampled':>13}")
e1,e2=[],[]
for name,e in eng.items():
    b = run(name, base)
    e1.append(abs(5.57-e)); e2.append(abs(b-e))
    print(f"{name:<13}{e:>10.1f}{5.57:>11.2f}{b:>13.2f}")
print(f"\nmean |error|   flat {statistics.fmean(e1):.2f} dB   sub-sampled {statistics.fmean(e2):.2f} dB")
