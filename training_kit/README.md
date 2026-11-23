# NSG RAN — Training Kit

Train an AI to control a 5G RAN, then deploy it against the hosted NSG RIC test.
Everything here runs **on your machine** — training does millions of fast local
steps and never touches the NSG servers.

## The pipeline

```
  [1] TRAIN  (this kit, local)          [2] VALIDATE  (hosted RIC test)
  ┌─────────────────────────┐          ┌──────────────────────────────┐
  │ NsgRanEnv (Gymnasium)   │          │ Download the xApp connector   │
  │  state → action → reward │  ──────► │ from the RIC Test UI, drop    │
  │ train your RL agent      │  model   │ your trained model into       │
  │ (SB3 / RLlib / …)        │          │ decide(), run it → it drives  │
  └─────────────────────────┘          │ the real test over WebSocket  │
                                        └──────────────────────────────┘
```

## Quick start

```bash
pip install -r requirements.txt
python train_example.py          # sanity rollout (random vs heuristic)
```

## Train a real agent

```python
from stable_baselines3 import PPO
from nsg_ran_env import NsgRanEnv

env = NsgRanEnv(reward_weights={"qos": 1.0, "energy": 0.4, "outage": 2.0})
model = PPO("MlpPolicy", env, verbose=1)
model.learn(total_timesteps=100_000)
model.save("nsg_policy")
```

## The contract

- **Observation** — a fixed float vector: 4 global aggregates + 4 values per cell
  (active, PRB%, UE load, power) + 3 per slice (QoS, throughput, PRB cap).
- **Action** — one `Discrete` control per step: no-op, toggle a cell (energy
  saving), hand a UE group to its next-best cell (traffic steering), or cap a
  slice's PRBs (QoS assurance).
- **Reward** — `qos·mean_QoS − energy·active_power_fraction − outage·outages`.
  Tune the weights in `NsgRanEnv(reward_weights=...)` for your objective.

Build the env for your own scenario by passing a config:
`NsgRanEnv(config=my_nsg_config_dict)`.

## Deploy

After training, load your model inside the xApp connector's `decide(kpm)` (map
the same KPM fields to control commands) and run it — it validates your agent
against the hosted RIC test, in the closed loop, live.
