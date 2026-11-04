#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description='Inspect an RPTS Stage 7 translation project database')
    ap.add_argument('db', nargs='?', default='reports/translation_project.sqlite')
    ns = ap.parse_args()
    path = Path(ns.db)
    if not path.is_file():
        raise SystemExit(f'Project database not found: {path}')
    db = sqlite3.connect(path)
    try:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        total = db.execute('select count(*) from segments').fetchone()[0]
        status = dict(db.execute('select status,count(*) from segments group by status').fetchall())
        channels = dict(db.execute('select channel,count(*) from segments group by channel').fetchall())
        origins = dict(db.execute('select origin,count(*) from segments group by origin').fetchall())
        ids = db.execute("select count(*) from segments where translation_id <> ''").fetchone()[0]
        exact_ids = db.execute("select count(*) from segments where translation_id_source in ('ast_translate','ast_translate_say')").fetchone()[0]
        generated_ids = db.execute("select count(*) from segments where translation_id_source like 'generated_%'").fetchone()[0]
        tm = db.execute('select count(*) from translation_memory').fetchone()[0]
        tm_lang = dict(db.execute('select language,count(*) from translation_memory group by language').fetchall())
        dlg_tl = db.execute('select count(*) from dialogue_translations').fetchone()[0]
        dlg_tl_lang = dict(db.execute('select language,count(*) from dialogue_translations group by language').fetchall())
        runtime = db.execute('select count(*) from runtime_targets').fetchone()[0]
        print('RPTS Stage 7 project database')
        print(f'Path: {path.resolve()}')
        print(f'Integrity: {integrity}')
        print(f'Segments: {total}')
        print(f'Statuses: {status}')
        print(f'Channels: {channels}')
        print(f'Origins: {origins}')
        print(f'RenPy translation IDs: {ids} total / {exact_ids} exact AST / {generated_ids} generated fallback')
        print(f'String translation memory: {tm} pairs / languages {tm_lang}')
        print(f'Existing dialogue translations: {dlg_tl} / languages {dlg_tl_lang}')
        print(f'Runtime targets: {runtime}')
        print('\nSample dialogue IDs:')
        rows = db.execute("select segment_id,translation_id,source_file,source_line,substr(source_text,1,80) from segments where translation_id <> '' order by source_file,source_line limit 8").fetchall()
        for r in rows:
            print(f'  {r[1]}  {r[2]}:{r[3]}  {r[4]}')
    finally:
        db.close()

if __name__ == '__main__':
    main()
