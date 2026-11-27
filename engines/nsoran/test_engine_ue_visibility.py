import unittest
from unittest.mock import patch

import engine_service as svc


def scenario_log():
    rows = [
        "Total energy consumption for mmWave cell {}: 42J at time 2.0".format(c)
        for c in (2, 3, 4)
    ]

    rows += [
        "Position of UE with IMSI {} is 150:100:1.5 at time 1.0, "
        "UE connected to Cell: {}".format(i, 3 if i == 2 else 1)
        for i in range(1, 7)
    ]

    return "\n".join(rows)


class UEVisibilityTests(unittest.TestCase):

    def test_six_ues_five_lte_one_mmwave(self):
        result = svc._ue_visibility(scenario_log(), {"n_ue": 6}, 1)

        self.assertEqual(result["requested_ues"], 6)
        self.assertEqual(result["observed_ues"], 6)
        self.assertEqual(result["lte_ues"], 5)
        self.assertEqual(result["mmwave_ues"], 1)
        self.assertEqual(result["kpm_reported_ues"], 1)

    def test_latest_position_wins(self):
        scn = scenario_log() + (
            "\nPosition of UE with IMSI 2 is 100:100:1.5 "
            "at time 5.0, UE connected to Cell: 1"
            "\nPosition of UE with IMSI 2 is 100:100:1.5 "
            "at time 2.0, UE connected to Cell: 3"
        )

        result = svc._ue_visibility(scn, {"n_ue": 6}, 1)

        self.assertEqual(result["lte_ues"], 6)
        self.assertEqual(result["mmwave_ues"], 0)

    def test_missing_positions_are_unknown(self):
        result = svc._ue_visibility("", {"n_ue": 6}, 1)

        self.assertIsNone(result["observed_ues"])
        self.assertIsNone(result["lte_ues"])
        self.assertIsNone(result["mmwave_ues"])

    def test_missing_energy_data_is_unknown(self):
        scn = (
            "Position of UE with IMSI 1 is 0:0:1.5 "
            "at time 1.0, UE connected to Cell: 1"
        )

        result = svc._ue_visibility(scn, {"n_ue": 6}, 0)

        self.assertEqual(result["observed_ues"], 1)
        self.assertIsNone(result["lte_ues"])
        self.assertIsNone(result["mmwave_ues"])

    def test_default_ue_count(self):
        result = svc._ue_visibility("", {}, 0)
        self.assertEqual(result["requested_ues"], 3)

    def test_engine_response_preserves_legacy_count(self):
        scn = scenario_log()

        xapp = "\n".join([
            "KPM ind_msg latency",
            "L3servingSINR3gpp_cell_3_UEID_00002 = 2.00",
            "DRB.UEThpDl = 5848.90",
            "RRU.PrbUsedDl = 2.48",
        ])

        def fake_read(path, tail_bytes=None):
            if path == "/tmp/scn0.log":
                return scn
            if path == "/tmp/xapp.log":
                return xapp
            return ""

        with patch.object(svc, "_read", side_effect=fake_read):
            result = svc._parse_logs(
                20,
                {"n_ue": 6, "rng_run": 42},
                "[ok] E2 agents connected: 4"
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ue_count"], 1)
        self.assertEqual(result["kpm_reported_ues"], 1)
        self.assertEqual(result["requested_ues"], 6)
        self.assertEqual(result["observed_ues"], 6)
        self.assertEqual(result["lte_ues"], 5)
        self.assertEqual(result["mmwave_ues"], 1)
        self.assertEqual(result["ue_observation_scope"], "full_run_log")

    def test_live_snapshot_scope(self):
        with patch.object(svc, "_read", return_value=""):
            result = svc._parse_logs(
                20, {"n_ue": 6}, "", live=True
            )

        self.assertEqual(
            result["ue_observation_scope"],
            "recent_log_tail"
        )
        self.assertIsNone(result["observed_ues"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
