from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import traceback

from .interactive import bundle_from_source, extract_messages, message_segments
from .compiled import BackendUnavailable, extract_compiled, extract_compiled_bytes, unrpyc_available
from .rpa import open_rpa, script_entries, RpaError
from .parser import RenPySourceParser, discover_character_symbols
from .logical import iter_logical_statements
from .tokens import scan_protected_tokens
from .russian import visible_text
from .multilingual import (
    interpolation_expression_rows, analyze_multilingual, build_translation_pairs, build_translation_queue, script_counts, infer_variant_roles, classify_dynamic_rows,
)
from .data_flow import analyze_dataflow, runtime_probe_source, is_probable_input_key_label
from .project_db import (
    build_project_segments, translation_id_coverage, write_project_database, write_project_segments_csv,
)

SOURCE_EXT = {".rpy", ".rpym"}
COMPILED_EXT = {".rpyc", ".rpymc"}
ALL_EXT = SOURCE_EXT | COMPILED_EXT


def _tl_language_from_report_path(rel: str):
    # Handles both direct paths (tl/EN/foo.rpyc) and RPA virtual paths
    # (archive.rpa::tl/EN/foo.rpyc). Preserve the language folder spelling.
    virtual = rel.split("::", 1)[-1].replace("\\", "/")
    parts = [p for p in virtual.split("/") if p]
    lower = [p.lower() for p in parts]
    if "tl" in lower:
        i = lower.index("tl")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _row_layer(row, tl_language):
    if tl_language is not None:
        return "translation"
    if row.get("language") not in (None, ""):
        return "translation"
    if str(row.get("kind", "")).startswith("translation_"):
        return "translation"
    return "source"


def resolve_game_dir(path: Path) -> Path:
    path = path.resolve()
    if path.is_dir() and path.name.lower() == "game":
        return path
    candidate = path / "game"
    if candidate.is_dir():
        return candidate
    return path


def discover_files(game_dir: Path, *, prefer_compiled=False, include_shadowed=False, exclude_tl=False):
    files = [p for p in game_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALL_EXT]
    if exclude_tl:
        files = [p for p in files if "tl" not in {x.lower() for x in p.relative_to(game_dir).parts[:-1]}]
    if include_shadowed:
        return sorted(files)

    groups = {}
    for p in files:
        rel = p.relative_to(game_dir)
        key = str(rel.with_suffix(""))
        groups.setdefault(key, []).append(p)

    selected = []
    for paths in groups.values():
        sources = [p for p in paths if p.suffix.lower() in SOURCE_EXT]
        compiled = [p for p in paths if p.suffix.lower() in COMPILED_EXT]
        if prefer_compiled and compiled:
            selected.extend(compiled)
        elif sources:
            selected.extend(sources)
        else:
            selected.extend(compiled)
    return sorted(selected)


def discover_archive_scripts(game_dir: Path, direct_paths, *, prefer_compiled=False, exclude_tl=False):
    direct_stems = {str(p.relative_to(game_dir).with_suffix("")).replace("\\", "/") for p in direct_paths}
    selected = []
    archive_errors = []
    seen_stems = set(direct_stems)
    archives = sorted(game_dir.rglob("*.rpa"))
    for archive_path in archives:
        try:
            arc = open_rpa(archive_path)
            groups = {}
            for entry in script_entries(arc):
                name_path = Path(entry.name)
                if exclude_tl and "tl" in {x.lower() for x in name_path.parts[:-1]}:
                    continue
                stem = str(name_path.with_suffix("")).replace("\\", "/")
                groups.setdefault(stem, []).append(entry)
            for stem, entries in groups.items():
                if stem in seen_stems:
                    continue
                sources = [e for e in entries if Path(e.name).suffix.lower() in SOURCE_EXT]
                compiled = [e for e in entries if Path(e.name).suffix.lower() in COMPILED_EXT]
                chosen = (compiled if prefer_compiled and compiled else sources if sources else compiled)
                for entry in chosen:
                    selected.append((arc, entry))
                if chosen:
                    seen_stems.add(stem)
        except Exception as e:
            archive_errors.append((archive_path, e))
    return selected, archive_errors, archives


