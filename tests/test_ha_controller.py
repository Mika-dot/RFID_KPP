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
