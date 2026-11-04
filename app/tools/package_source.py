"""Source-only ZIP and release checksums; works in a clone or an unpacked source distribution."""
import argparse
import subprocess
import shutil
import sys
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from rpts.update_package import sha256
from package_release import SOURCE_FILES, SOURCE_FOLDERS

ROOT = Path(__file__).resolve().parents[2]
EXTRA = ('README.md', 'AGENTS.md', 'Build.bat', 'RPTS.bat', 'RPTS.vbs', 'RPTS-Console.bat',
         'RPTS-Debug.bat', '.gitignore', '.gitattributes')


def source_files(root):
    root = Path(root)
    result = {p for p in EXTRA + SOURCE_FILES if (root/p).is_file()}
    for folder in SOURCE_FOLDERS + ('.github',):
        for path in (root/folder).rglob('*'):
            parts = path.relative_to(root/folder).parts
            if not path.is_file() or any(p.startswith('.') or p in {'bin', 'obj', '__pycache__', 'checks', 'archive'} for p in parts):
                continue
            if path.suffix.lower() in {'.pyc', '.pyo', '.zip', '.exe', '.dll', '.log', '.tmp'}:
                continue
            result.add(path.relative_to(root).as_posix())
    if (root/'.git').exists() and shutil.which('git'):
        tracked = set(subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode('utf-8').split('\0'))
        result &= tracked
    return sorted(result)


def build_sources(root, out):
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    version = (root/'app/VERSION.txt').read_text(encoding='utf-8').strip().replace('.', '_')
    archive = out/f'RenPyTranslator_{version}_source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in source_files(root): z.write(root/rel, 'RenPyTranslator-source/'+rel)
    return archive


def checksums(out):
    out = Path(out)
    files = sorted(p for p in out.iterdir() if p.is_file() and (p.suffix in {'.exe', '.zip'} or p.name == 'update.json'))
    (out/'SHA256SUMS.txt').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in files), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=ROOT/'dist')
    parser.add_argument('--checksums-only', action='store_true')
    args = parser.parse_args()
    if not args.checksums_only: print(build_sources(ROOT, args.out))
    checksums(args.out)
