# The one that mattered: runtime TX power never reached the radio

Found while verifying anomaly injection (roadmap item 4). It is written up on
its own because it is not about anomalies — it silently hollowed out three
shipped features.

## What was wrong

```cpp
void MmWaveEnbPhy::SetTxPower(double pow) { m_txPower = pow; }
```

The transmit power spectral density is built **once**, in
`MmWaveEnbPhy::DoInitialize`, and handed to the spectrum PHY:

```
DoInitialize -> SetSubChannels(m_channelChunks)
                  -> CreateTxPowerSpectralDensity()   // reads m_txPower
                  -> m_downlinkSpectrumPhy->SetTxPowerSpectralDensity(txPsd)
```

The spectrum PHY keeps using that stored PSD for every transmission. Nothing
rebuilds it. So `SetTxPower` after initialisation writes a number that only
`GetTxPower` ever reads back — the cell goes on radiating at whatever power it
was built with.

## How it surfaced

An anomaly test switched a cell off mid-run. The engine log showed the fault
firing:

```
[NSG-FAULT] t=0.05s cell=3 degraded to 0 dBm
```

and the results came back **byte-identical** to the healthy run — same UE count,
same PRB, and the per-UE minimum identical to the decimal (1204.0). That exact
signature — not "similar", identical — is what a lever that never reaches the
engine looks like, and it is the third time this project has seen it (the
others were FFR and BuildingsPropagationLossModel, both real ns-3 attributes on
models this engine never instantiates).

## What it affected

Every caller that sets a cell's power **after** the device is installed. In this
scenario the devices are installed at line 1710/1724 — `InstallSingleEnbDevice`
calls `device->Initialize()`, which builds the PSD — and the following all run
later:

| feature | what it did | what it actually did |
|---|---|---|
| anomaly injection (item 4) | drop a cell's power mid-run | nothing to the radio |
| RU sleep controller (Phase 2a) | `SetTxPower(0)` on an idle cell | nothing to the radio |
| **per-gNB power / cell-off (Level 3)** | per-cell startup TX power, 0 = off | **nothing to the radio** |

The Level-3 row is the serious one. It is the lever an energy-saving xApp
drives, and the whole "AI turns cells off and saves power" story rides on it.
Power still fell, because the energy model is driven separately from the PHY —
so switching a cell off looked **free**: the saving appeared and the coverage
cost did not.

That does not mean earlier energy numbers were fabricated. It means the
trade-off they were measuring only had one side of it.

## The fix

```cpp
void
MmWaveEnbPhy::SetTxPower(double pow)
{
    m_txPower = pow;
    if (m_downlinkSpectrumPhy && !m_channelChunks.empty())
    {
        SetSubChannels(m_channelChunks);   // rebuild + push the PSD
    }
}
```

Guarded on the spectrum PHY because this is also the attribute accessor, called
during construction before there is anything to push a PSD into.

## Measured after the fix

3 cells, 9 UEs, 50 MHz, `sim_time=20`. Total DL across reporting UEs.

| | total | lowest UE |
|---|---|---|
| healthy | 107.6 Mbps | 7027 kbps |
| middle cell switched off mid-run (fault) | 94.3 Mbps (−12.4%) | 1299 kbps |
| middle cell off from the start (`gnb_powers`) | 84.8 Mbps (−22.7%) | 5802 kbps |

Before the fix the first two rows were the same numbers.

## What to re-check because of this

* **Phase 2a sleep — re-run, and it holds.** Light −12.5%, deep −15.9% power,
  throughput unchanged. It holds because `test_sleep.py` only ever sleeps cells
  with **no UEs**, so silencing them really is free. What changed is that this
  is now a finding: before the fix those numbers would have come out the same
  whether the lever worked or not. Do not generalise it to sleeping a LOADED
  cell — that case has still never been measured.
* **Any Compare-leaderboard result where an xApp turned cells off or down.**
  The energy saving stands; the throughput/coverage column does not.
