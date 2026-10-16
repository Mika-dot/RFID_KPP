"""Native supervisor commands are journalled and reversible without tree edits."""
import tempfile
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy.ha import supervisor_binding as binding


class SupervisorBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'guardian').mkdir()
        (self.root / 'guardian/boot.py').write_text('bootstrap', encoding='utf-8')
        self.args = SimpleNamespace(config=self.root / 'node.json',
                                    environment=self.root / 'private.json')
        self.cfg = {'python': '/python path/bin/python'}
        self.dropin = self.root / 'unit.d/bootstrap.conf'

    @unittest.skipIf(os.name == 'nt', 'Linux systemd path semantics')
    def test_linux_rebind_and_restore_preserve_existing_dropin_and_do_not_start_services(self):
        self.dropin.parent.mkdir()
        self.dropin.write_text('old bootstrap\n')
        run = Mock(return_value=str(self.root / 'guardian/boot.py'))
        with patch.object(binding, 'DROPIN', self.dropin):
            saved = binding.capture(run)
            binding.install(saved, self.args, self.cfg, self.root, run)
            content = self.dropin.read_text()
            self.assertIn('ExecStart=\nExecStart=', content)
            self.assertIn('"/python path/bin/python"', content)
            self.assertIn(str(self.root / 'guardian/boot.py'), content)
            binding.restore(saved, self.args, run)
        self.assertEqual(self.dropin.read_text(), 'old bootstrap\n')
        self.assertTrue(all(call.args[0][:2] in (['systemctl', 'daemon-reload'], ['systemctl', 'show'])
                            for call in run.call_args_list))

    @unittest.skipIf(os.name == 'nt', 'Linux systemd path semantics')
    def test_linux_restore_removes_new_dropin_when_none_existed(self):
        with patch.object(binding, 'DROPIN', self.dropin):
            saved = binding.capture(Mock())
            binding.install(saved, self.args, self.cfg, self.root,
                            Mock(return_value=str(self.root / 'guardian/boot.py')))
            self.assertTrue(self.dropin.exists())
            binding.restore(saved, self.args, Mock())
        self.assertFalse(self.dropin.exists())

    def test_linux_systemd_arguments_escape_percent_quotes_and_reject_newlines(self):
        self.assertEqual(binding.systemd_quote('a%"b'), '"a%%\\"b"')
        with self.assertRaises(ValueError):
            binding.systemd_quote('path\nExecStart=other')

    def test_windows_preserves_task_definition_and_restores_via_private_file(self):
        run = Mock(return_value='<Task version="1.4">original definition</Task>')
        # Patch the module's os view, not global os.name (which changes pathlib).
        with patch.object(binding, 'os', SimpleNamespace(name='nt', open=__import__('os').open,
                fdopen=__import__('os').fdopen, O_CREAT=__import__('os').O_CREAT,
                O_TRUNC=__import__('os').O_TRUNC, O_WRONLY=__import__('os').O_WRONLY)):
            saved = binding.capture(run)
            binding.install(saved, self.args, self.cfg, self.root, run)
            script = run.call_args.args[0][-1]
            self.assertIn('Set-ScheduledTask', script)
            self.assertNotIn('Register-ScheduledTask', script)
            self.assertIn('run-windows.ps1', script)
            self.assertNotIn('Start-ScheduledTask', script)
            def restored(argv, timeout):
                self.assertIn('Register-ScheduledTask', argv[-1])
                xml = self.root / 'supervisor-task-restore.xml'
                self.assertEqual(xml.read_text(), saved['xml'])
            run.side_effect = restored
            binding.restore(saved, self.args, run)
        self.assertFalse((self.root / 'supervisor-task-restore.xml').exists())

    def test_missing_candidate_bootstrap_fails_before_any_native_mutation(self):
        (self.root / 'guardian/boot.py').unlink()
        run = Mock()
        with self.assertRaisesRegex(RuntimeError, 'QualifiedBootstrapMissing'):
            binding.install({'kind':'systemd-dropin'}, self.args, self.cfg, self.root, run)
        run.assert_not_called()
