from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import deploy_instance, napcat_watchdog, check_public_tree
from scripts import ops_runtime, instance_health


class OpsRegressionTests(unittest.TestCase):
    def test_exact_socket_gate_requires_listener_both_process_owners_and_exact_port(self):
        valid = '\n'.join((
            'LISTEN 0 128 127.0.0.1:6199 0.0.0.0:* users:(("python",pid=11,fd=1))',
            'ESTAB 0 0 127.0.0.1:6199 127.0.0.1:45678 users:(("python",pid=11,fd=2))',
            'ESTAB 0 0 127.0.0.1:45678 127.0.0.1:6199 users:(("qq",pid=22,fd=2))',
        ))
        self.assertTrue(ops_runtime.exact_onebot_sockets(valid, 6199, {11}, {22}))
        for invalid in (valid.replace(':6199', ':61990'), '\n'.join(valid.splitlines()[1:]),
                        valid.replace('pid=22', 'pid=23'), valid.replace('pid=11', 'pid=22'),
                        valid.replace('127.0.0.1:45678', '10.0.0.1:45678'),
                        '\n'.join(valid.splitlines()[:-1])):
            self.assertFalse(ops_runtime.exact_onebot_sockets(invalid, 6199, {11}, {22}))

    def _onebot_factory(self, *, identity='1234567890', online=True, good=True, failure=None):
        calls, connections = [], []
        payloads = [{'status': 'ok', 'retcode': 0, 'data': {'user_id': identity}},
                    {'status': 'ok', 'retcode': 0, 'data': {'online': online, 'good': good}}]
        def factory(host, port, timeout):
            connection = Mock()
            connection.request.side_effect = failure
            response = Mock(status=200)
            response.read.return_value = json.dumps(payloads[len(connections)]).encode()
            connection.getresponse.return_value = response
            calls.append((host, port, timeout))
            connections.append(connection)
            return connection
        return factory, calls, connections

    def test_onebot_probe_is_authenticated_loopback_read_only_and_identity_checked(self):
        values = {'BOT_SELF_ID': '1234567890', 'NAPCAT_ACCESS_TOKEN': 'synthetic-credential'}
        factory, calls, connections = self._onebot_factory()
        result = ops_runtime.onebot_status('carrot', values, connection_factory=factory)
        self.assertTrue(result['identity_matches'])
        self.assertEqual([('127.0.0.1', 6201, 3)]*2, calls)
        self.assertEqual(['/get_login_info', '/get_status'], [c.request.call_args.args[1] for c in connections])
        for connection in connections:
            self.assertEqual('POST', connection.request.call_args.args[0])
            self.assertEqual('Bearer synthetic-credential', connection.request.call_args.kwargs['headers']['Authorization'])
            connection.close.assert_called_once()
        for overrides in ({'identity': '9876543210'}, {'online': False}, {'good': False}, {'online': 'true'}):
            with self.subTest(overrides=overrides), self.assertRaises(RuntimeError):
                ops_runtime.onebot_status('carrot', values, connection_factory=self._onebot_factory(**overrides)[0])

    def test_onebot_probe_rejects_redirectable_remote_origins_and_redacts_failures(self):
        values = {'BOT_SELF_ID': '1234567890', 'NAPCAT_ACCESS_TOKEN': 'synthetic-credential'}
        for url in ('https://127.0.0.1:6201', 'http://localhost:6201', 'http://10.0.0.1:6201',
                    'http://127.0.0.1:6201/path', 'http://user@127.0.0.1:6201', 'http://127.0.0.1:6201?token=x'):
            factory = Mock()
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                ops_runtime.onebot_status('carrot', {**values, 'ONEBOT_HTTP_URL': url}, connection_factory=factory)
            factory.assert_not_called()
        factory, _, connections = self._onebot_factory(failure=TimeoutError('synthetic-credential'))
        with self.assertRaises(RuntimeError) as failure:
            ops_runtime.onebot_status('carrot', values, connection_factory=factory)
        self.assertNotIn('synthetic-credential', str(failure.exception))
        connections[0].close.assert_called_once()

    def test_running_process_must_use_requested_release_not_just_current_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release, wrong = root/'release', root/'old'
            release.mkdir(); wrong.mkdir()
            process = root/'proc/11'; process.mkdir(parents=True)
            (process/'cwd').symlink_to(release)
            (process/'cmdline').write_bytes(b'.venv/bin/python\0-B\0bot.py\0')
            instance_health.verify_running_release(11, release, proc_root=root/'proc')
            (process/'cwd').unlink(); (process/'cwd').symlink_to(wrong)
            with self.assertRaisesRegex(RuntimeError, 'working directory'):
                instance_health.verify_running_release(11, release, proc_root=root/'proc')

    def test_watchdog_check_only_never_creates_lock_or_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = napcat_watchdog.RuntimeTarget('carrot', 'napcat@carrot.service',
                'qqbot@carrot.service', 6199, root/'state/state.json', root/'locks/run.lock')
            metrics = napcat_watchdog.Metrics(1, 0, 1, False, False)
            with patch.object(napcat_watchdog, 'target_for_instance', return_value=target), \
                 patch.object(napcat_watchdog, 'collect_metrics', return_value=metrics), \
                 patch.object(sys, 'argv', ['watchdog', '--check-only']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, napcat_watchdog.main())
            self.assertEqual([], list(root.iterdir()))

    def test_rollback_rechecks_old_release_and_reports_both_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old, new = '1'*40, '2'*40
            for sha in (old, new):
                (root/'releases'/sha).mkdir(parents=True)
            instance = root/'instances/carrot'
            instance.mkdir(parents=True)
            (instance/'current').symlink_to(root/'releases'/old)
            health = Mock(return_value=False)
            with self.assertRaises(deploy_instance.DeploymentError) as failure:
                deploy_instance.deploy_existing_release('carrot', new, root, restart=Mock(), health=health)
            self.assertEqual([('carrot', new), ('carrot', old)], [call.args for call in health.call_args_list])
            self.assertIn('rollback', str(failure.exception))
            self.assertIn('failed', str(failure.exception))

    def test_public_tree_rejects_production_env_and_opaque_assignments_without_runtime_secrets(self):
        self.assertTrue(check_public_tree.path_findings('.env'))
        self.assertTrue(check_public_tree.path_findings('instances/carrot/.env.production'))
        raw = b'NAPCAT_ACCESS_TOKEN' + b'=' + b'opaque-synthetic-test-credential\n'
        self.assertTrue(check_public_tree.scan_bytes('settings.conf', raw, {}))
        self.assertEqual([], check_public_tree.path_findings('.env.example'))

    def test_watchdog_check_only_preserves_existing_bytes_mtime_and_failure_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = napcat_watchdog.RuntimeTarget('carrot', 'napcat@carrot.service',
                'qqbot@carrot.service', 6199, root/'state.json', root/'run.lock')
            target.state_path.write_text('{"last_restart_epoch": 1, "websocket_failures": 1}')
            target.lock_path.write_bytes(b'keep-existing-lock')
            before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (target.state_path, target.lock_path)]
            with patch.object(napcat_watchdog, 'target_for_instance', return_value=target), \
                 patch.object(napcat_watchdog, 'collect_metrics', return_value=napcat_watchdog.Metrics(1,0,1,False,False)), \
                 patch.object(sys, 'argv', ['watchdog', '--check-only']), contextlib.redirect_stdout(io.StringIO()):
                napcat_watchdog.main()
            after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (target.state_path, target.lock_path)]
            self.assertEqual(before, after)

    def test_health_subprocess_budget_is_real_and_preserves_last_failure_class(self):
        clock = iter((0, 0, 3, 3, 5, 5))
        with patch.object(deploy_instance.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 3)) as run, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertFalse(deploy_instance.wait_for_command_health(['probe'], timeout_seconds=5,
                             monotonic=lambda: next(clock), sleep=Mock()))
        self.assertEqual([5, 2], [call.kwargs['timeout'] for call in run.call_args_list])
        self.assertIn('probe_timeout', errors.getvalue())

    def test_entrypoint_checks_detect_relocated_venv_even_when_python_still_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root/'.venv/bin'; binary.mkdir(parents=True)
            (binary/'python').symlink_to(sys.executable)
            (binary/'activate').write_text('VIRTUAL_ENV="'+str(root/'.venv')+'"\n')
            (binary/'pip').write_text('#!'+str(root/'.venv/bin/python')+'\n')
            deploy_instance.validate_venv_entrypoints(root)
            (binary/'pip').write_text('#!/missing/export/.venv/bin/python\n')
            with self.assertRaisesRegex(deploy_instance.DeploymentError, 'stale interpreter'):
                deploy_instance.validate_venv_entrypoints(root)

    def test_lock_file_is_selected_without_changing_pins_or_fetching_in_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve()
            lock = release/'requirements.lock'; lock.write_text('synthetic-package==1.2.3\n')
            (release/'requirements.txt').write_text('synthetic-package>=1\n')
            with patch.object(deploy_instance.subprocess, 'run') as run, \
                 patch.object(deploy_instance, '_environment_run', return_value='') as environment, \
                 patch.object(deploy_instance, 'inspect_environment', return_value={
                     'python_version':'3.10.19','python_implementation':'cpython','pip_freeze_sha256':'a'*64}):
                build = deploy_instance.build_environment(release)
            self.assertEqual('requirements.lock', build['requirements_file'])
            self.assertIn(str(release/'.venv'), run.call_args.args[0])
            self.assertEqual((release, '-m', 'pip', 'install', '-r', str(lock)), environment.call_args_list[0].args)
            lock.write_text('synthetic-package>=1\n')
            with self.assertRaisesRegex(deploy_instance.DeploymentError, 'exact'):
                deploy_instance.build_environment(release)

    def test_public_assignment_placeholders_do_not_hide_real_single_json_identifier(self):
        for path in ('sample.conf', '.env.example', 'sample.json'):
            for source in ('AI_API_KEY'+'=opaque-value', '"SUPERUSERS"'+': ["8765432109"]',
                           '"NAPCAT_ACCESS_TOKEN"'+': "opaque-value"'):
                self.assertTrue(check_public_tree.scan_bytes(path, source.encode(), {}))
        for source in ('AI_API_KEY'+'=\nPORT=6199\n', 'NAPCAT_ACCESS_TOKEN=replace-with-random-token',
                       '"SUPERUSERS"'+': []'):
            self.assertEqual([], check_public_tree.scan_bytes('.env.example', source.encode(), {}))
        self.assertTrue(check_public_tree.path_findings('nested/backups/private.json'))
        self.assertTrue(check_public_tree.path_findings('nested/data/content_alert/managed/current.json'))

    def test_commit_range_scans_added_then_deleted_private_file_and_only_range_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q'); git('config', 'user.name', 'Synthetic Scanner'); git('config', 'user.email', 'synthetic@example.invalid')
            (root/'safe.txt').write_text('safe\n')
            git('add', '.'); git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root/'.env').write_text('NAPCAT_ACCESS_TOKEN'+'=opaque-range-fixture\n')
            git('add', '.env'); git('commit', '-qm', 'add fixture')
            git('rm', '-q', '.env'); git('commit', '-qm', 'delete fixture')
            with patch.object(check_public_tree, 'ROOT', root):
                self.assertEqual([], check_public_tree.scan_ref(None, {}))
                revisions = list(check_public_tree.revisions(base+'..HEAD'))
                self.assertEqual(2, len(revisions)); self.assertNotIn(base, revisions)
                findings = [finding for revision in revisions for finding in check_public_tree.scan_ref(revision, {})]
            self.assertTrue(any('private environment file' in finding for finding in findings))
            self.assertTrue(any('non-placeholder assignment' in finding for finding in findings))
            self.assertFalse(any('opaque-range-fixture' in finding for finding in findings))

    def test_failed_first_deploy_stops_candidate_without_restart_of_missing_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sha = '2'*40; (root/'releases'/sha).mkdir(parents=True)
            restart, stop = Mock(), Mock()
            with self.assertRaises(deploy_instance.DeploymentError):
                deploy_instance.deploy_existing_release('carrot', sha, root, restart=restart,
                                                        health=Mock(return_value=False), stop=stop)
            restart.assert_called_once_with('carrot'); stop.assert_called_once_with('carrot')
            report = json.loads((root/'instances/carrot/.deployment-result.json').read_text())
            self.assertEqual('no_previous_release_stopped', report['rollback'])

    def test_runtime_sensitive_json_lists_expand_to_single_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            identifier = str(10**9+73_918_526)
            (root/'.env').write_text('SUPERUSERS='+json.dumps([identifier])+'\n')
            with patch.object(check_public_tree, 'ROOT', root):
                values = check_public_tree._runtime_values()
            self.assertIn(identifier, values.values())
            findings = check_public_tree.runtime_findings('sample.txt', identifier, values)
            self.assertTrue(findings); self.assertNotIn(identifier, '\n'.join(findings))

    def test_health_version_runs_from_stable_bin_without_repository_imports(self):
        source = Path(deploy_instance.__file__).parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ('instance_health.py', 'deploy_instance.py', 'ops_runtime.py'):
                if (source/name).exists():
                    shutil.copy2(source/name, root/name)
            result = subprocess.run([sys.executable, str(root/'instance_health.py'), '--version'],
                                    cwd=root, env={**os.environ, 'PYTHONPATH': ''},
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('ops', result.stdout.lower())
            self.assertFalse((root/'__pycache__').exists())


if __name__ == '__main__':
    unittest.main()
