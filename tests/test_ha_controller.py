import threading
import unittest
from unittest.mock import Mock, patch

from guardian.controller import Controller


class ControllerTests(unittest.TestCase):
    def build(self):
        cfg={"node_id":"comparator", "nodes":[
            {"id":"physical","priority":1,"url":"http://physical"},
            {"id":"perimetr","priority":2,"url":"http://perimetr"},
            {"id":"comparator","priority":3,"url":"http://comparator"}]}
        store=Mock()
        store.lease.return_value={"owner":"physical","valid":True,"enabled":True,"age":200,"epoch":1}
        store.node_state.return_value={"faulted":False}
        controller=Controller(cfg,store,Mock(),threading.Event())
        return controller,store

    @patch.dict("os.environ",{"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json")
    def test_fence_before_demotion_and_new_grant(self,get):
        controller,store=self.build()
        calls=[]
        store.grant.side_effect=lambda owner: calls.append(("grant",owner))
        get.side_effect=lambda *a,**k: calls.append(("demote",None)) or (200,{})
        controller.poll=lambda node:(node["id"],{} if node["id"]=="physical" else {"prepared":True})
        controller.tick()
        self.assertEqual([("grant",None),("demote",None),("grant","perimetr")],calls)
        store.fault.assert_called_once_with("physical")

    @patch.dict("os.environ",{"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json",side_effect=TimeoutError)
    def test_unreachable_old_owner_still_fenced(self,get):
        controller,store=self.build()
        controller.poll=lambda node:(node["id"],{} if node["id"]=="physical" else {"prepared":True})
        controller.tick()
        self.assertEqual([None,"perimetr"],[c.args[0] for c in store.grant.call_args_list])

    @patch.dict("os.environ",{"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json", side_effect=TimeoutError)
    def test_hardware_target_receipt_matches_new_owner_epoch(self,get):
        controller,store=self.build()
        controller.cfg["hardware_fencing_required"] = True
        store.lease.side_effect=[
            {"owner":"physical","valid":True,"enabled":True,"age":200,"epoch":10},
            {"owner":"perimetr","valid":True,"enabled":True,"age":0,"epoch":12},
        ]
        store.pending_fences.return_value=[]
        store.claim_controller.return_value=True
        controller.poll=lambda node:(node["id"],{} if node["id"]=="physical" else {"prepared":True})
        calls=[]
        store.grant.side_effect=lambda owner,**kwargs:calls.append(("grant",owner,kwargs))
        with patch("guardian.hardware_fence.allow_target", side_effect=lambda *args:calls.append(("allow",args)) ) as allow:
            controller.tick()
        allow.assert_called_once_with(controller.cfg,"perimetr",12)
        self.assertLess(calls.index(("grant","perimetr",{})), calls.index(("allow",(controller.cfg,"perimetr",12))))

    @patch.dict("os.environ",{"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json",side_effect=TimeoutError)
    def test_hardware_unfence_failure_rolls_back_and_quarantines_target(self,get):
        controller,store=self.build()
        controller.cfg["hardware_fencing_required"] = True
        store.lease.side_effect=[
            {"owner":"physical","valid":True,"enabled":True,"age":200,"epoch":10},
            {"owner":"perimetr","valid":True,"enabled":True,"age":0,"epoch":12},
        ]
        store.pending_fences.side_effect=[[],[("perimetr",13)]]
        store.claim_controller.return_value=True
        controller.poll=lambda node:(node["id"],{} if node["id"]=="physical" else {"prepared":True})
        with patch("guardian.hardware_fence.allow_target",side_effect=RuntimeError("unfence failed")) as allow, \
             patch("guardian.hardware_fence.fence_previous",return_value={"verified":True}) as fence:
            with self.assertRaisesRegex(RuntimeError,"unfence failed"):
                controller.tick()
        allow.assert_called_once_with(controller.cfg,"perimetr",12)
        self.assertEqual([None,"perimetr",None],[c.args[0] for c in store.grant.call_args_list])
        self.assertIn("perimetr", [c.args[0] for c in store.fault.call_args_list])
        fence.assert_called_once_with(controller.cfg,"perimetr",13)

    def test_no_valid_reserve_means_no_new_owner(self):
        controller,store=self.build()
        controller.poll=lambda node:(node["id"],{})
        controller.tick()
        store.grant.assert_called_once_with(None)

    def test_disabled_schema_never_changes_leadership(self):
        controller,store=self.build()
        store.lease.return_value["enabled"]=False
        controller.poll=lambda node:(node["id"],{})
        controller.tick()
        store.grant.assert_not_called()

    def test_old_epoch_readiness_cannot_end_new_startup_grace(self):
        controller, store = self.build()
        store.lease.return_value.update(age=1, epoch=2)
        controller.poll = lambda node: (node["id"], {
            "prepared":True, "healthy":node["id"]=="physical", "active":node["id"]=="physical", "epoch":1})
        controller.tick()
        self.assertNotIn("physical", controller.policy.proven)
        store.grant.assert_called_once_with("physical")

    @patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json", side_effect=TimeoutError)
    def test_expired_failed_owner_is_quarantined_before_reserve_activation(self, get):
        controller, store = self.build()
        store.lease.return_value["valid"] = False
        controller.poll = lambda node:(node["id"], {} if node["id"]=="physical" else {"prepared":True})
        controller.tick()
        store.fault.assert_called_once_with("physical")
        self.assertEqual([None,"perimetr"], [c.args[0] for c in store.grant.call_args_list])

    @patch.dict("os.environ", {"PERIMETER_HA_TOKEN":"test"})
    @patch("guardian.controller.get_json", side_effect=TimeoutError)
    def test_expired_failed_owner_enters_repair_even_with_all_reserves_down(self, get):
        controller, store = self.build()
        store.lease.return_value["valid"] = False
        controller.poll = lambda node:(node["id"], {})
        controller.tick()
        store.fault.assert_called_once_with("physical")
        store.grant.assert_called_once_with(None)
