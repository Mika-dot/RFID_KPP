import copy
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from gateway.observed_lease import ObservedLease
from gateway.server import Router
from guardian.sql import SqlStore


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [dict(id=name, priority=i+1, url="http://"+name+":18200", web_url="http://"+name+":5050")
                      for i,name in enumerate(("physical","perimetr","comparator"))]
        self.rows = {n["id"]:dict(node=n["id"],sample_age=1,fencing_protocol=2,active=n["id"]=="physical",
            healthy=n["id"]=="physical",epoch=7,operator_maintenance=False,
            observed_lease=dict(owner="physical",epoch=7,valid=True,enabled=True,age_sec=1,remaining_sec=12)) for n in self.nodes}
        self.calls = []

    def request(self, url, token, **kwargs):
        self.calls.append((url,token))
        node = next(n["id"] for n in self.nodes if url.startswith(n["url"]))
        row = self.rows[node]
        if isinstance(row, Exception):
            raise row
        return 200, row

    def test_routes_from_two_witnesses_without_sql_or_writes(self):
        self.rows["comparator"] = TimeoutError()
        store = ObservedLease(self.nodes,"private",self.request)
        router = Router(self.nodes,store,"private",self.request)
        self.assertEqual(("http://physical:5050",("physical",7)),router.resolve())
        self.assertTrue(router.unchanged(("physical",7)))
        self.assertTrue(all(url.endswith("/status") and token=="private" for url,token in self.calls))

    def test_disagreement_and_one_surviving_witness_never_elect(self):
        self.rows["perimetr"]["observed_lease"]["epoch"] = 8
        self.rows["comparator"] = TimeoutError()
        with self.assertRaisesRegex(RuntimeError,"QuorumUnavailable"):
            ObservedLease(self.nodes,"private",self.request).lease()
        self.rows["perimetr"] = TimeoutError()
        with self.assertRaises(RuntimeError):
            ObservedLease(self.nodes,"private",self.request).lease()

    def test_cached_valid_flag_cannot_outlive_sql_deadline(self):
        for row in self.rows.values():
            row["observed_lease"].update(age_sec=3,remaining_sec=2)
        store = ObservedLease(self.nodes,"private",self.request)
        self.assertFalse(store.lease()["valid"])
        with self.assertRaisesRegex(RuntimeError,"NoValidWebOwner"):
            Router(self.nodes,store,"private",self.request).resolve()

    def test_rejects_stale_malformed_foreign_and_missing_deadlines(self):
        original = copy.deepcopy(self.rows["physical"])
        variants = [dict(sample_age=11),dict(sample_age=True),dict(fencing_protocol=True),dict(node="foreign")]
        leases = [dict(age_sec=9),dict(age_sec=-1),dict(remaining_sec=float("nan")),dict(remaining_sec=True),
                  dict(epoch=True),dict(owner="foreign"),dict(valid=1)]
        for change in variants:
            self.rows["physical"] = dict(original,**change)
            self.assertIsNone(ObservedLease(self.nodes,"private",self.request).witness(self.nodes[0]))
        for change in leases:
            row = copy.deepcopy(original);row["observed_lease"].update(change);self.rows["physical"] = row
            self.assertIsNone(ObservedLease(self.nodes,"private",self.request).witness(self.nodes[0]))
        self.rows["physical"] = original
        original["observed_lease"].pop("remaining_sec")
        self.assertIsNone(ObservedLease(self.nodes,"private",self.request).witness(self.nodes[0]))

    def test_sql_deadline_subtracts_entire_query_duration(self):
        conn = Mock();conn.execute.return_value.fetchone.return_value = ("physical",7,1,30,1,12.0)
        @contextmanager
        def connect():
            yield conn
        store = SqlStore()
        with patch.object(store,"connect",connect),patch("time.monotonic",side_effect=[10,12]):
            lease = store.lease()
        self.assertEqual(10,lease["remaining_sec"])
        conn.commit.assert_not_called()
        self.assertIn("DATEDIFF_BIG(millisecond",conn.execute.call_args.args[0])

    def test_witness_deadline_also_subtracts_http_round_trip(self):
        self.rows["physical"]["observed_lease"].update(age_sec=1,remaining_sec=2)
        with patch("gateway.observed_lease.time.monotonic",side_effect=[10,12]):
            value=ObservedLease(self.nodes,"private",self.request).witness(self.nodes[0])
        self.assertEqual(("physical",7,False,True),value)

    def test_quorum_rechecks_expiry_and_freshness_after_slow_peer_timeout(self):
        for age,remaining in ((0,1),(7,15)):
            with self.subTest(age=age,remaining=remaining):
                now=[0]
                for node in ("physical","perimetr"):
                    self.rows[node]["observed_lease"].update(age_sec=age,remaining_sec=remaining)
                def request(url,token,**kwargs):
                    if "comparator" in url:
                        now[0]=2
                        raise TimeoutError()
                    return self.request(url,token,**kwargs)
                pool=Mock();pool.__enter__=Mock(return_value=pool);pool.__exit__=Mock(return_value=False)
                pool.map.side_effect=lambda function,nodes:[function(node) for node in nodes]
                store=ObservedLease(self.nodes,"private",request)
                with patch("gateway.observed_lease.ThreadPoolExecutor",return_value=pool),patch("gateway.observed_lease.time.monotonic",side_effect=lambda:now[0]):
                    if age:
                        with self.assertRaisesRegex(RuntimeError,"QuorumUnavailable"):store.lease()
                    else:
                        self.assertFalse(store.lease()["valid"])
