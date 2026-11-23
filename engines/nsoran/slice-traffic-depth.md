# Slice and traffic depth

Roadmap item 2. Four things a scenario could not say before, and can now.

Code: `nsg_ai/schema.py` (`QOS_5QI_CATALOGUE`, `SlicePolicy`, `TrafficProtocol`,
`CallDistribution`), `nsg_ai/main.py` (`_slice_policy_spec`, `_ue_slice_specs`),
`src/lte/model/eps-bearer.{h,cc}`, `src/lte/model/lte-enb-rrc.cc`,
`src/mmwave/helper/mmwave-helper.cc`,
`src/mmwave/model/mmwave-flex-tti-mac-scheduler.{h,cc}`,
`scratch/scenario-zero-with_parallel_loging.cc`.
Test: `test_slice_depth.py` (`--engine` adds the real runs).

Note what this item is NOT: it drives no new engine *attributes* — the audit
still reads 148. The whole gain is depth inside levers we already had, which is
why "how many parameters" is the wrong question to grade a release on.

## 1. The full 5QI catalogue — 21 classes, not 9

The engine's `EpsBearer::Qci` has always carried the standardised 5G set, with
its own priority, packet delay budget and error loss rate per class. **We were
the ones throwing it away.** Our own Level-6 patch guarded the QCI at `1..9`
when rebuilding the bearer on the far side of X2, so every 5G-only class —
mission-critical PTT (65/66/67/69/70), V2X (75/79), low-latency eMBB and AR
(80), industrial automation (82-85) — silently became **9, buffered video, best
effort**. A scenario could say "discrete automation" and be scheduled as
streaming, with nothing anywhere reporting a problem.

Fixed by asking the catalogue instead of assuming LTE's range
(`NsgQciToEnum` / `NsgIsKnownQci`, in `eps-bearer` because both the mmWave
helper and `LteEnbRrc` need the same answer and lte cannot depend on mmwave).

An unknown 5QI is now **rejected by the schema**, not coerced. The engine still
falls back to 9 because its QoS table ends in `NS_FATAL_ERROR` and something has
to catch it — but that fallback is exactly the mechanism described above, so the
platform refuses first. 5QI is categorical: a value outside the set is not "too
big", it is meaningless.

## 2. Slice policy: min and dedicated, not just a ceiling

Before this, the only ratio the platform could express was a ceiling, and only
over the **live E2 control path** — a saved scenario could not carry a slice
policy at all.

| ratio | meaning | work-conserving |
|---|---|---|
| `max_pct` | never more than this, even in an idle cell | — |
| `min_pct` | guaranteed under contention, but only as much as the slice asks for | yes |
| `dedicated_pct` | held whether the slice uses it or not, nobody else may have it | **no** |

`dedicated` overlaps `min` rather than stacking: 20% dedicated plus a 30% floor
guarantees 30%, not 50%.

All three are enforced the same way — by **trimming what the other slices ask
for**, before the fair division. This scheduler fills flows round-robin from a
shared symbol budget until each deficit is met, so a UE whose request was cut
drops out of the later rounds and leaves the rest to whoever still wants
symbols. Trimming rather than pre-assigning is also what keeps this out of the
allocator itself.

**Known limit, stated rather than papered over:** a guarantee holds against
slices that have none, not between two guaranteed slices. The pass splits UEs
into protected and unprotected and trims only the second group, so two slices
that both ask for a floor simply compete. Mutual floors need a
proportional-reservation allocator — a bigger change than this one.

## 3. Call distribution family

The call model had a mean and one shape. It now has four, all preserving the
mean, so switching changes the **burstiness** and not the offered load:
exponential (memoryless, the classic Poisson process), constant (a metronome —
the repeatable control case), uniform over `[0, 2·mean]`, and pareto with shape
1.5 (heavy-tailed: rare very long bursts, which is what web and video actually
look like and what pushes peak load far above the average).

## 4. TCP as well as UDP

UDP is open loop — the offered rate arrives at the radio whatever the radio
does, which is the right choice when the radio itself is the subject. TCP is
closed loop: congestion control reacts to the losses and delay the RAN produces,
so the same scenario yields a different and less steady throughput. TCP needs
its own socket factory *and* its own sink ports (1236/1237): a `PacketSink` is
bound to one factory, so a TCP stream aimed at the UDP sink's port is simply
never received.

## Two bugs this found, both in the checking rather than the feature

