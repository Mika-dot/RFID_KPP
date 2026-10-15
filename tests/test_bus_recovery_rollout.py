"""Recovery phases preserve ownership and require observed live acceptance."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy.ha import recover_bus_rollout as rollout


class RecoveryRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg={'node_id':'comparator','state_dir':self.tmp.name,
                  'nodes':[{'id':key,'url':'http://'+key} for key in ('physical','perimetr','comparator')]}
        self.args=rollout.options(SimpleNamespace(release='b'*40))
        self.env={'PERIMETER_HA_TOKEN':'test-token'}
        self.rows={key:dict(node=key,sample_age=1,fencing_protocol=2,release_sha='b'*40,
            epoch=15,prepared=True,faulted=False,operator_maintenance=False,
            active=key=='physical',healthy=key=='physical',
            update={'operator_pinned':True,'pending':key!='physical'},
            services={str(i):{'ok':True} for i in range(5)},
            bus_sample_age=1,bus_metrics={'rfid_pending':0,'rfid_sent':50,'video_pending':0,'video_sent':0},
            metadata_mirror={'enabled':True,'error':None,'blocked_streams':[]})
            for key in ('physical','perimetr','comparator')}

    def test_acceptance_requires_all_pins_live_workers_fresh_metrics_and_completed_owner_trial(self):
        self.assertEqual((True,'physical',True),rollout.acceptance(self.rows,self.args.release))
        for case in ('pin','version','fault','worker','metrics','trial','maintenance','two_owners'):
            rows=copy.deepcopy(self.rows)
            if case=='pin':rows['comparator']['update']['operator_pinned']=False
            if case=='version':rows['perimetr']['release_sha']='a'*40
            if case=='fault':rows['comparator']['faulted']=True
            if case=='worker':rows['physical']['services']['0']['ok']=False
            if case=='metrics':rows['physical']['bus_sample_age']=16
            if case=='trial':rows['physical']['update']['pending']=True
            if case=='maintenance':rows['comparator']['operator_maintenance']=True
            if case=='two_owners':rows['comparator']['active']=True
            with self.subTest(case=case):
                self.assertFalse(rollout.acceptance(rows,self.args.release)[0])

    def test_mirror_error_is_explicitly_separate_from_core_ha_acceptance(self):
        self.rows['comparator']['metadata_mirror']['error']='MirrorReadBlocked'
        self.assertEqual((True,'physical',False),rollout.acceptance(self.rows,self.args.release))

    def test_hold_refuses_active_node_before_maintenance_preparation_or_install(self):
        with patch.object(rollout.update,'inventory',return_value=(self.rows,'comparator')), \
                patch.object(rollout,'maintenance') as maintenance, \
                patch.object(rollout.update,'prepare') as prepare, \
                patch.object(rollout.update,'apply') as apply:
            with self.assertRaisesRegex(RuntimeError,'LocalNodeIsActive'):
                rollout.stage_held(self.args,self.cfg,self.env)
        maintenance.assert_not_called(); prepare.assert_not_called(); apply.assert_not_called()

    def test_preparation_failure_unholds_local_node_without_installing(self):
        with patch.object(rollout.update,'inventory',return_value=(self.rows,'physical')), \
                patch.object(rollout,'maintenance') as maintenance, \
                patch.object(rollout.update,'prepare',side_effect=RuntimeError('qualification failed')), \
                patch.object(rollout.update,'apply') as apply, patch.object(rollout,'emit'):
            with self.assertRaisesRegex(RuntimeError,'qualification failed'):
                rollout.stage_held(self.args,self.cfg,self.env)
        self.assertEqual([True,False],[call.args[2] for call in maintenance.call_args_list])
        apply.assert_not_called()

    def test_unfinished_install_journal_prevents_unholding_uncertain_runtime(self):
        journal=Path(self.tmp.name)/'operator-release-install.json'
        def failed(*args):
            journal.write_text('private recovery journal')
            raise RuntimeError('restore failed')
        with patch.object(rollout.update,'inventory',return_value=(self.rows,'physical')), \
                patch.object(rollout,'maintenance') as maintenance, \
                patch.object(rollout.update,'prepare'), \
                patch.object(rollout.update,'apply',side_effect=failed), patch.object(rollout,'emit'):
            with self.assertRaisesRegex(RuntimeError,'restore failed'):
                rollout.stage_held(self.args,self.cfg,self.env)
        self.assertEqual([True],[call.args[2] for call in maintenance.call_args_list])

    def test_finish_requires_both_updated_reserves_before_preparing_active_perimetr(self):
        self.cfg['node_id']='perimetr'
        self.rows['comparator']['release_sha']='a'*40
        with patch.object(rollout.update,'inventory',return_value=(self.rows,'perimetr')), \
                patch.object(rollout.update,'prepare') as prepare:
            # Ensure this exercises replacement, not the idempotent already-installed case.
            self.rows['perimetr']['release_sha']='a'*40
            with self.assertRaisesRegex(RuntimeError,'QualifiedReserveRequired'):
                rollout.finish(self.args,self.cfg,self.env)
        prepare.assert_not_called()


if __name__=='__main__':
    unittest.main()
