"""Build a clean full portable ZIP plus a pinned dev-channel update descriptor."""
from __future__ import annotations
import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from rpts.update_package import REQUIRED, ROOT_FILES, owned_path, sha256, write_json

SOURCE_FOLDERS = ('app/src', 'app/tools', 'app/web', 'app/templates', 'app/tests',
                  'app/desktop/src', 'app/desktop/picker', 'docs')
SOURCE_FILES = ('app/VERSION.txt', 'app/pyproject.toml', 'app/ICON_SOURCES.json',
                'app/FLATICON-LICENSE.txt', 'app/THIRD_PARTY.md', 'app/desktop/build.ps1', 'app/desktop/prepare-runtime.ps1')
RUNTIME_FOLDERS = ('app/desktop/bin', 'runtime/python', 'runtime/dotnet', 'runtime/installers')


def distribution_files(root):
    root = Path(root)
    result = {x for x in ROOT_FILES | set(SOURCE_FILES) if (root/x).is_file()}
    for folder in SOURCE_FOLDERS + RUNTIME_FOLDERS:
        for path in (root/folder).rglob('*'):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(p.startswith('.') or p in {'obj', 'bin', '__pycache__', 'checks', 'archive'} for p in path.relative_to(root/folder).parts):
                continue
            if path.suffix.lower() in {'.pyc', '.pyo', '.log', '.zip', '.tmp'} and not rel.startswith('runtime/python/'):
                continue
            # The Python standard library ZIP is part of the runtime, not a generated build archive.
            if path.suffix.lower() in {'.pyc', '.pyo', '.log', '.tmp'}:
                continue
            owned_path(rel)
            result.add(rel)
    if not REQUIRED <= result:
        raise ValueError('Неполная переносимая сборка: '+', '.join(sorted(REQUIRED-result)))
    return sorted(result)


def build(root, out, build_id, build_number, notes=''):
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    version = (root/'app/VERSION.txt').read_text(encoding='utf-8').strip()
    files = distribution_files(root)
    manifest = {'schema': 1, 'channel': 'dev', 'platform': 'win-x64', 'version': version,
                'build_id': build_id, 'build_number': int(build_number),
                'files': [{'path': rel, 'bytes': (root/rel).stat().st_size, 'sha256': sha256(root/rel)} for rel in files]}
    write_json(root/'app/release_manifest.json', manifest)
    name = f'RenPyTranslator_{version.replace(".", "_")}_dev_win64.zip'
    archive = out/name
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel in files + ['app/release_manifest.json']:
            z.write(root/rel, 'RenPyTranslator/'+rel)
    descriptor = {k: manifest[k] for k in ('schema', 'channel', 'platform', 'version', 'build_id', 'build_number')}
    descriptor.update(min_updater=1, bytes=archive.stat().st_size, sha256=sha256(archive),
                      unpacked_bytes=sum(x['bytes'] for x in manifest['files'])+(root/'app/release_manifest.json').stat().st_size,
                      package_url=f'https://github.com/UFrimNe/RenPyTranslator/releases/download/dev-{build_number}/{name}', notes=notes)
    write_json(out/'dev-update.json', descriptor)
    return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--out', type=Path, default=Path('dist'))
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--build-number', type=int, required=True)
    parser.add_argument('--notes', default='Исправления и улучшения из ветки dev. Подробности — в истории GitHub.')
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.out, args.build_id, args.build_number, args.notes), ensure_ascii=False))
