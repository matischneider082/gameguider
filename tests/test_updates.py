"""Update checks must distinguish the running checkout from GitHub's branch."""
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import updates


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', '-b', updates.BRANCH, str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'Test'], check=True)
        (self.root / 'tracked').write_text('one')
        subprocess.run(['git', '-C', str(self.root), 'add', 'tracked'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'initial'], check=True)
        patcher = mock.patch('game_guide.updates._authenticated_compare', return_value=None)
        self.authenticated_compare = patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, status, ahead=0, behind=0):
        return io.BytesIO(json.dumps({'status': status, 'ahead_by': ahead, 'behind_by': behind}).encode())

    def test_available_update_and_safe_instructions(self):
        with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.response('ahead', 2, 0)):
            result = updates.check_for_updates(self.root)
        self.assertEqual(result['status'], 'available')
        self.assertIn('git pull --ff-only', result['instructions'])
        self.assertEqual(result['ahead_by'], 2)

    def test_up_to_date_with_local_changes_is_labeled(self):
        (self.root / 'tracked').write_text('modified')
        with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.response('identical')):
            result = updates.check_for_updates(self.root)
        self.assertEqual(result['status'], 'current')
        self.assertTrue(result['dirty'])

    def test_diverged_is_not_a_normal_update(self):
        with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.response('diverged', 2, 1)):
            result = updates.check_for_updates(self.root)
        self.assertEqual(result['status'], 'diverged')
        self.assertNotIn('instructions', result)

    def test_network_failure_is_inconclusive(self):
        with mock.patch('game_guide.updates.urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertEqual(updates.check_for_updates(self.root)['status'], 'error')

    def release(self, tag):
        return io.BytesIO(json.dumps({'tag_name': tag, 'html_url': f'https://github.com/x/releases/{tag}'}).encode())

    def test_a_shorter_version_is_not_older_than_the_same_release_with_trailing_zeros(self):
        # (1, 2) < (1, 2, 0) in tuple order, so 1.2 used to be "behind" 1.2.0.
        with mock.patch('importlib.metadata.version', return_value='1.2'):
            with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.release('v1.2.0')):
                same = updates.check_for_updates(self.root / 'missing')
        self.assertEqual(same['status'], 'current')
        self.assertEqual((same['installed'], same['latest']), ('1.2', '1.2.0'))
        with mock.patch('importlib.metadata.version', return_value='1.2'):
            with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.release('v1.3')):
                newer = updates.check_for_updates(self.root / 'missing')
        self.assertEqual(newer['status'], 'available')

    def test_installing_runs_the_release_installer_only_for_an_installed_copy_with_a_newer_release(self):
        ok = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch('game_guide.updates.check_for_updates', return_value={'status': 'available', 'latest': '9.9.9'}):
            with mock.patch('game_guide.updates.subprocess.run', return_value=ok) as run:
                done = updates.install_update()
        self.assertEqual((done['status'], done['latest']), ('installed', '9.9.9'))
        self.assertIn('releases/latest/download/install', ' '.join(run.call_args.args[0]))
        # A source checkout is also 'available' but has no release number: it updates with git, never the installer.
        for check in ({'status': 'available', 'revision': 'abc1234'}, {'status': 'current', 'latest': '0.3.0'}):
            with mock.patch('game_guide.updates.check_for_updates', return_value=check):
                with mock.patch('game_guide.updates.subprocess.run') as run:
                    with self.assertRaises(ValueError):
                        updates.install_update()
                run.assert_not_called()

    def test_a_failed_installer_reports_its_last_lines(self):
        failed = subprocess.CompletedProcess([], 1, '', 'a\nb\nboom')
        with mock.patch('game_guide.updates.check_for_updates', return_value={'status': 'available', 'latest': '9.9.9'}):
            with mock.patch('game_guide.updates.subprocess.run', return_value=failed):
                result = updates.install_update()
        self.assertEqual(result['status'], 'error')
        self.assertIn('boom', result['detail'])

    def test_an_installed_copy_compares_its_version_with_the_latest_release(self):
        with mock.patch('importlib.metadata.version', return_value='0.2.0'):
            for tag, status in (('v0.10.0', 'available'), ('v0.2.0', 'current'), ('v0.1.9', 'current')):
                with self.subTest(tag=tag), mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.release(tag)):
                    result = updates.check_for_updates(self.root / 'missing')
                self.assertEqual((result['status'], result['installed']), (status, '0.2.0'))
        self.assertEqual(result['github_url'], 'https://github.com/x/releases/v0.1.9')

    def test_a_private_release_is_read_through_the_github_cli_and_offline_is_an_error(self):
        gh = mock.Mock(stdout=json.dumps({'tag_name': 'v9.0.0'}))
        with mock.patch('game_guide.updates.urllib.request.urlopen', side_effect=OSError('404')), \
                mock.patch('game_guide.updates.subprocess.run', return_value=gh) as run:
            self.assertEqual(updates._release_check()['latest'], '9.0.0')
        self.assertEqual(run.call_args.args[0], ['gh', 'api', updates.RELEASES])
        with mock.patch('game_guide.updates.urllib.request.urlopen', side_effect=OSError('offline')), \
                mock.patch('game_guide.updates.subprocess.run', side_effect=OSError('no gh')):
            self.assertEqual(updates._release_check()['status'], 'error')

    def test_wrong_branch_has_no_pull_instructions(self):
        subprocess.run(['git', '-C', str(self.root), 'switch', '-qc', 'other'], check=True)
        with mock.patch('game_guide.updates.urllib.request.urlopen', return_value=self.response('ahead', 1, 0)):
            result = updates.check_for_updates(self.root)
        self.assertEqual(result['status'], 'available')
        self.assertNotIn('instructions', result)

    def test_authenticated_github_comparison_supports_private_repository(self):
        self.authenticated_compare.return_value = {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0}
        with mock.patch('game_guide.updates.urllib.request.urlopen') as public_request:
            result = updates.check_for_updates(self.root)
        self.assertEqual(result['status'], 'available')
        public_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
