"""Update validation, failed downloads, preservation, locks and rollback without Windows or network."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from rpts import update_package as pkg
from rpts import updater
from rpts.desktop import Desktop


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'RPTS'
        self.root.mkdir()
        self.archive = Path(self.temp.name)/'release.zip'
        self.files = {p: ('new '+p).encode() for p in pkg.REQUIRED}
        self.files['app/VERSION.txt'] = b'11.27\n'
        self.files['app/web/app.js'] = b'new frontend'

    def pack(self, extra=None, corrupt=None, change_manifest=None):
        manifest = {'schema': 1, 'channel': 'dev', 'platform': 'win-x64', 'version': '11.27',
                    'build_id': 'a'*40, 'build_number': 123,
                    'files': [{'path': p, 'bytes': len(v), 'sha256': hashlib.sha256(v).hexdigest()} for p, v in self.files.items()]}
        if change_manifest: change_manifest(manifest)
        with zipfile.ZipFile(self.archive, 'w') as z:
            for p, v in self.files.items():
                z.writestr('RenPyTranslator/'+p, b'X'*len(v) if corrupt == p else v)
            z.writestr('RenPyTranslator/app/release_manifest.json', json.dumps(manifest))
            for p, v in (extra or {}).items(): z.writestr(p, v)
        release = {'schema': 1, 'channel': 'dev', 'platform': 'win-x64', 'version': '11.27', 'build_id': 'a'*40,
                   'build_number': 123, 'bytes': self.archive.stat().st_size, 'unpacked_bytes': 100000,
                   'sha256': pkg.sha256(self.archive),
                   'package_url': 'https://github.com/UFrimNe/RenPyTranslator/releases/download/dev-123/RenPyTranslator_11_27_dev_win64.zip'}
        return release

    def put(self, rel, value):
        path = self.root/rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    def test_full_install_keeps_models_projects_settings_and_games(self):
        release = self.pack()
        keep = {'data/projects/game/work/translation_project.sqlite': b'DB', 'data/settings/desktop.json': b'SETTINGS',
                'runtime/local_ai/models/model.gguf': b'MODEL', 'data/translations/manual/text.rpy': b'MANUAL',
                'games/Emberbound/game/script.rpa': b'GAME', 'app/user-notes.txt': b'CUSTOM'}
        for p, data in keep.items(): self.put(p, data)
        self.put('app/web/app.js', b'old frontend')
        pkg.install(self.root, self.archive, release)
        for p, data in keep.items(): self.assertEqual(data, (self.root/p).read_bytes())
        self.assertEqual(b'new frontend', (self.root/'app/web/app.js').read_bytes())
        self.assertEqual(b'old frontend', (self.root/'data/updates/rollback/files/app/web/app.js').read_bytes())
        self.assertEqual('committed', pkg.read_json(self.root/'data/updates/rollback/journal.json')['status'])

    def test_locked_destination_rolls_back_every_file_and_removes_new_files(self):
        release = self.pack()
        for p in self.files: self.put(p, ('old '+p).encode())
        new = next(iter(self.files))
        (self.root/new).unlink()
        before = {p: (self.root/p).read_bytes() for p in self.files if p != new}
        attempts = []
        def fail(source, target):
            attempts.append(str(target))
            if len(attempts) == 6: raise PermissionError('WinError 32 fixture')
            os.replace(source, target)
        with self.assertRaises(PermissionError): pkg.install(self.root, self.archive, release, replace=fail)
        for p, data in before.items(): self.assertEqual(data, (self.root/p).read_bytes())
        self.assertFalse((self.root/new).exists())
        self.assertEqual('rolled_back', pkg.read_json(self.root/'data/updates/rollback/journal.json')['status'])

    def test_interrupted_journal_is_recovered_before_launch(self):
        self.put('app/VERSION.txt', b'half installed')
        self.put('app/web/new.js', b'new')
        self.put('data/updates/rollback/files/app/VERSION.txt', b'11.26')
        pkg.write_json(self.root/'data/updates/rollback/journal.json', {'status': 'applying', 'operations': [
            {'path': 'app/VERSION.txt', 'existed': True}, {'path': 'app/web/new.js', 'existed': False}]})
        self.assertTrue(pkg.recover(self.root))
        self.assertEqual(b'11.26', (self.root/'app/VERSION.txt').read_bytes())
        self.assertFalse((self.root/'app/web/new.js').exists())
        self.assertFalse(pkg.recover(self.root))

    def test_only_unchanged_owned_obsolete_files_are_removed(self):
        release = self.pack()
        for p in ('app/web/obsolete.js', 'app/web/custom.js'): self.put(p, b'original')
        old = {'files': [{'path': p, 'sha256': hashlib.sha256(b'original').hexdigest()}
                         for p in ('app/web/obsolete.js', 'app/web/custom.js')]}
        self.put('app/web/custom.js', b'user modification')
        pkg.write_json(self.root/'app/release_manifest.json', old)
        pkg.install(self.root, self.archive, release)
        self.assertFalse((self.root/'app/web/obsolete.js').exists())
        self.assertEqual(b'user modification', (self.root/'app/web/custom.js').read_bytes())

    def test_extra_data_model_or_traversal_is_rejected_before_any_change(self):
        for p in ('RenPyTranslator/data/settings/key.json', 'RenPyTranslator/runtime/local_ai/model.gguf',
                  'RenPyTranslator/app/../../evil', 'RenPyTranslator/app/CON.txt', 'RenPyTranslator/app/x:ads',
                  'RenPyTranslator/app/x. ', 'RenPyTranslator/app/a\\b.py'):
            with self.subTest(p=p):
                release = self.pack(extra={p: b'bad'})
                with self.assertRaises(ValueError): pkg.install(self.root, self.archive, release)
                self.assertFalse((self.root/'app/VERSION.txt').exists())

    def test_case_duplicate_and_unlisted_files_are_rejected(self):
        for p in ('RenPyTranslator/app/version.txt', 'RenPyTranslator/app/web/unlisted.js'):
            with self.subTest(p=p):
                self.pack(extra={p: b'bad'})
                with self.assertRaises(ValueError): pkg.inspect_package(self.archive)

    def test_wrong_inner_hash_changes_nothing(self):
        release = self.pack(corrupt='app/web/app.js')
        self.put('app/VERSION.txt', b'old')
        with self.assertRaisesRegex(ValueError, 'сумма'): pkg.install(self.root, self.archive, release)
        self.assertEqual(b'old', (self.root/'app/VERSION.txt').read_bytes())

    def test_pointer_mismatch_and_incomplete_inventory_are_rejected(self):
        self.pack()
        with self.assertRaises(ValueError): pkg.inspect_package(self.archive, {'build_id': 'b'*40})
        self.files.pop('runtime/python/python.exe')
        self.pack()
        with self.assertRaises(ValueError): pkg.inspect_package(self.archive)

    def test_symlink_install_target_is_rejected(self):
        if os.name == 'nt': self.skipTest('Windows symlink privilege not required for source suite')
        other = Path(self.temp.name)/'other'; other.mkdir()
        (self.root/'app').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError): pkg.install(self.root, self.archive, self.pack())
        self.assertEqual([], list(other.iterdir()))

    def test_symlink_archive_member_is_rejected(self):
        self.pack()
        with zipfile.ZipFile(self.archive, 'a') as z:
            info = zipfile.ZipInfo('RenPyTranslator/app/link')
            info.external_attr = 0o120777 << 16
            z.writestr(info, '/outside')
        with self.assertRaises(ValueError): pkg.inspect_package(self.archive)

    def test_competing_lock_cannot_start_update(self):
        with pkg.FileLock(self.root/'data/operation.lock'):
            with self.assertRaises(RuntimeError):
                with pkg.FileLock(self.root/'data/operation.lock'): pass
        with pkg.FileLock(self.root/'data/operation.lock'): pass

    def test_helper_imports_from_its_copy_with_isolated_python_path(self):
        helper = Path(self.temp.name)/'independent'
        helper.mkdir()
        app = Path(__file__).resolve().parents[1]
        shutil.copy2(app/'tools/apply_update.py', helper/'apply_update.py')
        shutil.copy2(app/'src/rpts/update_package.py', helper/'update_package.py')
        result = subprocess.run([sys.executable, '-I', str(helper/'apply_update.py'), '--self-check'],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows file sharing')
    def test_actual_windows_file_lock_causes_complete_rollback(self):
        release = self.pack()
        self.put('app/web/app.js', b'old locked frontend')
        with (self.root/'app/web/app.js').open('rb'):
            with self.assertRaises(PermissionError): pkg.install(self.root, self.archive, release)
        self.assertEqual(b'old locked frontend', (self.root/'app/web/app.js').read_bytes())
        self.assertFalse((self.root/'app/VERSION.txt').exists())

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows process handles')
    def test_windows_waits_for_exact_process_handle_and_accepts_cancellation(self):
        ready, cancel = self.root/'ready', self.root/'cancel'
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.5)'])
        try:
            pkg.wait_for_parents([child.pid], ready, cancel, timeout=10)
            self.assertTrue(ready.exists())
            self.assertIsNotNone(child.poll())
        finally:
            child.wait(timeout=10)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        try:
            cancel.touch()
            with self.assertRaises(pkg.HandoffCancelled): pkg.wait_for_parents([child.pid], ready, cancel)
            self.assertIsNone(child.poll())
        finally:
            child.kill(); child.wait(timeout=10)

    def test_download_verifies_outer_hash_and_removes_partial(self):
        release = self.pack()
        content = self.archive.read_bytes()
        target = self.root/'download.zip'
        updater.download(release, target, opener=lambda url: io.BytesIO(content))
        self.assertEqual(content, target.read_bytes())
        for bad in (content[:-1], content+b'extra', content.replace(b'new frontend', b'bad frontend')):
            with self.subTest(size=len(bad)):
                with self.assertRaises(ValueError): updater.download(release, target, opener=lambda url: io.BytesIO(bad))
                self.assertFalse(target.with_suffix('.part').exists())
                self.assertEqual(content, target.read_bytes())

    def test_release_requires_our_pinned_https_asset(self):
        release = self.pack()
        self.assertEqual('dev', updater.validate_release(release)['channel'])
        for field, value in [('package_url', 'https://example.com/a.zip'), ('package_url', release['package_url'].replace('dev-123', 'dev-latest')),
                             ('package_url', release['package_url']+'?token=secret'), ('sha256', 'bad'), ('build_number', '123'),
                             ('channel', 'main'), ('min_updater', 999), ('bytes', -1)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError): updater.validate_release(dict(release, **{field: value}))

    def test_check_no_release_offline_and_new_build(self):
        manager = updater.Updater(self.root)
        with patch.object(updater, 'open_url', side_effect=urllib.error.HTTPError(updater.FEED, 404, '', {}, None)):
            manager._check()
        self.assertEqual('unpublished', manager.snapshot()['status'])
        release = self.pack()
        with patch.object(updater, 'open_url', return_value=io.BytesIO(json.dumps(release).encode())): manager._check()
        self.assertEqual('available', manager.snapshot()['status'])
        with patch.object(updater, 'open_url', side_effect=OSError('offline')): manager._check()
        self.assertEqual('error', manager.snapshot()['status'])
        self.assertTrue((self.root/'data/logs/updater.log').is_file())

    def test_check_never_downgrades_or_reinstalls_same_commit(self):
        release = self.pack()
        manager = updater.Updater(self.root)
        for current in ({'build_id': 'a'*40}, {'build_id': 'b'*40, 'build_number': 124}):
            pkg.write_json(self.root/'app/release_manifest.json', current)
            with patch.object(updater, 'open_url', return_value=io.BytesIO(json.dumps(release).encode())): manager._check()
            self.assertEqual('current', manager.snapshot()['status'])
            self.assertIsNone(manager.release)

    def test_desktop_rejects_update_while_translation_runs_and_jobs_during_update(self):
        with patch.object(Desktop, '_hardware', lambda _: None): app = Desktop(self.root)
        app.job = {'status': 'running'}
        with patch.object(app.updater, 'begin') as begin:
            with self.assertRaises(ValueError): app.mutate('/api/update/install', {})
            begin.assert_not_called()
        app.job = None
        app.updater.set(status='downloading')
        with self.assertRaises(ValueError): app.start('report')
        with self.assertRaises(ValueError): app.mutate('/api/select', {'id': 'x'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