def _tokens(items):
    count = Counter()
    for x in items:
        if x.text:
            count.update(t.kind for t in scan_protected_tokens(x.text))
    return dict(sorted(count.items()))


def audit_game(
    target: str | Path,
    *,
    backend="auto",
    reports_dir="reports",
    try_harder=False,
    prefer_compiled=False,
    include_shadowed=False,
    exclude_tl=False,
    unrpyc_path=None,
    max_files=None,
    progress=True,
    multilingual_config=None,
):
    target = Path(target)
    game_dir = resolve_game_dir(target)
    reports = Path(reports_dir)
    if not reports.is_absolute():
        reports = Path.cwd() / reports
    reports.mkdir(parents=True, exist_ok=True)

    paths = discover_files(
        game_dir,
        prefer_compiled=prefer_compiled,
        include_shadowed=include_shadowed,
        exclude_tl=exclude_tl,
    )
    archived, archive_open_errors, archives_found = discover_archive_scripts(
        game_dir, paths, prefer_compiled=prefer_compiled, exclude_tl=exclude_tl
    )
    work = [("file", p) for p in paths] + [("rpa", pair) for pair in archived]
    if max_files is not None:
        work = work[:max_files]

    parser = RenPySourceParser()
    file_reports = []
    flat_items = []
    source_texts = {}
    interactive_bundles = {}
    errors = []
    backend_counts = Counter()
    extraction_warnings = []

    for archive_path, e in archive_open_errors:
        errors.append({
            "path": str(archive_path.relative_to(game_dir)),
            "error": f"RPA open failed: {type(e).__name__}: {e}",
            "traceback": "",
        })

    total = len(work)
    for i, (work_kind, obj) in enumerate(work, 1):
        if work_kind == "file":
            path = obj
            rel = str(path.relative_to(game_dir))
            ext = path.suffix.lower()
        else:
            arc, entry = obj
            rel = f"{arc.path.relative_to(game_dir)}::{entry.name}"
            ext = Path(entry.name).suffix.lower()
        if progress:
            print(f"[{i}/{total}] {rel}")
        tl_language = _tl_language_from_report_path(rel)
        try:
            if ext in SOURCE_EXT:
                raw = path.read_bytes() if work_kind == "file" else arc.read(entry.name)
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    text = raw.decode("utf-8-sig", errors="replace")
                    extraction_warnings.append({"file": rel, "code": "encoding_replacement",
                                                "line": raw[:exc.start].count(b"\n") + 1, "column": 0,
                                                "message": "Повреждён UTF-8: часть символов заменена при чтении файла."})
                # Defer source recognition until Character declarations from all
                # files are known. Ren'Py store bindings routinely cross files.
                items = []
                source_texts[rel] = text
                meta = {"backend": "source", "chars": len(text), "from_archive": work_kind == "rpa"}
                if work_kind == "rpa":
                    meta["archive_version"] = arc.version
            else:
                if work_kind == "file":
                    items, meta = extract_compiled(
                        path,
                        backend=backend,
                        try_harder=try_harder,
                        unrpyc_path=unrpyc_path,
                        defer_source=True,
                    )
                else:
                    items, meta = extract_compiled_bytes(
                        arc.read(entry.name), suffix=ext, backend=backend,
                        try_harder=try_harder, unrpyc_path=unrpyc_path,
                        defer_source=True,
                    )
                    meta["archive_version"] = arc.version
                    meta["archive"] = str(arc.path.relative_to(game_dir))
            bundle = meta.pop("_interactive_bundle", None)
            if tl_language is None:
                interactive_bundles[rel] = bundle or bundle_from_source(source_texts.get(rel, ""))
            memory_source = meta.pop("_memory_source", None)
            if memory_source:
                source_texts[rel] = memory_source
            backend_counts[meta.get("backend", "unknown")] += 1
            if tl_language is not None:
                meta["tl_language"] = tl_language
            kinds = Counter(x.kind for x in items)
            file_reports.append({
                "path": rel,
                "extension": ext,
                "count": len(items),
                "kinds": dict(sorted(kinds.items())),
                "protected_tokens": _tokens(items),
                "meta": meta,
            })
            for c in items:
                row = c.to_dict()
                row["file"] = rel
                if tl_language is not None and row.get("language") in (None, ""):
                    row["language"] = tl_language
                row["layer"] = _row_layer(row, tl_language)
                flat_items.append(row)
        except Exception as e:
            errors.append({
                "path": rel,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })

    project_statements = [st for rel, text in source_texts.items()
                          if _tl_language_from_report_path(rel) is None
                          for st in iter_logical_statements(text)]
    symbols = discover_character_symbols(project_statements)
    failed_source_paths = set()
    rows_by_file = defaultdict(list)
    for row in flat_items:
        rows_by_file[row["file"]].append(row)
    for file_report in file_reports:
        rel = file_report["path"]
        file_rows = rows_by_file[rel]
        text = source_texts.get(rel)
        if text is not None:
            try:
                is_source = file_report["meta"]["backend"] == "source"
                recognizer = parser if is_source else RenPySourceParser(infer_say=False, infer_menu=False)
                items = recognizer.parse(text, character_symbols=symbols)
                tl_language = _tl_language_from_report_path(rel)
                existing = {(r["kind"], r.get("text"), r["line"], r.get("speaker"), r.get("dynamic"),
                             r.get("expression"), r.get("language")) for r in file_rows}
                for c in items:
                    row = c.to_dict()
                    if tl_language is not None and row.get("language") in (None, ""):
                        row["language"] = tl_language
                    key = (row["kind"], row.get("text"), row["line"], row.get("speaker"), row.get("dynamic"),
                           row.get("expression"), row.get("language"))
                    if key in existing:
                        continue
                    existing.add(key)
                    row["file"] = rel
                    row["layer"] = _row_layer(row, tl_language)
                    flat_items.append(row)
                    file_rows.append(row)
                extraction_warnings.extend(dict(warning, file=rel) for warning in recognizer.diagnostics)
            except Exception as exc:
                failed_source_paths.add(rel)
                errors.append({"path": rel, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        for limitation in file_report["meta"].get("limitations", []):
            extraction_warnings.append({"file": rel, "code": "limited_backend", "line": 0, "column": 0,
                                        "message": limitation})
        file_report["count"] = len(file_rows)
        file_report["kinds"] = dict(sorted(Counter(r["kind"] for r in file_rows).items()))
        file_report["protected_tokens"] = dict(sorted(Counter(
            t.kind for r in file_rows if r.get("text") for t in scan_protected_tokens(r["text"])).items()))
    file_reports = [r for r in file_reports if r["path"] not in failed_source_paths]
    backend_counts = Counter(r["meta"]["backend"] for r in file_reports)
    warnings_by_file = Counter(w["file"] for w in extraction_warnings)
    for file_report in file_reports:
        file_report["meta"]["warning_count"] = warnings_by_file[file_report["path"]]

    kind_counts = Counter(x["kind"] for x in flat_items)
    token_counts = Counter()
    for f in file_reports:
        token_counts.update(f["protected_tokens"])

    error_groups = Counter(e["error"] for e in errors)
    source_items = [x for x in flat_items if x.get("layer") == "source"]
    translation_items = [x for x in flat_items if x.get("layer") == "translation"]
    translation_languages = Counter(x.get("language") or "unknown" for x in translation_items)

    explicit_multilingual = {}
    if multilingual_config:
        cfg_path = Path(multilingual_config)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        explicit_multilingual = cfg.get("functions", cfg) if isinstance(cfg, dict) else {}
        if not isinstance(explicit_multilingual, dict):
            raise ValueError("multilingual config must contain a JSON object of functions")

    translation_queue, localization_queue, queue_skipped = build_translation_queue(source_items)
    source_dynamic = [x for x in source_items if x.get("dynamic")]
    ml_stats, ml_calls, ml_variants, dynamic_unresolved = analyze_multilingual(
        source_dynamic + interpolation_expression_rows(source_items), explicit_functions=explicit_multilingual
    )
    translation_pairs = build_translation_pairs(translation_items)
    dynamic_analysis = classify_dynamic_rows(dynamic_unresolved)
    dataflow_helpers = {x["function"] for x in ml_stats if x.get("confidence") == "high"}
    dataflow_resolved, dataflow_unresolved, dataflow_sources = analyze_dataflow(
        dynamic_unresolved, source_texts, multilingual_helpers=dataflow_helpers
    )

    # Stage 4.1: unresolved display expressions are not synonymous with missing
    # translatable content. Separate renderer pass-through, runtime metadata and
    # developer-only text from the small set that actually needs runtime content
    # observation. This prevents Say/Menu text from being translated twice.
    dynamic_already_covered = [x for x in dataflow_unresolved if x.get("dynamic_disposition") == "already_covered"]
    runtime_localization = [x for x in dataflow_unresolved if x.get("dynamic_disposition") == "runtime_localization"]
    runtime_metadata = [x for x in dataflow_unresolved if x.get("dynamic_disposition") == "runtime_metadata"]
    developer_only_dynamic = [x for x in dataflow_unresolved if x.get("dynamic_disposition") == "developer_only"]
    runtime_targets = [x for x in dataflow_unresolved if x.get("dynamic_disposition") == "runtime_content"]

    # Static strings proven through data-flow are real source candidates, but keep
    # key-name labels (Ctrl/Esc/Tab/...) out of semantic translation by default.
    dataflow_key_invariants = [x for x in dataflow_sources if is_probable_input_key_label(x)]
    dataflow_language_sources = [x for x in dataflow_sources if not is_probable_input_key_label(x)]
    dataflow_translation_queue, dataflow_localization_queue, dataflow_invariant = build_translation_queue(dataflow_language_sources)
    dataflow_invariant = dataflow_invariant + [dict(x, queue_reason="input_key_label") for x in dataflow_key_invariants]

    # Exact-text inventory used to post-filter optional runtime probe logs.
    # Keep filtering outside the game: the probe remains tiny and low-risk, while
    # the audit can distinguish already-known AST/UI/TM text from genuinely new
    # runtime content afterwards.
    runtime_known_map = {}
    def _remember_runtime_known(text, layer, kind):
        if not isinstance(text, str) or not text:
            return
        rec = runtime_known_map.setdefault(text, {"text": text, "occurrences": 0, "layers": set(), "kinds": set()})
        rec["occurrences"] += 1
        if layer: rec["layers"].add(str(layer))
        if kind: rec["kinds"].add(str(kind))
    for row in flat_items:
        if not row.get("dynamic"):
            _remember_runtime_known(row.get("text"), row.get("layer"), row.get("kind"))
    for row in ml_variants:
        _remember_runtime_known(row.get("text"), row.get("layer") or "source", "multilingual_variant")
    for row in dataflow_sources:
        _remember_runtime_known(row.get("text"), "source", "dataflow")
    runtime_known_inventory = []
    for rec in runtime_known_map.values():
        runtime_known_inventory.append({
            "text": rec["text"],
            "occurrences": rec["occurrences"],
            "layers": " | ".join(sorted(rec["layers"])),
            "kinds": " | ".join(sorted(rec["kinds"])),
        })
    runtime_known_inventory.sort(key=lambda x: x["text"])

    script_chars = Counter()
    for row in translation_queue:
        script_chars.update(script_counts(visible_text(str(row.get("text") or ""))))
    dominant_source_script = script_chars.most_common(1)[0][0] if script_chars else None
    ml_variants, ml_source_variant_guess = infer_variant_roles(ml_variants, dominant_source_script)
    for st in ml_stats:
        if st["function"] in ml_source_variant_guess:
            st["source_variant_guess"] = ml_source_variant_guess[st["function"]]
        else:
            st["source_variant_guess"] = ""

    # Stage 5: a stable project representation that can be used by providers,
    # editors and patch generation without rescanning the game. Dialogue identity
    # prefers Ren'Py translation identifiers when the compiled AST exposes them.
    id_stats, id_coverage_rows = translation_id_coverage(source_items, translation_items)
    project_segments = build_project_segments(
        translation_queue, localization_queue, dataflow_translation_queue,
        dataflow_localization_queue, ml_variants, runtime_targets, translation_pairs=translation_pairs,
    )

    character_declarations = [r for r in source_items if r.get("kind") == "character_name"]
    known_declarations = {r.get("source") for r in character_declarations}
    for file, bundle in interactive_bundles.items():
        for declaration in bundle["declarations"]:
            if declaration not in known_declarations:
                character_declarations.append({"source": declaration, "file": file})
                known_declarations.add(declaration)
    interactive_inventory = extract_messages(interactive_bundles)
    interactive_rows = message_segments(interactive_inventory)
    interactive_sources = {r["source_text"] for r in interactive_rows}
    project_segments = [r for r in project_segments if not (r["kind"] == "localized_string" and r["source_text"] in interactive_sources)] + interactive_rows
    (reports / "interactive_messages.json").write_text(json.dumps(interactive_inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "interactive_messages": len(interactive_rows),
        "interactive_debug_messages": sum(bool(m["debug"]) for m in interactive_inventory["messages"]),
        "interactive_unresolved": len(interactive_inventory["unresolved"]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": str(target.resolve()),
        "game_dir": str(game_dir),
        "requested_backend": backend,
        "unrpyc_available": unrpyc_available(unrpyc_path),
        "try_harder": bool(try_harder),
        "files_discovered": total,
        "archives_found": len(archives_found),
        "archive_scripts_selected": len(archived),
        "files_ok": len(file_reports),
        "files_failed": len(errors),
        "extraction_warning_count": len(extraction_warnings),
        "files_with_warnings": len({w["file"] for w in extraction_warnings}),
        "parser_errors": sum(w["code"] in {"unterminated_string", "unclosed_group", "encoding_replacement"} for w in extraction_warnings),
        "limited_backend_files": len({w["file"] for w in extraction_warnings if w["code"] == "limited_backend"}),
        "error_groups": dict(error_groups.most_common()),
        "backend_files": dict(sorted(backend_counts.items())),
        "candidates": len(flat_items),
        "source_candidates": len(source_items),
        "existing_translation_candidates": len(translation_items),
        "translation_languages": dict(sorted(translation_languages.items())),
        "kinds": dict(sorted(kind_counts.items())),
        "protected_tokens": dict(sorted(token_counts.items())),
        "dynamic_candidates": sum(1 for x in flat_items if x.get("dynamic")),
        "translation_queue": len(translation_queue),
        "localization_queue": len(localization_queue),
        "queue_skipped": len(queue_skipped),
        "multilingual_calls": len(ml_calls),
        "multilingual_variants": len(ml_variants),
        "dynamic_unresolved": len(dynamic_unresolved),
        "dynamic_classes": dict(Counter(x.get("dynamic_class") for x in dynamic_analysis)),
        "dataflow_resolved": len(dataflow_resolved),
        "dataflow_unresolved": len(dataflow_unresolved),
        "dataflow_resolution_kinds": dict(Counter(x.get("resolution_kind") for x in dataflow_resolved)),
        "dataflow_multilingual_helpers": sorted(dataflow_helpers),
        "dynamic_already_covered": len(dynamic_already_covered),
        "runtime_localization": len(runtime_localization),
        "runtime_metadata": len(runtime_metadata),
        "developer_only_dynamic": len(developer_only_dynamic),
        "runtime_targets": len(runtime_targets),
        "runtime_known_inventory": len(runtime_known_inventory),
        "dynamic_dispositions": dict(Counter(x.get("dynamic_disposition") for x in dataflow_resolved + dataflow_unresolved)),
        "dataflow_translation_queue": len(dataflow_translation_queue),
        "dataflow_localization_queue": len(dataflow_localization_queue),
        "dataflow_invariant": len(dataflow_invariant),
        "translation_pairs": len(translation_pairs),
        "project_segments": len(project_segments),
        "project_pending_semantic": sum(1 for x in project_segments if x.get("channel") == "semantic" and x.get("status") == "pending"),
        "project_pending_localization": sum(1 for x in project_segments if x.get("channel") == "localization" and x.get("status") == "pending"),
        "project_runtime_pending": sum(1 for x in project_segments if x.get("status") == "runtime_pending"),
        "translation_id_stats": id_stats,
        "translation_id_coverage": id_coverage_rows,
        "source_script_characters": dict(sorted(script_chars.items())),
        "dominant_source_script": dominant_source_script,
        "multilingual_source_variant_guess": ml_source_variant_guess,
        "multilingual_functions": {x["function"]: x["confidence"] for x in ml_stats if x["confidence"] != "none"},
    }
    report = {"summary": summary, "files": file_reports, "errors": errors, "warnings": extraction_warnings, "items": flat_items}

    (reports / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["file", "layer", "kind", "text", "line", "column", "speaker", "dynamic", "expression", "language", "source", "label", "translation_id", "translation_id_source", "translation_id_alternate", "say_code"]
    def write_rows(name, rows):
        with (reports / name).open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    write_rows("candidates.csv", flat_items)
    write_rows("source_candidates.csv", source_items)
    write_rows("existing_translations.csv", translation_items)

    def write_dict_rows(name, rows, fieldnames):
        with (reports / name).open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    queue_fields = fields + ["script_hint", "queue_reason"]
    write_dict_rows("extraction_warnings.csv", extraction_warnings, ["file", "line", "column", "code", "message"])
    write_dict_rows("translation_queue.csv", translation_queue, queue_fields)
    write_dict_rows("localization_queue.csv", localization_queue, queue_fields)
    write_dict_rows("queue_skipped.csv", queue_skipped, queue_fields)
    write_dict_rows(
        "multilingual_functions.csv", ml_stats,
        ["function", "calls", "eligible_static_variant_calls", "eligible_ratio",
         "distinct_script_pair_calls", "distinct_script_ratio", "name_hint", "confidence", "reason", "source_variant_guess"]
    )
    write_dict_rows(
        "multilingual_calls.csv", ml_calls,
        ["call_id", "function", "confidence", "variant_count", "kind", "file", "line", "layer", "expression", "call_expression", "tail"]
    )
    write_dict_rows(
        "multilingual_variants.csv", ml_variants,
        ["call_id", "function", "confidence", "variant_index", "text", "script_hint", "role_guess", "kind", "file", "line", "layer"]
    )
    write_dict_rows("dynamic_unresolved.csv", dynamic_unresolved, fields)
    write_dict_rows("dynamic_analysis.csv", dynamic_analysis, fields + ["dynamic_class", "recommended_next_step"])
    dataflow_fields = fields + [
        "normalized_expression", "dataflow_status", "resolution_kind", "provenance",
        "resolved_count", "resolved_values", "screen_name", "dynamic_disposition",
        "disposition_reason", "source_context",
    ]
    write_dict_rows("dataflow_resolved.csv", dataflow_resolved, dataflow_fields)
    write_dict_rows("dataflow_unresolved.csv", dataflow_unresolved, dataflow_fields)
    dataflow_source_fields = [
        "file", "line", "kind", "expression", "normalized_expression", "resolution_kind",
        "provenance", "text", "screen_name", "source_context", "layer", "dynamic",
        "speaker", "column", "language", "source", "label", "translation_id", "translation_id_source", "translation_id_alternate", "say_code",
    ]
    write_dict_rows("dataflow_sources.csv", dataflow_sources, dataflow_source_fields)
    write_dict_rows("dynamic_already_covered.csv", dynamic_already_covered, dataflow_fields)
    write_dict_rows("runtime_localization.csv", runtime_localization, dataflow_fields)
    write_dict_rows("runtime_metadata.csv", runtime_metadata, dataflow_fields)
    write_dict_rows("developer_only_dynamic.csv", developer_only_dynamic, dataflow_fields)
    write_dict_rows("runtime_targets.csv", runtime_targets, dataflow_fields)
    write_dict_rows("runtime_known_inventory.csv", runtime_known_inventory, ["text", "occurrences", "layers", "kinds"])
    dataflow_queue_fields = dataflow_source_fields + ["script_hint", "queue_reason"]
    write_dict_rows("dataflow_translation_queue.csv", dataflow_translation_queue, dataflow_queue_fields)
    write_dict_rows("dataflow_localization_queue.csv", dataflow_localization_queue, dataflow_queue_fields)
    write_dict_rows("dataflow_invariant.csv", dataflow_invariant, dataflow_queue_fields)
    write_project_segments_csv(reports / "project_segments.csv", project_segments)
    write_dict_rows(
        "translation_id_coverage.csv", id_coverage_rows,
        ["language", "source_ids", "translation_ids", "matched_ids", "source_match_ratio", "core_source_ids", "core_matched_ids", "core_source_match_ratio"],
    )
    write_project_database(
        reports / "translation_project.sqlite",
        segments=project_segments, translation_pairs=translation_pairs, dialogue_translations=translation_items,
        multilingual_variants=ml_variants, runtime_targets=runtime_targets,
        metadata={"game_dir": str(game_dir), "summary": summary, "created_by": "RPTS Stage 11.16", "interactive_inventory": interactive_inventory, "interactive_extraction_version": "11.16", "character_declarations": character_declarations},
    )
    (reports / "runtime_probe.rpy").write_text(runtime_probe_source(), encoding="utf-8")
    write_dict_rows(
        "translation_pairs.csv", translation_pairs,
        ["pair_id", "file", "language", "language_bucket", "old", "new", "old_line", "new_line", "occurrence_count", "identical", "old_script_hint", "new_script_hint"]
    )
    with (reports / "errors.log").open("w", encoding="utf-8") as f:
        for e in errors:
            f.write(f"=== {e['path']} ===\n{e['error']}\n{e['traceback']}\n")

    lines = [
        "RenPy Parser Test Suite - real game audit",
        f"Game dir: {game_dir}",
        f"Backend requested: {backend}",
        f"Unrpyc available: {summary['unrpyc_available']}",
        f"Files: {summary['files_ok']} OK / {summary['files_failed']} failed / {total} selected",
        f"Extraction warnings: {summary['extraction_warning_count']} / parser errors {summary['parser_errors']} / limited backend files {summary['limited_backend_files']}",
        f"RPA archives: {summary['archives_found']} found / {summary['archive_scripts_selected']} script entries selected",
        f"Candidates (all): {summary['candidates']}",
        f"Primary source candidates: {summary['source_candidates']}",
        f"Existing translation candidates: {summary['existing_translation_candidates']}",
        f"Translation languages: {summary['translation_languages']}",
        f"Dynamic: {summary['dynamic_candidates']}",
        f"Translation queue: {summary['translation_queue']} semantic / {summary['localization_queue']} localization-only / skipped {summary['queue_skipped']}",
        f"Multilingual calls: {summary['multilingual_calls']} / variants {summary['multilingual_variants']}",
        f"Dynamic unresolved: {summary['dynamic_unresolved']} / classes {summary['dynamic_classes']}",
        f"Data-flow: {summary['dataflow_resolved']} static-resolved / {summary['dataflow_unresolved']} unresolved raw / kinds {summary['dataflow_resolution_kinds']}",
        f"Data-flow multilingual helpers: {summary['dataflow_multilingual_helpers']}",
        f"Dynamic disposition: {summary['dynamic_dispositions']}",
        f"Runtime content targets: {summary['runtime_targets']} / already-covered {summary['dynamic_already_covered']} / localization {summary['runtime_localization']} / metadata {summary['runtime_metadata']} / developer-only {summary['developer_only_dynamic']}",
        f"Runtime known exact-text inventory: {summary['runtime_known_inventory']}",
        f"Data-flow source queue: {summary['dataflow_translation_queue']} semantic / {summary['dataflow_localization_queue']} localization-only / invariant {summary['dataflow_invariant']}",
        f"Translation pairs: {summary['translation_pairs']}",
        f"Project segments: {summary['project_segments']} / semantic {summary['project_pending_semantic']} / localization {summary['project_pending_localization']} / runtime-pending {summary['project_runtime_pending']}",
        f"RenPy translation ids: {summary['translation_id_stats']}",
        f"RenPy id language coverage: {summary['translation_id_coverage']}",
        f"Source script characters: {summary['source_script_characters']}",
        f"Dominant source script (hint only): {summary['dominant_source_script']}",
        f"Multilingual source variant guess: {summary['multilingual_source_variant_guess']}",
        f"Multilingual functions: {summary['multilingual_functions']}",
        f"Backends: {summary['backend_files']}",
        f"Kinds: {summary['kinds']}",
        f"Protected tokens: {summary['protected_tokens']}",
        f"Error groups: {summary['error_groups']}",
        "",
        "This command is read-only: it does not modify the game files.",
    ]
    (reports / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    return report, reports


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only audit of a real Ren'Py game directory")
    ap.add_argument("target", help="Game root or its game/ directory")
    ap.add_argument("--backend", choices=["auto", "native", "unrpyc"], default="auto")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--try-harder", action="store_true", help="Use unrpyc anti-obfuscation strategies")
    ap.add_argument("--prefer-compiled", action="store_true", help="If both .rpy and .rpyc exist, test compiled")
    ap.add_argument("--include-shadowed", action="store_true", help="Audit both source and compiled twins")
    ap.add_argument("--exclude-tl", action="store_true")
    ap.add_argument("--unrpyc-path")
    ap.add_argument("--max-files", type=int)
    ap.add_argument("--multilingual-config", help="Optional JSON mapping of known multilingual helper functions")
    ns = ap.parse_args(argv)
    report, out = audit_game(
        ns.target,
        backend=ns.backend,
        reports_dir=ns.reports,
        try_harder=ns.try_harder,
        prefer_compiled=ns.prefer_compiled,
        include_shadowed=ns.include_shadowed,
        exclude_tl=ns.exclude_tl,
        unrpyc_path=ns.unrpyc_path,
        max_files=ns.max_files,
    )
    s = report["summary"]
    print("\nDone")
    print(f"  Files: {s['files_ok']} OK, {s['files_failed']} failed")
    print(f"  Extraction warnings: {s['extraction_warning_count']}; parser errors: {s['parser_errors']}")
    if s['extraction_warning_count']:
        print("  Check extraction_warnings.csv for locations and extraction limits.")
    print(f"  Candidates: {s['candidates']}")
    print(f"  Backends: {s['backend_files']}")
    print(f"  Report: {out}")
    if s["files_failed"] or s["parser_errors"]:
        print(f"  Error groups: {s['error_groups']}")
        print("  Check errors.log for failed files.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
