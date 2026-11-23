# The audit's backlog is a pool, not a target

`param_audit.py` reports ~476 engine attributes that nothing drives. Read as a
to-do list, that number says the platform exposes a small fraction of what the
engine can do. Measured, it says almost nothing of the sort.

This is the record of the first systematic probe, so the number stops being
quoted as opportunity.

## What was probed

The two largest classes in the backlog: `MmWaveHelper` (21 undriven) and
`LteEnbNetDevice` (26). 47 attributes.

Triage first, by reading what they are:

| group | count | why it is not a product knob |
|---|---|---|
| object pointers / maps | ~14 | `LteEnbRrc`, `E2Termination`, `ComponentCarrierMap`, `LteFfrAlgorithm` — wiring between objects, not settings |
| E2 transport config | ~7 | `E2TermIp`, `E2Port`, `E2ModeLte` — infrastructure the orchestrator already sets |
| LTE-anchor namesakes | ~8 | `EnableCuCpReport`, `E2Periodicity`, `ReducedPmValues` — the mmWave equivalents are already driven; all user traffic rides mmWave |
| ID bases | ~4 | `BasicCellId`, `BasicImsi` |
| genuine candidates | **~5** | carrier aggregation, `PathlossModel`, `AnrEnabled`, LTE anchor bandwidth, anchor reporting |

Then the five candidates were run on the real engine against a **three-run
baseline range**, and each had to fall outside it on throughput, PRB or BLER.

## Result

| candidate | verdict |
|---|---|
| carrier aggregation (`UseCa` + 2 CC) | **BREAKS** — `NS_ASSERT m_componentCarrierPhyParams.size() != 0` |
| `PathlossModel` → Friis | INERT |
| `AnrEnabled` | INERT |
| LTE anchor `DlBandwidth` = 100 RB | INERT |
| LTE anchor `EnableCuCpReport` = false | INERT |

**Zero of the 47 are parameters the platform is missing.**

Carrier aggregation is the interesting one, and it is not a missing parameter —
it is a missing **feature**. The flag exists and the helper honours it, but it
expects a component-carrier PHY map this scenario never builds, so enabling it
aborts the run at startup. Adding CA means growing the scenario, not adding a
field. That distinction is the whole point of this document.

## The methodology mistake that nearly got published

The first pass of this probe took **one** baseline run and compared each
candidate against it. All four non-crashing candidates came back **MOVES**.

They were all wrong. The four candidates landed within 1% of each other while
the lone baseline sat 7 Mbps and 3 BLER points away — the baseline was the
outlier, not the candidates. What gave it away was that one of the four
"movers" was *turning off an E2 report on the LTE anchor*, which cannot
plausibly change mmWave throughput.

Had that pass been believed, four parameters would have been added to the
schema on the strength of a single unlucky sample, and each would have shipped
as a control that controls nothing — the exact failure this project keeps
paying to undo.

**Rule: a candidate is compared against a baseline RANGE, never a baseline
sample.** The probe now takes three baseline runs and requires the candidate to
fall outside the observed spread with margin.

## And a second methodology finding: consecutive runs drift upward

Chasing the outlier above turned up something worth knowing on its own. Four
identical runs, back to back, on a freshly restarted container:

| run | total | PRB |
|---|---|---|
| 1 | 95.2 Mbps | 10.33 |
| 2 | 96.6 | 10.44 |
| 3 | 97.3 | 10.56 |
| 4 | 98.4 | 10.67 |

Monotonic, on both metrics, and the earlier three-run baseline climbed the same
way. That is not noise — noise does not walk in one direction on two metrics at
once.

The likely cause is that the engine's run is **wall-clock bounded** (the xApp
window is 40 s + 15 s per gNB). As the host warms up — caches, CPU frequency,
no competing build — the process gets through more SIMULATED time inside the
same wall-clock window, so more traffic is measured. Throughput measured this
way is therefore partly a measurement of how fast the machine was.

**Consequence for every A/B in this repo:** when B always runs after A, B
carries a bias of roughly +1% per position, ~3% over four runs. For the effects
we have claimed — slice reservation −20.8%, ceiling −23.2%, outage −13.2%,
beamwidth +31.7% to +48.8% — this is irrelevant. For anything under ~5% it is
not, and two verdicts sit in that zone:

* "min on an idle slice is free, +9.0%" — the direction of the drift is the
  direction of that number, so part of the +9% may be position, not policy.
  The conclusion (a floor on an idle slice does not cost the network) is
  unaffected either way; the +9% should not be quoted as an effect.
* "a fault that recovers costs less than one that does not, +5.7%" — same
  caveat. The ordering is what matters and the ordering is what is asserted.

The defence, applied here and worth applying to new comparisons: judge against
a baseline **range** rather than a sample, and prefer effects large enough that
a few percent of drift cannot explain them.

## How to read the backlog from now on

The remaining ~470 are dominated by the same categories: FFR (56 attributes
across four classes this scenario never instantiates), `ThreeGppHttpVariables`
(19, no HTTP traffic model in the scene), `GaussMarkovMobilityModel` (11, not
the mobility model we install), and more object pointers.

So the honest framing is not "we expose 159 of 594". It is: **the engine
registers 594 attributes; a scenario this shape can consume a few hundred of
them at most, and we drive 159 of those that matter.** Anything added from here
earns its place by moving a measured run, or it does not go in.
