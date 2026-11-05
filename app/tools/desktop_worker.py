from __future__ import annotations
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

APP=Path(__file__).resolve().parents[1];ROOT=APP.parent
sys.path.insert(0,str(APP/'src'))
sys.path.insert(0,str(APP/'tools'))
from rpts.workspace import Workspace,OperationLock,read_json,write_json
from rpts.runtime_control import check_cancelled
from rpts import diagnostics


def select_translation_scope(config, *, ui_only=False):
    """GUI actions have explicit scopes, independent of saved CLI filters."""
    from rpts.ui_project import UI_KINDS
    selection = config.setdefault('translation', {})
    selection.update(channels=['semantic'], include_statuses=['pending', 'error'],
                     kinds=sorted(UI_KINDS) if ui_only else [], origins=[],
                     include_interactive=True)
    selection['segment_ids']=[]
    selection['refresh_legacy_ai']=False
    selection.pop('repair_scope',None)
    return config


def emit(event_type,**data):
    print('@@RPTS '+json.dumps({'type':event_type,**data},ensure_ascii=False),flush=True)

def phase(name):
    check_cancelled();emit('phase',label=name)

def prompt(message):
    emit('input',prompt=message.strip())
    answer=sys.stdin.readline()
    if not answer:raise KeyboardInterrupt()
    data=json.loads(answer)
    if data.get('cancel'):raise KeyboardInterrupt()
    return str(data.get('value',''))

