import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from deploy.ha import switch_fence as fence


class SwitchFenceTests(unittest.TestCase):
    def config(self):
        # Fictional test addresses, never queried.
        return dict(version=1,dedicated_reader_paths_confirmed=True,
            snmpget=str(Path.cwd()/"snmpget"),snmpset=str(Path.cwd()/"snmpset"),credentials_dir=str(Path.cwd()/"private"),
            nodes={name:dict(switch_ip="192.0.2.1",if_index=i+1,exclusive=True)
                   for i,name in enumerate(("physical","perimetr","comparator"))})

    def runner(self, values):
        return Mock(side_effect=[subprocess.CompletedProcess([],0,str(v).encode(),b"") for v in values])

    def store(self, owner="perimetr",epoch=9):
        return Mock(lease=Mock(return_value=dict(owner=owner,epoch=epoch,valid=True,enabled=True)))

    def test_fence_requires_admin_and_oper_readback_and_unchanged_authority(self):
        cfg=self.config();run=self.runner([2,2,2]);store=self.store()
        receipt=fence.operate(cfg,"physical",9,False,store,run)
        self.assertEqual(dict(node="physical",isolated=True,epoch=9),receipt)
        self.assertEqual(2,store.lease.call_count)
        self.assertEqual(3,run.call_count)
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(cfg["credentials_dir"],call.kwargs["env"]["SNMPCONFPATH"])
            self.assertNotIn("-A",call.args[0]);self.assertNotIn("-X",call.args[0])

    def test_unfence_requires_target_ownership_and_link_up(self):
        receipt=fence.operate(self.config(),"physical",9,True,self.store(owner="physical"),self.runner([1,1,1]))
        self.assertEqual(dict(node="physical",reader_access=True,epoch=9),receipt)
        run=self.runner([1,1,2])
        with self.assertRaisesRegex(RuntimeError,"Readback"):
            fence.operate(self.config(),"physical",9,True,self.store(owner="physical"),run)

    def test_no_command_for_live_owner_wrong_epoch_disabled_or_shared_port(self):
        run=Mock()
        for store in (self.store(owner="physical"),self.store(epoch=10),Mock(lease=Mock(return_value=dict(enabled=False)))):
            with self.assertRaises(RuntimeError):fence.operate(self.config(),"physical",9,False,store,run)
        cfg=self.config();cfg["nodes"]["comparator"]=copy.deepcopy(cfg["nodes"]["physical"])
        with self.assertRaisesRegex(ValueError,"SharedInterface"):
            fence.operate(cfg,"physical",9,False,self.store(),run)
        cfg=self.config();cfg["nodes"]["physical"]["exclusive"]=False
        with self.assertRaises(ValueError):fence.validate(cfg)
        run.assert_not_called()

    def test_no_receipt_if_readback_or_post_operation_epoch_changes(self):
        with self.assertRaisesRegex(RuntimeError,"Readback"):
            fence.operate(self.config(),"physical",9,False,self.store(),self.runner([2,2,1]))
        store=self.store();store.lease.side_effect=[dict(owner="perimetr",epoch=9,valid=True,enabled=True),dict(owner="perimetr",epoch=10,valid=True,enabled=True)]
        with self.assertRaisesRegex(RuntimeError,"AuthorityChanged"):
            fence.operate(self.config(),"physical",9,False,store,self.runner([2,2,2]))

    def test_private_authpriv_credentials_required(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder);path.chmod(0o700);cfg=self.config();cfg["credentials_dir"]=folder
            file=path/"snmp.conf";file.write_text("defSecurityLevel authPriv\n",encoding="utf-8");file.chmod(0o600)
            fence.credentials(cfg)
            file.write_text("defSecurityLevel authNoPriv\n",encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError,"AuthPriv"):
                fence.credentials(cfg)
