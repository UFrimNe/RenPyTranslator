from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from .providers import (create_provider, ProviderError, ProviderContextLimitError,
    ProviderResponseFormatError, ProviderBatchIntegrityError)
from .translation_engine import (_call_provider_resilient, _prompt_items, _restore_provider_text,
    _system_prompt, _validate_quality_translation, _review_story, _repair_single_translation, _review_stages, load_config)
from .model_policy import model_family
from .scene_situation import index_scopes, situation_for


def evaluation_context(row,case,profiles,config):
    before=case.get('before',[{'kind':'narration','speaker':'','text':case['context']}] if case.get('context') else [])
    context={'before':[dict(r) for r in before],'after':[dict(r) for r in case.get('after',[])]}
    neighbours=[]
    for side in ('before','after'):
        for i,item in enumerate(context[side]):item.setdefault('id',f"{row['segment_id']}:{side}:{i}")
    for item in context['before']+[{'id':row['segment_id'],'text':row['source_text'],'kind':row['kind'],'speaker':row['speaker']}]+context['after']:
        neighbours.append({'segment_id':item['id'],'source_text':item['text'],'source_file':'evaluation',
            'source_line':len(neighbours)+1,'kind':item.get('kind') or ('dialogue' if item.get('speaker') else 'narration'),
            'speaker':item.get('speaker',''),'label':row['label'],'origin':row['origin'],'context_json':'{"context_scope":[]}'})
    by={'evaluation':neighbours};index={r['segment_id']:('evaluation',i) for i,r in enumerate(neighbours)};index_scopes(by)
    limit=int((config.get('provider') or {}).get('context_limit_tokens') or 6144)
    budget=0 if limit<=2048 else 240 if limit<=4096 else 640
    situation=case.get('situation_context') or situation_for(row,context,by,index,profiles,max_chars=budget)
    if situation:context['situation_context']=situation
    return context


def evaluate(config: dict, cases: list[dict], output_dir: str | Path, *, dry_run: bool = False) -> dict:
    """Fixed scenes for human evaluation; automatic checks are not a quality score."""
    if not cases or any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or not isinstance(c.get("source"), str) for c in cases):
        raise ValueError("Each evaluation case requires string id and source")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be unique")
    if str(config.get("target_language_code") or "").lower() != "ru":
        raise ValueError("This evaluation targets Russian only")
    provider = None if dry_run else create_provider(config["provider"])
    if provider is not None and provider.name == "mock":
        raise ValueError("A mock translation cannot evaluate model quality")
    output = Path(output_dir); output.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    for case in cases:
        row = {"segment_id":case["id"], "source_text":case["source"], "kind":case.get("kind", "dialogue"),
            "speaker":case.get("speaker", "character" if case.get("profile") else ""), "label":case["id"], "translation_id":case["id"], "source_line":2,
            "origin":case.get("origin", "static"),
            "reference_en": case.get("reference_en", "")}
        profiles = dict(case.get("profiles") or {})
        if case.get("profile"): profiles[row["speaker"] or "character"] = case["profile"]
        for role in ("viewpoint", "addressee"):
            if case.get(role):
                row[role] = case[role]
        if row["kind"] in {"narration", "centered"}:
            row["speaker"] = ""
            if case.get("profile") and not row.get("viewpoint"):
                row["viewpoint"] = {"speaker_id": "character", "gender": case["profile"].get("gender", "unknown")}
        context=evaluation_context(row,case,profiles,config)
        items, tokens = _prompt_items([row], {case["id"]:context}, profiles=profiles,
            en_by_id={case["id"]:case.get("reference_en", "")})
        translated = ""; error = ""; valid = "not_run"; fixes = []; fatal = False; draft = ""; repaired = False
        one_started = time.monotonic()
        if not dry_run:
            try:
                returned = _call_provider_resilient(provider, system_prompt=_system_prompt(config, items[0]["mode"]),
                    items=items, split_on_response_error=True)
                draft = returned[0]["text"]
                returned = _review_story(provider, config=config, mode=items[0]["mode"], items=items, returned=returned)
                if returned[0].get("_error"):
                    raise ProviderResponseFormatError(returned[0]["_error"])
                try:
                    translated, fixes = _restore_provider_text(row["source_text"], returned[0]["text"], tokens[case["id"]])
                    ok, error = _validate_quality_translation(row, translated, config, profiles)
                except ValueError as exc:
                    ok, error = False, str(exc)
                if not ok and config.get("quality", {}).get("repair_validation_errors"):
                    translated = _repair_single_translation(provider, config=config, row=row,
                        prompt_item=dict(items[0], draft=returned[0]["text"]), tokens=tokens[case["id"]], profiles=profiles, reason=error)
                    repaired = True
                    ok, error = _validate_quality_translation(row, translated, config, profiles)
                valid = "pass" if ok else "fail"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"; valid = "error"
                fatal = isinstance(exc, ProviderError) and not isinstance(exc, (ProviderContextLimitError, ProviderResponseFormatError, ProviderBatchIntegrityError))
        results.append({"id":case["id"], "feature":case.get("feature", ""), "context":case.get("context", ""),
            "source":case["source"], "reference_en":case.get("reference_en", ""), "profile":json.dumps(case.get("profile", {}),ensure_ascii=False),
            "draft":draft, "translation":translated, "repaired":repaired, "criteria":case.get("criteria", ""), "automatic_check":valid, "error":error,
            "token_fixes":"; ".join(fixes), "seconds":round(time.monotonic()-one_started,3) if not dry_run else "",
            "reported_translation":case.get("reported_translation", ""),
            "meaning_1_to_5":"", "naturalness_1_to_5":"", "voice_1_to_5":"", "emotion_1_to_5":"", "reviewer_notes":""})
        if not dry_run:
            print(f"[RU EVAL] {len(results)}/{len(cases)}: {case['id']} — {valid}", flush=True)
        if fatal:
            break
    with (output/"russian_evaluation.csv").open("w",newline="",encoding="utf-8-sig") as f:
        writer=csv.DictWriter(f,fieldnames=list(results[0])); writer.writeheader(); writer.writerows(results)
    summary={"dry_run":dry_run, "cases":len(cases), "cases_recorded":len(results), "automatic_pass":sum(r["automatic_check"]=="pass" for r in results),
        "automatic_fail_or_error":sum(r["automatic_check"] in {"fail","error"} for r in results),
        "model":config.get("provider",{}).get("model_identity") or config.get("provider",{}).get("model"),
        "prompt_profile":model_family(config.get("provider",{})), "review_stages":_review_stages(config),
        "seconds":round(time.monotonic()-started,3) if not dry_run else None,
        "note":"Automatic checks verify syntax and known mechanical defects. Human ratings are required for meaning, tone, profanity and voice.",
        "config":config}
    (output/"russian_evaluation.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="Fixed English/Chinese scenes for human assessment of Russian localization")
    parser.add_argument("--config", required=True)
    app = Path(__file__).resolve().parents[2]
    templates = app / "templates" if (app / "templates").is_dir() else app
    parser.add_argument("--cases",default=str(templates / "russian_evaluation_cases.json"))
    parser.add_argument("--out",default="work/quality/russian_evaluation")
    parser.add_argument("--dry-run",action="store_true",help="Create a review template without calling a model")
    args=parser.parse_args(argv)
    cases=json.loads(Path(args.cases).read_text(encoding="utf-8"))
    result=evaluate(load_config(args.config),cases,args.out,dry_run=args.dry_run)
    print(json.dumps({key:value for key,value in result.items() if key != "config"},ensure_ascii=False,indent=2))
    return 2 if result["automatic_fail_or_error"] else 0
