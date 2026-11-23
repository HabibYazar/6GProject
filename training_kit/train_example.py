"""
Train (or evaluate) an agent on the NSG RAN environment.

Runs fully on your machine. Two parts:
  1. A quick sanity rollout (random vs. a simple heuristic) so you can see the
     environment and reward working before committing to a long training run.
  2. A ready-to-uncomment Stable-Baselines3 PPO block for real RL training.

After training, export your policy and plug it into the xApp connector's
decide() to validate it against the hosted RIC test.

    pip install -r requirements.txt
    python train_example.py
"""
import numpy as np

from nsg_ran_env import NsgRanEnv


def rollout(env, policy, episodes=5):
    """Average episode return for a policy: fn(obs, env) -> action."""
    returns = []
    for _ in range(episodes):
        obs, _ = env.reset()
        total = 0.0
        done = False
        while not done:
            action = policy(obs, env)
            obs, reward, terminated, truncated, _ = env.step(action)
            total += reward
            done = terminated or truncated
        returns.append(total)
    return float(np.mean(returns))


def random_policy(obs, env):
    return env.action_space.sample()


def heuristic_policy(obs, env):
    """Domain heuristic: sleep an idle cell if one exists, else no-op.
    A trained RL agent should beat this."""
    kpm = env.sim.get_kpm_report()
    for i, spec in enumerate(env._actions):
        if spec and spec[0] == "toggle_cell":
            cell = next(c for c in kpm["cells"] if c["id"] == spec[1])
            if cell["active"] and cell["connected_ues"] == 0:
                return i
    return 0  # no-op


if __name__ == "__main__":
    env = NsgRanEnv(reward_weights={"qos": 1.0, "energy": 0.4, "outage": 2.0})
    print(f"observation dim: {env.observation_space.shape[0]}, actions: {env.action_space.n}")

    print("\n=== Sanity rollout (higher return = better) ===")
    print(f"  random policy    : {rollout(env, random_policy):+.2f}")
    print(f"  heuristic policy : {rollout(env, heuristic_policy):+.2f}")
    print("  → an RL agent should learn to beat the heuristic.")

    # ── Real RL training (uncomment; pip install stable-baselines3) ────────────
    # from stable_baselines3 import PPO
    # model = PPO("MlpPolicy", env, verbose=1)
    # model.learn(total_timesteps=100_000)
    # model.save("nsg_policy")
    # # then export to ONNX (or keep the SB3 model) and use it in the xApp connector.
