"""Recovery phases preserve ownership and require observed live acceptance."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy.ha import recover_bus_rollout as rollout


class RecoveryRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg={'node_id':'comparator','state_dir':self.tmp.name,
                  'nodes':[{'id':key,'url':'http://'+key} for key in ('physical','perimetr','comparator')]}
        self.args=rollout.options(SimpleNamespace(release='b'*40))
        self.env={'PERIMETER_HA_TOKEN':'test-token'}
        self.rows={key:dict(node=key,sample_age=1,fencing_protocol=2,release_sha='b'*40,
            auto_failback=False,epoch=15,prepared=True,faulted=False,operator_maintenance=False,
            active=key=='physical',healthy=key=='physical',
            update={'operator_pinned':True,'pending':key!='physical'},
            services={str(i):{'ok':True} for i in range(5)},
            bus_sample_age=1,bus_metrics={'rfid_pending':0,'rfid_sent':50,'video_pending':0,'video_sent':0},
            metadata_mirror={'enabled':True,'error':None,'blocked_streams':[]})
            for key in ('physical','perimetr','comparator')}

    def test_acceptance_requires_all_pins_live_workers_fresh_metrics_and_completed_owner_trial(self):
        self.assertEqual((True,'physical',True),rollout.acceptance(self.rows,self.args.release))
        for case in ('pin','version','fault','worker','metrics','trial','maintenance','two_owners','failback'):
            rows=copy.deepcopy(self.rows)
            if case=='failback':rows['comparator']['auto_failback']=True
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

    def test_auto_main_release_accepts_qualified_unpinned_nodes(self):
        rows = copy.deepcopy(self.rows)
        for row in rows.values():
            row["auto_update"] = True
            row["update"]["operator_pinned"] = False
            row["update"]["qualification"] = "passed"
        self.assertEqual((True, "physical", True),
                         rollout.acceptance(rows, self.args.release))
        rows["comparator"]["update"]["qualification"] = "failed"
        self.assertFalse(rollout.acceptance(rows, self.args.release)[0])

    def test_verify_requires_two_minutes_continuous_health_after_any_bad_sample(self):
        clock = [0]
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        calls = [0]
        def inventory(*args):
            calls[0] += 1
            rows = copy.deepcopy(self.rows)
            if calls[0] == 13:  # Degrade once, 60 seconds into the first interval.
                rows['physical']['services']['0']['ok'] = False
            return rows, 'physical'
        with patch.object(rollout.update, 'inventory', side_effect=inventory), \
                patch.object(rollout.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(rollout.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+seconds)), \
                patch.object(rollout, 'build_opener', return_value=opener), patch.object(rollout, 'emit'):
            result = rollout.verify(self.args, self.cfg, self.env)
        self.assertEqual(185, clock[0])
        self.assertEqual(120, result['stable_seconds'])
        opener.open.assert_called_once()

    def test_verify_rejects_divergent_epochs_even_with_healthy_rows(self):
        self.rows['comparator']['epoch'] = 14
        with patch.object(rollout.update, 'inventory', return_value=(self.rows, 'physical')), \
                patch.object(rollout.time, 'monotonic', side_effect=[0, 601]), \
                patch.object(rollout, 'emit') as emit, patch.object(rollout, 'build_opener') as opener:
            with self.assertRaisesRegex(RuntimeError, 'LiveAcceptanceNotConfirmed'):
                rollout.verify(self.args, self.cfg, self.env)
        self.assertEqual(0, emit.call_args.kwargs['accepted_samples'])
        opener.assert_not_called()

    def test_mirror_error_is_explicitly_separate_from_core_ha_acceptance(self):
        self.rows['comparator']['metadata_mirror']['error']='MirrorReadBlocked'
        self.assertEqual((True,'physical',False),rollout.acceptance(self.rows,self.args.release))

    def test_repeated_held_phase_does_not_reinstall_an_observed_pinned_target(self):
        self.rows['comparator'].update(operator_maintenance=True, preflight={'ok':True})
        with patch.object(rollout.update, 'inventory', return_value=(self.rows, 'physical')), \
                patch.object(rollout.update, 'prepare') as prepare, \
                patch.object(rollout.update, 'apply') as apply, patch.object(rollout, 'emit'):
            result = rollout.stage_held(self.args, self.cfg, self.env)
        self.assertTrue(result['already_installed'])
        prepare.assert_not_called()
        apply.assert_not_called()

    def test_hold_refuses_active_node_before_maintenance_preparation_or_install(self):
        with patch.object(rollout.update,'inventory',return_value=(self.rows,'comparator')), \
                patch.object(rollout,'maintenance') as maintenance, \
                patch.object(rollout.update,'prepare') as prepare, \
                patch.object(rollout.update,'apply') as apply:
            with self.assertRaisesRegex(RuntimeError,'LocalNodeIsActive'):
                rollout.stage_held(self.args,self.cfg,self.env)
        maintenance.assert_not_called(); prepare.assert_not_called(); apply.assert_not_called()

    def test_untried_replacement_is_enabled_only_after_passive_hold_acknowledgement(self):
        self.assertFalse(self.args.replace_untried_reserve)
        def prepare(args, cfg, env):
            maintenance.assert_called_once_with(self.cfg, self.env['PERIMETER_HA_TOKEN'], True, self.args.release)
            self.assertTrue(args.replace_untried_reserve)
        with patch.object(rollout.update, 'inventory', return_value=(self.rows, 'physical')), \
                patch.object(rollout, 'maintenance') as maintenance, \
                patch.object(rollout.update, 'prepare', side_effect=prepare), \
                patch.object(rollout.update, 'apply', return_value={'installed':True}), \
                patch.object(rollout, 'emit'):
            rollout.stage_held(self.args, self.cfg, self.env)

    def test_failed_maintenance_hold_does_not_enable_trial_replacement(self):
        with patch.object(rollout.update, 'inventory', return_value=(self.rows, 'physical')), \
                patch.object(rollout, 'maintenance', side_effect=RuntimeError('hold unavailable')), \
                patch.object(rollout.update, 'prepare') as prepare, \
                patch.object(rollout.update, 'apply') as apply:
            with self.assertRaisesRegex(RuntimeError, 'hold unavailable'):
                rollout.stage_held(self.args, self.cfg, self.env)
        self.assertFalse(self.args.replace_untried_reserve)
        prepare.assert_not_called()
        apply.assert_not_called()

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
