'use strict';
const $=s=>document.querySelector(s), e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>`<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${RPTS_ICONS[n]||RPTS_ICONS.info}"/></svg>`;
const b=(label,action,cls='',extra='')=>`<button class="button ${cls}" data-action="${action}" ${extra}><span class="button-content">${label}</span></button>`;
const navs=[['games','Библиотека','library'],['model','Модели и перевод','model'],['tasks','Задачи','download'],['settings','Настройки лаунчера','settings'],['help','Помощь и диагностика','help']];
let gameTab='overview',libraryTab='games',modelTab='catalog',settingsTab='folders',helpTab='guide',translationDraft=null;
let rowsData={rows:[],total:0},rowsLoading=false,rowsRequest=0,rowsProject='',rowDrafts={};
let token=location.hash.slice(1)||sessionStorage.getItem('rpts_token')||'';sessionStorage.setItem('rpts_token',token);history.replaceState(null,'',location.pathname);
let state=null,view='games',selected='',lastSignature='',modalType='',dismissedJob='',lastPrompt='',filter='',rowOffset=0,rowQuery='',rowStatus='',connectionFails=0;
const updateBusy=()=>['downloading','preparing','restarting'].includes(state?.update?.status);
const busy=()=>updateBusy()||Boolean(state?.job&&['running','waiting_input','stopping'].includes(state.job.status));
const chosen=()=>state?.projects.find(x=>x.id===selected), model=()=>state?.models.find(x=>x.key===state.selected_model);
const fmt=n=>Number(n||0).toLocaleString('ru-RU');
function date(v){if(!v)return 'Дата не записана';const d=new Date(typeof v==='number'?v*1000:v);return isNaN(d)?String(v):d.toLocaleString('ru-RU',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'});}
async function api(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:{'Authorization':'Bearer '+token,...(data===undefined?{}:{'Content-Type':'application/json'})},body:data===undefined?undefined:JSON.stringify(data)});const value=await r.json();if(!r.ok)throw Error(value.error||'Не удалось выполнить запрос.');return value;}
function toast(message){$('#toast').textContent=message;$('#toast').classList.add('visible');setTimeout(()=>$('#toast').classList.remove('visible'),4500);}
function modal(title,body,footer='',type=''){window.rptsSelects.close();modalType=type;$('#modal-content').innerHTML=`<div class="modal-head"><h2 id="modal-title">${e(title)}</h2><button class="icon-button" data-action="close-modal" aria-label="Закрыть">${icon('close')}</button></div><div class="modal-body">${body}<div id="modal-error" class="error-text" role="alert"></div></div>${footer?`<div class="modal-foot">${footer}</div>`:''}`;if(!$('#modal').open)$('#modal').showModal();window.rptsSelects.enhance($('#modal'));}
function closeModal(){window.rptsSelects.close();$('#modal').close();modalType='';}
function progress(value,cls=''){return `<div class="progress ${cls}"><span style="width:${Math.max(0,Math.min(100,Number(value)||0))}%"></span></div>`;}
function header(title,subtitle,action=''){return `<div class="page-head"><div><div class="eyebrow">ТВОИ ИСТОРИИ. НА РУССКОМ.</div><h1>${title}</h1><p class="muted small">${subtitle}</p></div>${action}</div>`;}
function statusGame(g){if(!g.available)return ['orange','Папка недоступна'];if(!g.audited)return ['muted','Готова к анализу'];if(g.errors)return ['orange',`Ошибки: ${g.errors}`];if(g.total&&g.ready===g.total)return ['','Переведено'];if(g.ready)return ['purple','Перевод начат'];return ['muted','Ожидает перевода'];}
function badge(g){const [cls,text]=statusGame(g);return `<span class="status-label ${cls}">${e(text)}</span>`;}
function renderJob(){const j=state.job;if(!j||dismissedJob===j.id&&!['running','waiting_input','stopping'].includes(j.status)){$('#job-panel').innerHTML='';return;}const active=['running','waiting_input','stopping'].includes(j.status);$('#job-panel').innerHTML=`<section class="job-box ${!active&&j.status==='completed'?'completed':''}"><div class="job-main"><strong>${active?'<span class="spinner"></span>':icon(j.status==='completed'?'check':'info')}${e(active?j.phase:j.message)}</strong><p>${e(j.status==='stopping'?j.message:active?(j.total?`${fmt(j.done)} / ${fmt(j.total)} строк · ошибок ${j.errors} · осталось ~${j.eta||'…'}`:j.detail||'Подготавливаем данные. Время зависит от модели и компьютера.'):j.result?.warning?'Есть строки с ошибками. Их можно перевести повторно.':'Результат и подробности сохранены.')}</p>${active?(j.progress===null?'<div class="progress indeterminate"><span></span></div>':progress(j.progress)):''}</div><div class="job-actions">${j.status==='waiting_input'?b('Нужен ответ','prompt','primary'):''}${b('Журнал','journal','ghost')}${active?b(icon('stop')+' Остановить','cancel','',j.status==='stopping'?'disabled':''):`<button class="icon-button" data-action="dismiss-job" aria-label="Скрыть уведомление">${icon('close')}</button>`}</div></section>`;if(modalType==='journal'&&$('#job-log')){const log=$('#job-log'),atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<50;log.textContent=(j.logs||[]).join('\n');if(atBottom)log.scrollTop=log.scrollHeight;}if(j.status==='waiting_input'&&lastPrompt!==j.id+j.input){lastPrompt=j.id+j.input;showPrompt();}}
async function refresh(force=false){try{const next=await api('/api/state');connectionFails=0;$('#connection').hidden=true;state=next;if(!state.projects.some(x=>x.id===selected))selected=state.projects.some(x=>x.id===state.preferences.active_project)?state.preferences.active_project:state.projects[0]?.id||'';const sig=JSON.stringify([state.projects,state.translations,state.models,state.selected_model,state.backend,state.mode,state.hardware,state.history,state.job,state.report_available,state.update]);if(force||sig!==lastSignature){if(!$('#main').contains(document.activeElement)||!document.activeElement.matches('input,textarea,select,[role=combobox]')||force){lastSignature=sig;render();}}renderJob();renderUpdateNotice();}catch(err){if(++connectionFails>2){$('#connection').hidden=false;$('#connection').textContent='Не удалось подключиться к RPTS: '+err.message;}}}
async function start(action,params={},identity=selected){await api('/api/job',{action,project_id:identity,params});closeModal();dismissedJob='';await refresh(true);}
function addGame(){modal('Добавить игру',`<p class="muted small" style="margin-bottom:18px">Выбери папку игры или вставь путь к её EXE. Поддерживаются игры на Ren’Py, включая архивы .rpa.</p><div class="field"><label for="game-path">Путь к игре</label><div class="inline-field"><input id="game-path" placeholder="F:\\Games\\Emberbound" autocomplete="off">${b(icon('folder'),'browse-game','','aria-label="Выбрать папку"')}</div></div>`,b('Отмена','close-modal','ghost')+b('Добавить игру','confirm-add','primary'),'add');}
async function pickFolder(field,kind,button) {
 if(!field)return;button.disabled=true;
 try{const path=await window.rptsNative.chooseFolder(field.value,kind);if(path!==null&&field.isConnected){field.value=path;field.dispatchEvent(new Event('input',{bubbles:true}));}}
 finally{if(button.isConnected)button.disabled=false;if(field.isConnected)field.focus();}
}
function translateDialog(){const g=chosen(),m=model();if(!g)throw Error('Сначала добавь игру.');modal('Перевести '+g.title,`<p class="muted">${e(m?.title||'Модель будет выбрана по оборудованию')}</p><p class="muted small">${m?.ready?'Модель и движок уже на диске.':'При первом запуске загрузятся движок и модель '+e(m?.size||'')+'.'} Ручные правки сохранятся. Найденные ошибки рода в машинном переводе проверяются автоматически.</p><label class="switch"><input id="auto-install" type="checkbox" checked>Установить перевод после завершения без ошибок</label><label class="switch"><input id="auto-launch" type="checkbox">Запустить игру после установки</label><div class="notice">Перевод получает исходник, соседние реплики и сведения о персонажах. Ошибки и полные запросы записываются в диагностику.</div>`,b('Отмена','close-modal','ghost')+b(icon('play')+' Начать перевод','confirm-translate','primary'),'translate');}
function launchDialog(){
 const g=chosen();if(!g)throw Error('Сначала выбери игру.');
 if(busy())throw Error('Дождись завершения остановки текущей задачи, затем нажми «Играть».');
 const ready=g.ready>0,partial=g.ready<g.total;
 modal('Запустить '+g.title,`<p class="muted launch-summary">${ready?`Готово ${fmt(g.ready)} из ${fmt(g.total)} строк. ${partial?'Можно установить готовую часть и продолжить перевод позже.':'Можно установить текущий перевод перед игрой.'}`:'Готовых строк пока нет. Игру можно запустить сразу.'}</p>
 <div class="launch-choices">
 <button class="launch-choice recommended" data-action="launch-current" data-project="${e(g.id)}" ${ready?'':'disabled'}><span class="choice-icon">${icon('download')}</span><span><strong>Установить перевод и запустить</strong><small>${partial?'Соберём и установим готовую часть из рабочей базы. Непереведённые строки останутся как в игре.':'Соберём текущую версию с последними переводами и ручными правками.'} Версия сохранится в библиотеке.</small></span>${icon('arrow')}</button>
 <button class="launch-choice" data-action="launch-only" data-project="${e(g.id)}"><span class="choice-icon">${icon('play')}</span><span><strong>Просто запустить игру</strong><small>${g.installed?'Запуск с уже установленным переводом. Новые строки пока не устанавливаются.':'Запуск без установки нового перевода.'}</small></span>${icon('arrow')}</button>
 </div>`,b('Отмена','close-modal','ghost'),'launch');
}
function showPrompt(){if(!state.job?.input)return;modal('Ответ для продолжения',`<pre class="log prompt-context">${e((state.job.logs||[]).slice(-22).join('\n'))}</pre><p style="white-space:pre-wrap">${e(state.job.input)}</p><div class="field" style="margin-top:18px"><label for="worker-answer">Твой ответ</label><input id="worker-answer" autocomplete="off"></div>`,b('Остановить операцию','cancel','ghost')+b('Продолжить','answer','primary'),'prompt');}
async function showRows(){
 if(!chosen()?.audited)throw Error('Сначала выполни анализ игры.');
 view='game';gameTab='text';const identity=selected,request=++rowsRequest;if(rowsProject!==identity){rowsData={rows:[],total:0};rowsProject=identity;}rowsLoading=true;render();
 try{const data=await api('/api/rows?'+new URLSearchParams({project_id:identity,status:rowStatus,q:rowQuery,offset:rowOffset}));
 if(request!==rowsRequest||selected!==identity)return;rowsData=data;window.currentRows=data.rows;
 }finally{if(request===rowsRequest){rowsLoading=false;if(view==='game'&&gameTab==='text'&&selected===identity)render();}}
}
async function act(action,el){
 if(action==='show-updates'){view='settings';settingsTab='updates';render();return;}
 if(action==='update-check'){await api('/api/update/check',{});await refresh(true);return;}
 if(action==='update-install'){
  if(Object.keys(rowDrafts).length||translationDraft)throw Error('Сохрани ручные правки и параметры перевода перед перезапуском.');
  await api('/api/update/install',{});await refresh(true);return;
 }
 if(action==='close-modal'){closeModal();return;}
 if(action==='add-game'){addGame();return;}
 if(action==='browse-game'){await pickFolder($('#game-path'),'game',el);return;}
 if(action==='confirm-add'){const result=await api('/api/games',{path:$('#game-path').value});selected=result.id;closeModal();view='game';gameTab='overview';await refresh(true);toast('Игра добавлена. Можно начать перевод.');return;}
 if(action==='select-game'){selected=el.dataset.id;if(!busy())await api('/api/select',{id:selected});view='game';gameTab='overview';render();return;}
 if(action==='launch'){launchDialog();return;}
 if(action==='launch-current'||action==='launch-only'){await start('launch',{install_current:action==='launch-current'},el.dataset.project);return;}
 if(action==='translate'){translateDialog();return;}
 if(action==='confirm-translate'){await start('translate',{install:$('#auto-install').checked,launch:$('#auto-install').checked&&$('#auto-launch').checked});return;}
 if(action==='choose-model'){await api('/api/settings',{model:el.dataset.id,backend:state.backend,mode:'balanced'});await refresh(true);toast('Модель выбрана. Скачивание начнётся при переводе или по кнопке «Загрузить».');return;}
 if(action==='discard-settings'){translationDraft=null;render();return;}
 if(action==='save-settings'){const glossary=JSON.parse($('#glossary-setting').value||'{}');await api('/api/settings',{model:$('#model-setting').value,backend:$('#backend-setting').value,mode:$('#mode-setting').value,notes:$('#notes-setting').value,glossary});translationDraft=null;await refresh(true);toast('Настройки сохранены.');return;}
 if(action==='cancel'){await api('/api/cancel',{});closeModal();await refresh();return;}
 if(action==='prompt'){showPrompt();return;}
 if(action==='answer'){await api('/api/respond',{value:$('#worker-answer').value});closeModal();await refresh();return;}
 if(action==='search-actions'){showActionSearch();return;}
 if(action==='build-install'){modal('Собрать и установить перевод', '<p>Сначала сохрани готовую часть в отдельную версию. Затем на вкладке «Версии перевода» нажми «Установить».</p>',b('Сохранить версию','build','primary'),'build');return;}
 if(action==='journal'){modal('Журнал текущей операции',`<pre class="log" id="job-log">${e((state.job?.logs||[]).join('\n'))}</pre>`,b('Закрыть','close-modal'),'journal');$('#job-log').scrollTop=$('#job-log').scrollHeight;return;}
 if(action==='dismiss-job'){dismissedJob=state.job.id;renderJob();return;}
 if(action==='game-tools'){view='game';gameTab='tools';render();return;}
 if(action==='open'){await api('/api/open',{target:el.dataset.target,project_id:selected});return;}
 if(action==='rows'){rowOffset=0;await showRows();return;}
 if(action==='find-rows'){rowQuery=$('#row-query').value;rowStatus=$('#row-status').value;rowOffset=0;await showRows();return;}
 if(action==='rows-prev'||action==='rows-next'){rowOffset=Math.max(0,rowOffset+(action==='rows-next'?30:-30));await showRows();return;}
 if(action==='save-row'){const r=window.currentRows[Number(el.dataset.index)];await api('/api/save-row',{project_id:selected,id:r.segment_id,updated_at:r.updated_at,translation:$('#row-'+el.dataset.index).value});delete rowDrafts[selected+':'+r.segment_id];toast('Правка сохранена. Собери новую версию перевода.');await showRows();await refresh();return;}
 if(action==='import-translation'){if(!selected)throw Error('Сначала выбери игру, для которой добавляешь перевод.');const show=path=>modal('Добавить готовый перевод',`<p class="muted small" style="margin-bottom:18px">Игра: ${e(chosen().title)}. Укажи папку RPTS-патча с файлом patch_manifest.json. Оригинальная папка останется на месте.</p><div class="inline-field"><input id="patch-path" value="${e(path||'')}" placeholder="Папка готового патча">${b(icon('folder'),'browse-patch','','aria-label="Выбрать папку перевода"')}</div>`,b('Отмена','close-modal','ghost')+b('Добавить','confirm-import','primary'),'import');window.patchDialog=show;show('');return;}
 if(action==='browse-patch'){await pickFolder($('#patch-path'),'translation',el);return;}
 if(action==='confirm-import'){await start('import_translation',{path:$('#patch-path').value});return;}
 if(action==='install'){await start('install',{translation_id:el.dataset.id},el.dataset.project);return;}
 if(['report','install_model','audit','translate_ui','profiles','build','check','capture_on','capture_off','import_runtime','coverage','uninstall','restore','retranslate','evaluate','unrpyc'].includes(action)){await start(action);return;}
}
document.addEventListener('click',async ev=>{if(ev.target.closest('[data-tab],[data-command]'))return;const el=ev.target.closest('[data-action],[data-view]');if(!el||el.disabled)return;ev.preventDefault();try{if(el.dataset.view){view=el.dataset.view;if(view==='diagnostics'){view='help';helpTab='diagnostics';}if(el.id==='help-nav')helpTab='guide';lastSignature='';render();$('#main').focus({preventScroll:true});window.scrollTo(0,0);return;}await act(el.dataset.action,el);}catch(err){const node=$('#modal-error');if($('#modal').open&&node)node.textContent=err.message;else toast(err.message);}});
document.addEventListener('input',ev=>{if(ev.target.id==='game-search'){filter=ev.target.value;$('#game-grid').innerHTML=gameGrid();}});
$('#modal').addEventListener('close',()=>{modalType='';});
(async()=>{await refresh(true);setInterval(()=>refresh(),1800);})();

function renderUpdateNotice(){
 const notice=$('#update-notice');if(!notice)return;
 const u=state?.update;notice.hidden=!u||!['available','downloading','preparing','restarting'].includes(u.status);
 notice.innerHTML=icon('refresh')+' '+(updateBusy()?'Обновление…':'Доступно обновление');
 if(u?.status==='restarting'){$('#connection').hidden=false;$('#connection').textContent='RPTS обновляется. Окно закроется и откроется снова.';}
}