**The suite's first green run was fake.** The engine serialises batch runs
behind a lock and answers `status: busy` when it cannot get it, and that lock
outlives the previous HTTP response by a moment — so back-to-back runs hit it. A
busy reply carries no measurements, which read as zero throughput, and the
assertion "the numbers moved by more than 5%" happily accepted a collapse from
11.0 Mbps to 0.0 as proof that slice policy works. `run()` now retries on busy
and raises on any non-ok status, and `thp()` refuses to return zero at all: a
run that carried no traffic is a broken run, not a measurement of zero.

**A UE with two TCP services would have aborted the sim.** The DL sink is bound
per node and port, and the first version installed it per *service*. One UE
carrying voice and data over TCP — an ordinary config — would have died at t=0
with "Failed to bind socket", exactly the trap the shared uplink sink was
written to avoid. Installed once per UE now, with a test that carries two.

**The reservation was enforced by an allocation, not a reservation.** Two
separate defects, both found by running the corrected suite rather than by
reading the code:

* The pass trimmed the other slices whenever total demand exceeded the
  subframe, *even with nothing reserved*. Capping demand at the subframe is the
  allocator's own job and it does it by fair round-robin; doing it first
  replaced that with a greedy hand-out. A `min` on a slice no UE carries — a
  guaranteed no-op — moved throughput by **+69%**.
* Trimming greedily then turned out to be *more efficient* than the fair split
  it replaced: concentrating symbols on a few UEs per subframe means bigger
  transport blocks and less per-TB overhead. Holding half a cell out of the
  pool came back as an **18% throughput increase** — the reservation cost was
  real, the concentration bonus was larger. The min/dedicated pass now scales
  every request by the same factor, changing the size of the allocation and
  not its shape. (The ceiling pass keeps its greedy hand-out: its quota can be
  a few percent of a subframe, where proportional scaling rounds everyone to
  zero.)

**The call model could not be measured at all.** A constant call process
returned exactly zero on every metric — PRB, PDCP PDUs, throughput. Not a
distribution bug: `OnOffApplication` always begins with an OFF period, the
measured KPM window is only about 0.35 s of simulated time, and the call-length
floor was 0.5 s "because shorter is below the engine's reporting resolution".
So the first burst could not fit in the window. With an exponential model some
UEs drew short enough off times to squeak in and with a deterministic one none
did, which is why the same offered load read 0 / 16.5 / 63.9 Mbps depending
only on luck. The floor is now 0.05 s and the on+off period is compressed to
0.15 s, several full cycles inside the window — after which all three
distributions carry traffic within a 1.19× spread.

## Measured on the real engine

3 cells, 8 UEs in two slices, both saturating, 50 MHz, `sim_time=20`.

Throughput is the SUM across reporting UEs, in Mbps. Baseline spread across
repeats is about 5%, so read anything under that as noise.

| what changed | total DL | vs baseline |
|---|---|---|
| baseline (5QI 82 + 5QI 9, no policy) | 92.4 | — |
| `min` 50% on a 5QI **no UE carries** | 100.8 | +9.0% |
| `dedicated` 50% on a 5QI **no UE carries** | 73.2 | **−20.8%** |
| `max` 10% ceiling on a loaded slice | 71.0 | −23.2% |
| call model: constant / exponential / pareto | 87.4 / 75.7 / 73.3 | spread 1.19× |
| TCP instead of UDP | 90.6 (udp 88.5) | +2.4% — noise |

The idle-slice pair is the one that matters: **the same 50% means opposite
things** depending on which ratio it is written in — free when it is a floor,
a fifth of the network when it is a reservation. That is the only check here
that would fail if the scheduler treated min and dedicated as synonyms. The
`min` row passed against a 10% band at 9.0%, which is close; the band is that
wide because the baseline itself moves ~5% between runs.

TCP is the honest negative. Aggregate throughput barely moves, because with
both services offering far more than the radio can carry the radio is the
bottleneck either way. Lowering the threshold until 2.4% counted would have
been inventing a result. The closed loop shows up where it should — in the
queue:

| | PDCP DL delay |
|---|---|
| UDP | 22 |
| TCP | 10 (**−52.9%**) |

Congestion control stops sending before the buffer fills. That is the
measurement that distinguishes the two protocols at this horizon, and it is
what the test asserts.

The idle-slice pair is the one that matters: **the same 50% means opposite
things** depending on which ratio it is written in, and that is the only check
here that would fail if the scheduler treated min and dedicated as synonyms.
