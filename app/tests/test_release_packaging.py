"""Ensure the delivered source can be built and distribution archives cannot collect user data."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from package_source import build_sources
from package_release import distribution_files
from rpts.update_package import REQUIRED


class ReleasePackagingTests(unittest.TestCase):
    def test_unpacked_source_zip_keeps_build_entry_points_without_git_or_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'source'
            files = {'Build.bat': 'build', 'app/VERSION.txt': '11.28',
                     'app/launcher/launcher.cpp': 'code', 'app/launcher/build.cmd': 'build',
                     'app/installer/RPTS.iss': 'installer', 'app/tools/build-release.ps1': 'build',
                     '.github/workflows/release.yml': 'ci',
                     'runtime/python/python.exe': 'binary', 'data/settings/secret.json': 'secret',
                     'app/tools/.env': 'secret', 'app/desktop/src/obj/cache.json': 'cache'}
            for rel, value in files.items():
                path = root/rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
            with patch('package_source.subprocess.check_output') as git:
                archive = build_sources(root, Path(directory)/'out')
                git.assert_not_called()
            with zipfile.ZipFile(archive) as z: names = z.namelist()
            for rel in ('Build.bat', 'app/launcher/launcher.cpp', 'app/installer/RPTS.iss', 'app/tools/build-release.ps1', '.github/workflows/release.yml'):
                self.assertIn('RenPyTranslator-source/'+rel, names)
            self.assertFalse(any('/runtime/' in p or '/data/' in p or p.endswith('/.env') or '/obj/' in p for p in names))

    def test_distribution_requires_root_exe_and_excludes_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for rel in REQUIRED | {'runtime/local_ai/model.gguf', 'data/projects/game.sqlite', 'Build.bat', 'app/desktop/bin/debug.pdb', 'runtime/dotnet/debug.pdb', 'runtime/python/test.pdb', 'app/desktop/bin/RPTS.Desktop.pdb', 'app/desktop/bin/RPTS.FolderPicker.pdb'}:
                path = root/rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
            names = distribution_files(root)
            self.assertIn('app/desktop/bin/RPTS.Desktop.pdb', names)
            self.assertIn('app/desktop/bin/RPTS.FolderPicker.pdb', names)
            self.assertFalse(any(p.endswith('debug.pdb') or p.endswith('test.pdb') for p in names))
            self.assertIn('RPTS.exe', names)
            self.assertIn('Build.bat', names)
            self.assertNotIn('runtime/local_ai/model.gguf', names)
            self.assertNotIn('data/projects/game.sqlite', names)
            (root/'RPTS.exe').unlink()
            with self.assertRaisesRegex(ValueError, 'RPTS.exe'): distribution_files(root)