def run(task):
    ws=Workspace(ROOT).initialize()
    with OperationLock(ws.data/'operation.lock'):
        identity=task.get('project_id','')
        if identity:
            ws.project(identity);os.environ['RPTS_PROJECT_ID']=identity
        import rpts_menu as menu
        menu._input=prompt;menu._pause=lambda:None;menu._clear=lambda:None
        menu._activate_project_workspace()
        diagnostics.begin_session(ws.data,'RPTS Desktop '+(APP/'VERSION.txt').read_text().strip())
        diagnostics.event('desktop_task',task=task)
        action=task['action'];params=task.get('params') or {}
        check_cancelled()
        def need_project():
            if not identity:raise ValueError('Сначала выбери игру.')
        def ready_ai():
            from rpts import local_ai as ai
            settings=read_json(menu.LOCAL_AI_SETTINGS)
            if settings.get('model_key') not in ai.MODEL_SPECS:
                settings=menu._load_local_ai_settings()
            if settings.get('model_key') not in ai.MODEL_SPECS:
                hw=ai.detect_hardware();settings.update(model_key=ai.recommend_model(hw).key,backend=ai.recommended_backend(hw))
            if settings.get('backend') not in {'cpu','cuda','vulkan'}:settings['backend']=ai.recommended_backend(ai.detect_hardware())
            settings.setdefault('home',str(ws.ai));ai.save_settings(menu.LOCAL_AI_SETTINGS,settings)
            status=ai.installation_status(settings)
            if not status.get('engine_present'):
                phase('Загрузка движка локального ИИ');ai.install_engine(settings['home'],settings['backend'])
            if not status.get('model_present'):
                phase('Загрузка модели');ai.install_model(settings['home'],ai.MODEL_SPECS[settings['model_key']])
            phase('Подготовка настроек модели')
            actual=menu._ensure_local_ai_ready(simple=True)
            if not actual:raise RuntimeError('Не удалось подготовить модель. Открой журнал или собери диагностику.')
            return actual
        def ensure_project():
            need_project()
            if not menu.WORK_DB.exists():
                phase('Анализ файлов игры')
                if menu.audit(str(ws.game(identity))):raise RuntimeError('Не удалось прочитать игру.')
            if not menu.ensure_work():raise RuntimeError('Рабочая база не готова. Открой диагностику проекта.')
        def build():
            from rpts.patch_project import build_patch
            phase('Сборка русского перевода')
            patch=menu.WORK/'patch'
            manifest=build_patch(menu.WORK_DB,patch,overrides_path=menu._ensure_ui_overrides())
            saved=ws.save_translation(identity,patch)
            emit('artifact',kind='translation',id=saved)
            return patch,manifest
        if action=='install_model':
            ready_ai();return {'message':'Модель готова к локальному переводу.'}
        if action=='audit':
            need_project();phase('Анализ файлов игры')
            rc=menu.audit(str(ws.game(identity)))
            if rc or not menu.ensure_work():raise RuntimeError('Анализ игры не завершён. Подробности в журнале.')
            return {'message':'Игра проанализирована. Можно начать перевод.'}
        if action in {'translate','translate_ui','google_translate'}:
            from rpts.workspace import TRANSLATION_METHODS
            method='google_free' if action=='google_translate' else params.get('method') or read_json(ws.project(identity)/'project.json').get('translation_method','local_ai')
            if method not in TRANSLATION_METHODS:raise ValueError('Неизвестный способ перевода.')
            diagnostics.event('translation_method',method=method,ui_only=action=='translate_ui')
            if method=='google_free':
                from rpts.translation_engine import translate_project,load_config,reset_mock
                from rpts.patch_project import install_patch,launch_game
                from rpts.providers import ProviderRateLimitError,ProviderError
                ensure_project();menu._ensure_free_google_config()
                cfg=load_config(menu.FREE_USER_CFG)
                # Google cannot use AI profiles, prompts or editorial passes.
                cfg['provider'].update(type='google_free',model='google-web-rpc')
                cfg['quality']={'enabled':False,'quality_dir':str(menu.LOCAL_AI_QUALITY),
                                'character_profiles_file':str(menu.LOCAL_AI_QUALITY/'character_profiles.json')}
                cfg['translation'].update(refresh_legacy_ai=False,concurrent_batches=1,stop_on_batch_error=True,
                                          use_existing_translation_memory=False,progress_every_batches=1)
                select_translation_scope(cfg,ui_only=action=='translate_ui')
                reset_mock(menu.WORK_DB);phase('Google — быстрый перевод')
                try:result=translate_project(menu.WORK_DB,cfg)
                except ProviderRateLimitError as exc:
                    return {'message':'Google временно ограничил запросы. Готовые строки сохранены. Продолжи позже.',
                            'warning':True,'rate_limited':True,'method':method,'detail':str(exc)}
                except ProviderError as exc:
                    raise RuntimeError('Google не ответил. Готовые строки сохранены; попробуй продолжить позже. Подробности: '+str(exc)) from exc
                check_cancelled()
                info=read_json(ws.project(identity)/'project.json')
                info.update(model='Google Translate',updated_at=time.time());write_json(ws.project(identity)/'project.json',info)
                if result.get('errors'):
                    return {'message':'Готовые строки сохранены. Остались ошибки; нажми «Продолжить перевод».','warning':True,'method':method,**result}
                patch,manifest=build()
                if params.get('install',True):
                    phase('Установка перевода');install_patch(patch,ws.game(identity),backup_dir=menu.LOCAL_AI_BACKUPS/'installed')
                if params.get('launch',False) and params.get('install',True):
                    phase('Запуск игры');launch_game(ws.game(identity))
                return {'message':'Google-перевод сохранён в библиотеке.','method':method,**result}
        if action in {'translate','translate_ui','profiles'}:
            ensure_project();settings=ready_ai();server=None
            from rpts.local_ai import prepare_runtime_server,start_server,stop_server,detect_hardware
            from rpts.translation_engine import translate_project,load_config,reset_mock
            from rpts.patch_project import install_patch,launch_game
            try:
                phase('Запуск локального ИИ');runtime=prepare_runtime_server(settings)
                server=start_server(runtime,menu.LOCAL_AI_LOG,detect_hardware())
                check_cancelled();cfg=read_json(menu.LOCAL_AI_CFG)
                cfg['provider']['base_url']=f"http://127.0.0.1:{runtime['port']}/v1"
                cfg['provider']['api_key_env']='RPTS_LOCAL_AI_KEY'
                write_json(menu.LOCAL_AI_CFG,cfg);cfg=load_config(menu.LOCAL_AI_CFG)
                select_translation_scope(cfg,ui_only=action=='translate_ui')
                phase('Перевод и проверка строк' if action!='profiles' else 'Проверка персонажей')
                reset_mock(menu.WORK_DB)
                result=translate_project(menu.WORK_DB,cfg,profiles_only=action=='profiles')
                check_cancelled()
                project=ws.project(identity);info=read_json(project/'project.json')
                info.update(game_dir=str(ws.game(identity)),model=settings['model_key'],updated_at=time.time());write_json(project/'project.json',info)
                if action=='profiles':return {'message':'Профили персонажей обновлены.',**result}
                if result.get('errors'):
                    return {'message':'Готовые строки сохранены. Остались ошибки; их можно повторить кнопкой «Продолжить».','warning':True,**result}
                patch,manifest=build()
                if params.get('install',True):
                    phase('Установка перевода');install_patch(patch,ws.game(identity),backup_dir=menu.LOCAL_AI_BACKUPS/'installed')
                if params.get('launch',False):
                    phase('Запуск игры');launch_game(ws.game(identity))
                return {'message':'Перевод сохранён в библиотеке.','translated':result.get('translated',0),'interactive_untranslated':manifest.get('interactive_untranslated',0)}
            finally:stop_server(server)
        if action=='build':
            ensure_project();_,manifest=build();return {'message':'Версия перевода сохранена в библиотеке.',**manifest}
        if action=='install':
            need_project();from rpts.patch_project import install_patch
            translation=next((x for x in ws.translations() if x['id']==params.get('translation_id')),None)
            if not translation or translation['project_id']!=identity:raise ValueError('Этот перевод относится к другой игре.')
            phase('Установка выбранной версии')
            return {'message':'Перевод установлен.',**install_patch(ws.translation_path(translation['id']),ws.game(identity),backup_dir=menu.LOCAL_AI_BACKUPS/'installed')}
        if action=='import_translation':
            need_project();phase('Проверка готового перевода')
            saved=ws.save_translation(identity,Path(params['path']))
            return {'message':'Перевод добавлен в библиотеку.','translation_id':saved}
        if action=='report':
            phase('Сборка диагностического ZIP')
            if menu.pack_report():raise RuntimeError('Не удалось собрать отчёт.')
            return {'message':'Диагностический ZIP готов.','report':str(ws.reports/'RenPyAuditReport.zip')}
        if action=='launch':
            need_project()
            from rpts.patch_project import launch_game,install_patch
            from rpts.workspace import counts
            install_current=params.get('install_current',False)
            if not isinstance(install_current,bool):
                raise ValueError('Некорректный вариант запуска игры.')
            result={}
            if install_current:
                ensure_project()
                progress=counts(menu.WORK_DB)
                if not progress['ready']:
                    raise ValueError('Пока нет готовых строк. Выбери «Просто запустить игру».')
                patch,manifest=build()
                if not manifest['translated_rows']:
                    raise ValueError('В рабочей базе нет строк, пригодных для установки.')
                phase('Установка текущего перевода')
                installed=install_patch(patch,ws.game(identity),backup_dir=menu.LOCAL_AI_BACKUPS/'installed')
                result.update(installed=installed,translated_rows=manifest['translated_rows'],
                              total=progress['total'],partial=progress['ready']<progress['total'])
            phase('Запуск игры')
            result.update(message='Текущий перевод установлен. Игра запущена.' if install_current else 'Игра запущена.',
                          launched=launch_game(ws.game(identity)))
            return result
        actions={'check':menu.check_project,'capture_on':menu.ui_enable_capture,'capture_off':menu.ui_disable_capture,
                 'import_runtime':menu.ui_import_runtime,'coverage':menu.ui_coverage,'uninstall':menu.uninstall_patch,
                 'retranslate':menu.local_ai_retranslate_all,'restore':menu.local_ai_restore_backup,
                 'evaluate':menu.local_ai_evaluate_russian,'unrpyc':menu.install_unrpyc,
                 }
        if action in actions:
            if action!='unrpyc':need_project()
            phase('Выполняю выбранную операцию')
            if actions[action]():raise RuntimeError('Операция завершилась с ошибкой. Открой журнал.')
            return {'message':'Операция завершена.'}
        raise ValueError('Неизвестная операция.')

if __name__=='__main__':
    try:
        task=json.loads(sys.stdin.readline())
        result=run(task);emit('result',result=result)
    except KeyboardInterrupt:
        emit('cancelled',message='Остановлено. Готовые строки сохранены.');raise SystemExit(130)
    except Exception as e:
        import traceback
        traceback.print_exc();diagnostics.exception('desktop_worker_error')
        emit('failure',message=str(e));raise SystemExit(1)
