import unittest

from guardian.policy import Observation, Policy

NODES = [{"id": "physical", "priority": 1}, {"id": "perimetr", "priority": 2},
         {"id": "comparator", "priority": 3}]
READY = Observation(True, True, False, True)
STANDBY = Observation(True, False, False, True)
DOWN = Observation()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(NODES, recovery_seconds=60, startup_seconds=30)
        self.lease = {"owner": "physical", "valid": True, "age": 100}
        self.obs = {"physical": READY, "perimetr": STANDBY, "comparator": STANDBY}

    def test_initial_priority(self):
        self.assertEqual("physical", self.policy.choose({"owner": None, "valid": False}, self.obs, 0))

    def test_physical_network_failure(self):
        self.obs["physical"] = DOWN
        self.assertEqual("perimetr", self.policy.choose(self.lease, self.obs, 0))

    def test_two_failed_nodes(self):
        self.obs["physical"] = self.obs["perimetr"] = DOWN
        self.assertEqual("comparator", self.policy.choose(self.lease, self.obs, 0))

    def test_all_down_is_safe_stop(self):
        self.assertIsNone(self.policy.choose(self.lease, {}, 0))

    def test_prepared_but_no_agent_not_eligible(self):
        self.obs["physical"] = DOWN
        self.obs["perimetr"] = Observation(True, True, False, False)
        self.assertEqual("comparator", self.policy.choose(self.lease, self.obs, 0))

    def test_faulted_node_cannot_promote(self):
        self.obs["physical"] = Observation(True, True, True, True)
        self.assertEqual("perimetr", self.policy.choose(self.lease, self.obs, 0))

    def test_startup_grace_does_not_hide_later_degradation(self):
        lease = dict(self.lease, age=1)
        self.assertEqual("physical", self.policy.choose(lease, self.obs, 0))
        self.obs["physical"] = STANDBY
        self.assertEqual("perimetr", self.policy.choose(lease, self.obs, 1))

    def test_initial_startup_wait(self):
        self.obs["physical"] = STANDBY
        self.assertEqual("physical", self.policy.choose(dict(self.lease, age=1), self.obs, 0))
        self.assertEqual("perimetr", self.policy.choose(dict(self.lease, age=31), self.obs, 31))

    def test_failback_requires_continuous_stability(self):
        lease = dict(self.lease, owner="perimetr")
        self.obs["perimetr"] = READY
        self.assertEqual("perimetr", self.policy.choose(lease, self.obs, 0))
        self.assertEqual("physical", self.policy.choose(lease, self.obs, 61))

    def test_recovery_flap_resets_stability(self):
        lease = dict(self.lease, owner="perimetr")
        self.obs["perimetr"] = READY
        self.policy.choose(lease, self.obs, 0)
        self.obs["physical"] = DOWN
        self.policy.choose(lease, self.obs, 30)
        self.obs["physical"] = READY
        self.assertEqual("perimetr", self.policy.choose(lease, self.obs, 60))
        self.assertEqual("perimetr", self.policy.choose(lease, self.obs, 90))
        self.assertEqual("physical", self.policy.choose(lease, self.obs, 121))

    def test_expired_owner_is_not_trusted(self):
        self.obs["physical"] = DOWN
        self.assertEqual("perimetr", self.policy.choose(dict(self.lease, valid=False), self.obs, 0))

    def test_quiet_rfid_is_not_a_failure_signal(self):
        # Election accepts readiness, never an RFID read count.
        self.assertEqual("physical", self.policy.choose(self.lease, self.obs, 0))

    def test_new_epoch_has_its_own_startup_grace(self):
        self.policy.choose(dict(self.lease, epoch=1), self.obs, 0)
        self.obs["physical"] = STANDBY
        self.assertEqual("physical", self.policy.choose(dict(self.lease, age=1, epoch=2), self.obs, 1))
        self.assertEqual("perimetr", self.policy.choose(dict(self.lease, age=31, epoch=2), self.obs, 31))
