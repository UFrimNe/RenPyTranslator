/* Launcher views. Translation requests and provider policy live in the existing backend. */
function tabs(items, active, scope) {
 return `<nav class="tabs" aria-label="Разделы страницы">${items.map(([key,label])=>`<button class="tab ${key===active?'active':''}" data-tab="${key}" data-scope="${scope}" aria-current="${key===active?'page':'false'}">${e(label)}</button>`).join('')}</nav>`;
}
function routeButton(label,destination,tab='',ic='arrow') {
 return `<button class="button ghost" data-command="route" data-route="${destination}" data-route-tab="${tab}">${icon(ic)}<span>${e(label)}</span></button>`;
}
function emptyPanel(title,text,button='') {return `<section class="empty"><h2>${e(title)}</h2><p>${e(text)}</p>${button}</section>`;}
function tip(text) {return `<div class="footer-hint">${icon('info')}<span>${e(text)}</span></div>`;}
function renderNav() {
 $('#nav').innerHTML=navs.map(([key,label,ic],index)=>`${index===3?'<div class="nav-divider"></div>':''}<button class="nav-item ${(view===key||view==='game'&&key==='games')?'active':''}" data-view="${key}" ${(view===key||view==='game'&&key==='games')?'aria-current="page"':''}><span class="nav-icon">${icon(ic)}</span><span class="nav-text">${label}</span>${key==='games'?`<span class="nav-count">${state.projects.length}</span>`:key==='tasks'&&busy()?'<span class="spinner" aria-label="Задача выполняется"></span>':''}</button>`).join('');
 $('#settings-button').innerHTML=icon('settings');$('#help-icon').innerHTML=icon('help');
 $('#breadcrumb').textContent=view==='game'?'Библиотека / '+(chosen()?.title||'Игра'):(navs.find(x=>x[0]===view)?.[1]||'Рабочее пространство');
}
function gameCard(g,index) {
 const colors=[['#593e35','#74503c'],['#343d5b','#516b85'],['#3b344e','#725a70'],['#294d47','#598477']][index%4];
 return `<article class="game-card"><button class="card-art" data-action="select-game" data-id="${e(g.id)}" aria-label="Открыть ${e(g.title)}" style="--art1:${colors[0]};--art2:${colors[1]}"><span class="art-kicker">REN’PY · ИСТОРИЯ</span><span class="art-letter">${e(g.title[0]?.toUpperCase())}</span><span class="art-title">${e(g.title)}</span></button><div class="card-body"><h3 title="${e(g.game_dir)}">${e(g.title)}</h3><div class="card-meta">${badge(g)}<span>${g.total?fmt(g.ready)+' / '+fmt(g.total):'Без анализа'}</span></div>${progress(g.percent,g.percent===100?'green':'')}<div class="card-bottom"><span class="muted small">${g.installed?'Патч установлен':g.total?g.percent+'% строк готово':'Готова к началу'}</span>${b('Открыть '+icon('arrow'),'select-game','ghost',`data-id="${e(g.id)}"`)}</div></div></article>`;
}
function gameGrid() {
 const games=state.projects.filter(x=>x.title.toLowerCase().includes(filter.toLowerCase()));
 return games.map(gameCard).join('')+(!games.length&&state.projects.length?'<p class="muted">Игр с таким названием нет.</p>':'');
}
function versionCards(translations) {
 return `<div class="grid">${translations.map(t=>`<article class="model-card version-card"><div class="model-top"><span class="model-glyph">${icon('library')}</span><span class="status-label ${t.total&&t.ready>=t.total?'':'orange'}">${t.total&&t.ready>=t.total?'Готовый перевод':'Частичный перевод'}</span></div><h3>${e(t.title)}</h3><dl class="version-facts"><div><dt>Сохранён</dt><dd>${e(date(t.created_at))}</dd></div><div><dt>Готово</dt><dd>${fmt(t.ready)}${t.total?' / '+fmt(t.total):''} строк · русский</dd></div><div><dt>Модель</dt><dd>${e(state.models.find(m=>m.key===t.model)?.title||t.model||'Не записана')}</dd></div></dl><div class="button-row">${b(icon('download')+' Установить','install','primary',`data-id="${e(t.id)}" data-project="${e(t.project_id)}" ${busy()?'disabled':''}`)}<span class="muted small">${t.working?'Рабочая сборка':'Сохранённая версия'}</span></div></article>`).join('')}</div>`;
}
function games() {
 const head=header('Библиотека','Выбери игру — все её действия и настройки будут на одной странице.',b(icon('plus')+' Добавить игру','add-game','primary',busy()?'disabled':''));
 const navigation=tabs([['games','Игры · '+state.projects.length],['translations','Готовые переводы · '+state.translations.length]],libraryTab,'library');
 if(libraryTab==='translations')return head+navigation+`<div class="section-bar"><div><h2>Сохранённые версии</h2><p class="muted small">У каждого перевода указаны игра и модель.</p></div>${b(icon('folder')+' Добавить с диска','import-translation','',!chosen()||busy()?'disabled':'')}</div>`+(state.translations.length?versionCards(state.translations):emptyPanel('Здесь будут готовые переводы','Открой игру и начни перевод. Сохранённые версии появятся здесь автоматически.'))+tip('Для импорта сначала открой нужную игру. Патчи других игр не устанавливаются случайно.');
 if(!state.projects.length)return head+navigation+emptyPanel('Добавь первую игру','Укажи папку Ren’Py-игры или её EXE. Затем можно сразу начать перевод.',b(icon('plus')+' Выбрать игру','add-game','primary'));
 return head+navigation+`<div class="library-summary"><span>Игры <strong>${state.projects.length}</strong></span><span>Патч установлен <strong>${state.projects.filter(g=>g.installed).length}</strong></span><span>Версии <strong>${state.translations.length}</strong></span></div><div class="section-bar"><h2>Мои игры</h2><label class="search">${icon('search')}<input id="game-search" aria-label="Поиск игры" placeholder="Найти игру…" value="${e(filter)}"></label></div><div class="grid" id="game-grid">${gameGrid()}</div>`+tip('Открой карточку: там перевод, текст, версии, телефон и инструменты именно этой игры.');
}
function gameOverview(g) {
 const m=model(),complete=g.total>0&&g.ready===g.total;
 const primary=complete?b(icon('library')+' Сохранить версию','build','primary',busy()?'disabled':''):b(icon('play')+' '+(g.ready?'Продолжить перевод':'Начать перевод'),'translate','primary',busy()||!g.available?'disabled':'');
 return `<div class="journey"><span class="${g.audited?'done':'current'}">${icon(g.audited?'check':'search')}<span>1. Анализ игры</span></span><span class="${complete?'done':g.audited?'current':''}">${icon(complete?'check':'edit')}<span>2. Перевод текста</span></span><span class="${g.installed?'done':complete?'current':''}">${icon(g.installed?'check':'download')}<span>3. Установка патча</span></span></div><div class="overview"><section class="focus-panel"><div class="status-label purple">${icon('games')} ПРОЕКТ ИГРЫ</div><h2>${complete?'Текст готов к сборке':g.ready?'Можно продолжать':g.audited?'Можно начинать перевод':'Начнём с анализа файлов'}</h2><p class="caption">${g.errors?'Остались строки с ошибками: '+g.errors+'. Продолжение повторит их обработку.':complete?'Сохрани версию, затем установи её на вкладке «Версии перевода».':'Готовые строки сохраняются. Первый запуск сам подготовит игру и модель.'}</p>${progress(g.percent)}<div class="progress-caption"><span>${fmt(g.ready)} / ${fmt(g.total)} строк · ${g.percent}%${g.runtime?' · '+g.runtime+' динамических выражений для проверки':''}</span><strong>${g.errors?g.errors+' ошибок':''}</strong></div><div class="button-row">${primary}${b(icon('play')+' Играть','launch','',busy()||!g.available?'disabled':'')}</div></section><section class="model-panel"><span class="model-glyph">${icon('model')}</span><h3>${e(m?.title||'Определяем модель…')}</h3><p class="muted">${e(state.backend.toUpperCase())} · ${m?.ready?'Готова к работе':m?.installed?'Нужен движок':'Загрузится при первом запуске'}</p><p class="muted">${state.mode==='balanced'?'Перевод с проверками':state.mode==='reviewed'?'Перевод и редактура':'Полная редактура'}</p>${routeButton('Модель и параметры','model','catalog','settings')}</section></div><div class="quick-grid">${jumpCard('edit','Текст и ошибки','Просмотр исходника, поиск и ручные правки','text')}${jumpCard('library','Версии перевода','Сохранение, установка и импорт патча','versions')}${jumpCard('games','Телефон и интерфейс','Сообщения и элементы из файлов игры','interface')}</div>`;
}
function jumpCard(ic,title,text,tab) {return `<button class="jump-card" data-tab="${tab}" data-scope="game">${icon(ic)}<span><strong>${e(title)}</strong><small>${e(text)}</small></span>${icon('arrow')}</button>`;}
const projectTools=[
 ['check','Проверить проект','Проверка базы и текущего состояния','check'],
 ['audit','Повторить анализ','Найти строки в файлах игры','search'],
 ['profiles','Профили персонажей','Род, имена и подтверждённые сведения','model'],
 ['evaluate','Проверка качества','Проверочный набор и отчёт','edit'],
 ['restore','Восстановить базу','Выбрать резервную копию проекта','refresh'],
 ['retranslate','Перевести заново','Сброс с резервной копией и подтверждением','refresh'],
 ['uninstall','Удалить патч','Откат установленного RPTS-перевода','close']
];
function toolGrid(items) {return `<div class="tool-grid">${items.map(([action,title,detail,ic])=>`<button class="tool ${['uninstall','retranslate'].includes(action)?'careful':''}" data-action="${action}" ${busy()||!chosen()||(!chosen().audited&&action!=='audit')?'disabled':''}>${icon(ic)}<span><strong>${e(title)}</strong><small>${e(detail)}</small></span>${icon('arrow')}</button>`).join('')}</div>`;}
function interfacePage() {
 const g=chosen();
 if(!g?.audited)return emptyPanel('Текст найдётся при анализе игры','Диалоги, доступные телефонные сообщения и интерфейс читаются из файлов. Запускать игру для этого не нужно.',b(icon('search')+' Проанализировать','audit','primary',busy()?'disabled':''));
 return `<section class="panel"><div class="section-bar"><div><h2>Телефон и интерфейс — в обычном переводе</h2><p>Анализ читает доступный текст из скриптов и архивов игры. Найденные сообщения переводятся вместе с диалогами и устанавливаются в общем патче.</p></div><span class="status-label purple">${icon('check')} Из файлов игры</span></div>
 <div class="interface-stats"><div><span>Сообщения панелей</span><strong>${fmt(g.phone_ready)} / ${fmt(g.phone_total)}</strong><small>готово из найденных</small></div><div><span>Элементы интерфейса</span><strong>${fmt(g.ui_ready)} / ${fmt(g.ui_total)}</strong><small>готово из найденных</small></div></div>
 <p>Чтобы закончить только эту часть, нажми «Перевести телефон и UI». Если текст отсутствует в базе, повтори анализ файлов. Готовые переводы сохранятся.</p>
 <div class="button-row">${b(icon('edit')+' Перевести телефон и UI','translate_ui','primary',busy()?'disabled':'')}${b(icon('search')+' Повторить анализ','audit','',busy()?'disabled':'')}${b(icon('play')+' Играть','launch','ghost',busy()||!g.available?'disabled':'')}</div>
 </section>${toolGrid([['coverage','Проверить покрытие','Отчёт о найденных, переведённых и неразрешённых строках','diagnostics']])}
 <details class="panel capture-fallback"><summary><span>${icon('search')} Текст всё ещё не найден?</span><small>Дополнительный захват во время игры</small></summary>
 <p>Это запасной способ для текста, который формируется во время игры и не удалось извлечь из файлов. RPTS добавляет в игру запись отображаемых строк через Ren’Py. Это не чтение памяти процесса.</p>
 <div class="capture-steps">${[
 ['capture_on','Включить захват','Добавить запись отображаемого текста в журнал','play'],
 ['launch','Открыть игру','Открой нужный телефон или панель, затем закрой игру','games'],
 ['import_runtime','Импортировать текст','Добавить найденные строки в рабочую базу','download'],
 ['translate_ui','Перевести найденное','Обработать новые сообщения и элементы','edit']
 ].map(([a,t,d,ic],i)=>`<div class="capture-step"><span class="step-number">${i+1}</span><div><h3>${t}</h3><p>${d}</p></div>${b(icon(ic)+' '+(i===1?'Запустить':i===2?'Импортировать':i===3?'Перевести':'Включить'),a,'',busy()?'disabled':'')}</div>`).join('')}</div>
 <div class="button-row">${b(icon('stop')+' Выключить захват','capture_off','ghost',busy()?'disabled':'')}</div></details>`+tip('Текст на изображениях не распознаётся автоматически. При запуске кнопка «Играть» предложит установить готовый перевод.');
}
function textPage(g) {
 if(!g.audited)return emptyPanel('Сначала нужен анализ игры','Анализ найдёт строки и подготовит рабочую базу.',b(icon('search')+' Проанализировать','audit','primary',busy()?'disabled':''));
 return `<div class="row-toolbar"><div class="inline-field"><input id="row-query" aria-label="Поиск по тексту" placeholder="Исходник или перевод…" value="${e(rowQuery)}"><select id="row-status" aria-label="Статус строк"><option value="">Все строки</option><option value="error" ${rowStatus==='error'?'selected':''}>Только ошибки</option></select>${b(icon('search')+' Найти','find-rows')}</div><span class="muted small">${fmt(rowsData.total)} строк</span></div><div class="notice small">Ручная правка сохраняется как подтверждённая. После правок собери новую версию во вкладке «Версии перевода».</div>${rowsLoading&&!rowsData.rows.length?'<div class="loading">Читаю строки…</div>':rowsData.rows.length?rowsData.rows.map((r,i)=>`<section class="row-editor"><div class="row-meta"><span>${e(r.speaker||'Рассказчик')}</span><span>${e(r.status)}</span><span class="path">${e(r.source_file)}:${r.source_line}</span></div><div class="row-columns"><div><span class="field-label">Исходник</span><div class="source">${e(r.source_text)}</div></div><div><label class="field-label" for="row-${i}">Перевод</label><textarea id="row-${i}" data-row-id="${e(r.segment_id)}" aria-label="Перевод строки ${i+1}">${e(rowDrafts[selected+':'+r.segment_id]??r.translation)}</textarea></div></div><div class="row-save">${b(icon('check')+' Сохранить правку','save-row','',`data-index="${i}" ${busy()?'disabled':''}`)}</div></section>`).join(''):emptyPanel('Ничего не найдено','Измени поисковый запрос или фильтр.')}<div class="pager">${b('← Назад','rows-prev','ghost',rowOffset?'':'disabled')}<span>${rowsData.total?rowOffset+1:0}–${Math.min(rowOffset+30,rowsData.total)} / ${fmt(rowsData.total)}</span>${b('Далее →','rows-next','',rowOffset+30<rowsData.total?'':'disabled')}</div>`;
}
function gamePage() {
 const g=chosen();if(!g)return header('Игра не выбрана','Вернись в библиотеку и открой карточку.')+routeButton('В библиотеку','games');
 const head=`<div class="back-row">${routeButton('Библиотека','games','','library')}<span class="muted small">${e(g.game_dir)}</span></div>`+header(e(g.title),'Все действия относятся к этой игре.',badge(g));
 const navigation=tabs([['overview','Обзор'],['text','Текст и ошибки'],['versions','Версии перевода'],['interface','Телефон и интерфейс'],['tools','Инструменты']],gameTab,'game');
 let body='';
 if(gameTab==='overview')body=gameOverview(g);
 if(gameTab==='text')body=textPage(g);
 if(gameTab==='versions'){
  const versions=state.translations.filter(t=>t.project_id===selected);
  body=`<div class="section-bar"><div><h2>Версии для ${e(g.title)}</h2><p class="muted small">Собери готовые строки в отдельный патч, затем установи нужную версию.</p></div><div class="button-row">${b(icon('library')+' Сохранить версию','build','primary',!g.ready||busy()?'disabled':'')}${b(icon('folder')+' Импорт с диска','import-translation','',busy()?'disabled':'')}</div></div>`+(versions.length?versionCards(versions):emptyPanel('Сохранённых версий ещё нет','Когда часть строк будет готова, нажми «Сохранить версию».'));
 }
 if(gameTab==='interface')body=interfacePage();
 if(gameTab==='tools')body=`<div class="section-bar"><div><h2>Обслуживание проекта</h2><p class="muted small">Для обычного перевода эти действия не нужны.</p></div>${b(icon('folder')+' Папка игры','open','ghost','data-target="game"')}</div>`+toolGrid(projectTools)+`<div class="notice warning">Повторный перевод и удаление патча требуют подтверждения. Подсказки и список резервных копий появятся в окне операции.</div><div class="button-row">${routeButton('Собрать диагностический ZIP','help','diagnostics','diagnostics')}${b(icon('folder')+' Папка проекта','open','ghost','data-target="project"')}</div>`;
 return head+navigation+body;
}
function translationSettings() {
 const draft=translationDraft||{model:state.selected_model,backend:state.backend,mode:state.mode,notes:state.notes,glossary:JSON.stringify(state.glossary,null,2)};
 return `<div class="settings-layout"><section class="panel"><h2>Параметры перевода</h2><p>Общие настройки для следующих запусков. Сохранённый текст не сбрасывается.</p><div class="fields" id="translation-form"><div class="field"><label for="model-setting">Модель</label><select id="model-setting">${state.models.map(m=>`<option value="${e(m.key)}" ${draft.model===m.key?'selected':''}>${e(m.title)}</option>`).join('')}</select></div><div class="field"><label for="backend-setting">Вычисления</label><select id="backend-setting">${[['cuda','NVIDIA CUDA'],['vulkan','Vulkan · AMD / Intel / NVIDIA'],['cpu','Процессор · CPU']].map(([v,l])=>`<option value="${v}" ${draft.backend===v?'selected':''}>${l}</option>`).join('')}</select></div><div class="field"><label for="mode-setting">Обработка текста</label><select id="mode-setting">${[['balanced','Перевод + проверки'],['reviewed','Перевод + редактура'],['full_review','Полная редактура · три прохода']].map(([v,l])=>`<option value="${v}" ${draft.mode===v?'selected':''}>${l}</option>`).join('')}</select><small>Для Hy-MT2 свободная редактура остаётся экспериментальным режимом. Рекомендуется «Перевод + проверки».</small></div><div class="field"><label for="notes-setting">Указания для перевода</label><textarea id="notes-setting" maxlength="8000">${e(draft.notes)}</textarea><small>Уместный мат допускается по смыслу исходника. Эти указания общие для всех игр.</small></div><div class="field"><label for="glossary-setting">Глоссарий · JSON</label><textarea id="glossary-setting" spellcheck="false">${e(draft.glossary)}</textarea><small>Пример: {"原名": "Русское имя"}</small></div><div class="button-row">${b(icon('check')+' Сохранить настройки','save-settings','primary',busy()?'disabled':'')}${b('Сбросить правки','discard-settings','ghost',translationDraft?'':'disabled')}</div><span class="muted small" id="draft-status">${translationDraft?'Есть несохранённые изменения.':''}</span></div></section><aside class="panel"><h2>Где что искать</h2><div class="help-item"><strong>Имена и род персонажей</strong><p>Игра → Инструменты → Профили персонажей.</p></div><div class="help-item"><strong>Телефон и динамические сообщения</strong><p>Игра → Телефон и интерфейс.</p></div><div class="help-item"><strong>Папки, консоль и будущие обновления</strong><p>Настройки лаунчера.</p></div>${routeButton('Открыть каталог моделей','model','catalog','model')}</aside></div>`;
}
function models() {
 const head=header('Модели и перевод','Выбор модели и все параметры перевода собраны здесь.');
 const navigation=tabs([['catalog','Каталог моделей'],['parameters','Параметры перевода']],modelTab,'model');
 if(modelTab==='parameters')return head+navigation+translationSettings();
 const hw=state.hardware;
 return head+navigation+`<div class="hardware-line">${icon('model')}<span>${hw.loading?'Определяю оборудование…':hw.gpus?.length?e(hw.gpus.map(x=>x.name+' · '+x.memory).join(' / ')):'Выделенная видеокарта не обнаружена'}${hw.ram?' · ОЗУ '+e(hw.ram):''}</span>${routeButton('Параметры','model','parameters','settings')}</div><div class="grid">${state.models.map(m=>`<section class="model-card ${m.key===state.selected_model?'selected':''}"><div class="model-top"><span class="model-glyph">${icon('model')}</span><div class="tag-row">${m.recommended?'<span class="status-label purple">Подходит по памяти</span>':''}${m.installed?'<span class="status-label">На диске</span>':''}</div></div><h3>${e(m.title)}</h3><p>${e(m.description)}</p><div class="specs"><span>${e(m.size)} на диске</span><span>ОЗУ от ${m.ram} ГБ</span><span>VRAM ~${m.vram} ГБ</span></div><div class="button-row">${b(m.key===state.selected_model?icon('check')+' Выбрана':'Выбрать','choose-model',m.key===state.selected_model?'ghost':'primary',`data-id="${e(m.key)}" ${busy()||m.key===state.selected_model?'disabled':''}`)}${m.key===state.selected_model&&!m.ready?b(icon('download')+' Загрузить','install_model','',busy()?'disabled':''):''}</div></section>`).join('')}</div>`+tip('Рекомендация учитывает память компьютера. Качество текста сравнивай на короткой сцене. Вычисления CPU / CUDA / Vulkan — во вкладке «Параметры перевода».');
}
function settingsPage() {
 const head=header('Настройки лаунчера','Хранение файлов и обслуживание приложения.');
 const navigation=tabs([['folders','Папки и данные'],['advanced','Дополнительно'],['updates','Обновления']],settingsTab,'settings');
 let body='';
 if(settingsTab==='folders')body=`<section class="panel"><h2>Файлы на компьютере</h2><p>Игры остаются в своих папках. Проекты, переводы и модели хранятся отдельно.</p>${Object.entries({projects:'Проекты и рабочие базы',translations:'Готовые переводы',models:'Модели и llama.cpp',reports:'Диагностические ZIP'}).map(([k,v])=>`<div class="folder-row"><span class="folder-symbol">${icon('folder')}</span><div><strong>${v}</strong><div class="path">${e(state.paths[k])}</div></div>${b('Открыть','open','ghost',`data-target="${k}"`)}</div>`).join('')}</section>${state.warnings.map(x=>`<div class="notice warning">${e(x)}</div>`).join('')}`;
 if(settingsTab==='advanced')body=`<section class="panel"><h2>Прежнее консольное меню</h2><p>Все расширенные функции сохранены: внешние провайдеры, ручные профили, импорт, обслуживание и восстановление. Одновременно выполнять операции в двух окнах нельзя.</p>${b(icon('code')+' Открыть консоль','open','','data-target="console"')}<div class="separator"></div><h2>Дополнительные компоненты</h2><p>Декомпилятор нужен для некоторых игр с закрытыми скриптами. Обычный запуск переводчика сам подскажет, если его не хватает.</p>${b(icon('download')+' Установить / обновить unrpyc','unrpyc','',busy()?'disabled':'')}<div class="separator"></div>${routeButton('Параметры перевода','model','parameters','settings')}</section>`;
 if(settingsTab==='updates')body=updatesPanel();
 return head+navigation+body;
}
function jobName(action) {return ({audit:'Анализ игры',translate:'Перевод игры',translate_ui:'Перевод интерфейса',install_model:'Загрузка модели',build:'Сборка перевода',install:'Установка патча',report:'Диагностический ZIP',profiles:'Профили персонажей',launch:'Запуск игры',restore:'Восстановление базы',retranslate:'Повторный перевод'})[action]||action;}
function tasksPage() {
 const j=state.job;
 const current=j?`<section class="panel task-detail"><div class="section-bar"><div><span class="status-label ${busy()?'purple':''}">${busy()?'Выполняется':'Завершено'}</span><h2>${e(jobName(j.action))}</h2><p>${e(state.projects.find(g=>g.id===j.project_id)?.title||'Приложение')} · ${e(j.phase)}</p></div><div class="button-row">${busy()?b(icon('stop')+' Остановить','cancel','',j.status==='stopping'?'disabled':''):''}${b('Журнал','journal','ghost')}</div></div><p>${e(j.message||'Результаты сохраняются по мере работы.')}</p>${busy()?(j.progress===null?'<div class="progress indeterminate"><span></span></div>':progress(j.progress)):''}${j.total?`<p>${fmt(j.done)} / ${fmt(j.total)} строк · ошибок ${j.errors} · осталось ~${e(j.eta||'…')}</p>`:''}<pre class="log">${e((j.logs||[]).slice(-35).join('\n')||'Журнал ещё пуст.')}</pre></section>`:emptyPanel('Сейчас ничего не выполняется','Начни перевод в библиотеке или скачай модель в разделе «Модели и перевод».',routeButton('Открыть библиотеку','games','','library'));
 return header('Задачи','Текущий этап, загрузки, сообщения и история операций.')+current+`<div class="section-bar"><h2>История</h2></div><section class="panel">${state.history.length?state.history.slice(0,10).map(j=>`<div class="history-row"><div><strong>${e(jobName(j.action))}</strong><p>${e(j.message||'')} · ${e(date(j.started_at))}</p></div><span>${({completed:'Готово',failed:'Ошибка',cancelled:'Остановлено',interrupted:'Прервано'})[j.status]||e(j.status)}</span></div>`).join(''):'<p>Здесь появятся завершённые операции.</p>'}</section>`;
}
function help() {
 const head=header('Помощь и диагностика','Подсказки для начала работы и один ZIP для разбора проблем.');
 const navigation=tabs([['guide','Как пользоваться'],['diagnostics','Диагностика']],helpTab,'help');
 if(helpTab==='diagnostics')return head+navigation+`<section class="panel"><div class="section-bar"><div><h2>Отчёт о проблеме</h2><p>Состояние базы, настройки, запросы ИИ, ответы и журналы в одном ZIP.</p></div>${b(icon('download')+' Собрать отчёт','report','primary',busy()?'disabled':'')}</div><div class="field"><label for="report-project">Игра для отчёта</label><select id="report-project">${state.projects.map(g=>`<option value="${e(g.id)}" ${g.id===selected?'selected':''}>${e(g.title)}</option>`).join('')||'<option>Игры ещё не добавлены</option>'}</select></div><div class="notice">В ZIP может присутствовать текст игры и переводов. API-ключи скрываются.</div><div class="button-row">${b(icon('folder')+' Открыть ZIP','open','',`data-target="report" ${state.report_available?'':'disabled'}`)}${b('Папка отчётов','open','ghost','data-target="reports"')}</div></section>`;
 return head+navigation+`<div class="steps">${[['Добавь игру','Библиотека → Добавить игру. Укажи папку или EXE. Файлы игры переносить не нужно.'],['Начни перевод','Открой карточку игры → Начать перевод. Первый запуск подготовит модель. Затем можно продолжать с места остановки.'],['Установи готовую версию','Останови перевод, дождись сохранения и нажми «Играть» → «Установить перевод и запустить». Можно играть с готовой частью, а позже продолжить перевод.']].map(([title,text],i)=>`<section class="step"><span>${i+1}</span><div><h3>${title}</h3><p>${text}</p></div></section>`).join('')}</div><div class="quick-grid help-shortcuts">${guideLink('Модель, скорость, глоссарий','Все параметры перевода','model','parameters','model')}${guideLink('Телефон не переведён','Выбери игру → Телефон и интерфейс','game','interface','games')}${guideLink('Возникла ошибка','Собрать диагностический ZIP','help','diagnostics','diagnostics')}</div>`+tip('Ctrl+K открывает поиск по действиям. 100% строк — показатель покрытия, а не гарантия художественного качества.');
}
function guideLink(title,text,route,tab,ic) {return `<button class="jump-card" data-command="route" data-route="${route}" data-route-tab="${tab}">${icon(ic)}<span><strong>${e(title)}</strong><small>${e(text)}</small></span>${icon('arrow')}</button>`;}
function render() {
 if(!state)return;window.rptsSelects.close();renderNav();
 $('#main').innerHTML=({games,game:gamePage,model:models,tasks:tasksPage,settings:settingsPage,help}[view]||games)();
 window.rptsSelects.enhance($('#main'));
}
const commandList=[
 ['Добавить игру','Папка или EXE','games','','library'],['Выбрать модель','Каталог моделей','model','catalog','model'],
 ['Настроить перевод','Скорость, режим, CPU, CUDA, Vulkan, мат, глоссарий','model','parameters','settings'],
 ['Посмотреть текст и ошибки','Игра → Текст и ошибки','game','text','edit'],['Установить перевод','Игра → Версии перевода','game','versions','download'],
 ['Перевести телефон','Сообщения и интерфейс из файлов, дополнительный захват','game','interface','games'],
 ['Род, имена и профили персонажей','Игра → Инструменты','game','tools','model'],['Восстановление и повторный перевод','Игра → Инструменты','game','tools','refresh'],
 ['Собрать отчёт о проблеме','Диагностический ZIP','help','diagnostics','diagnostics'],['Открыть папки','Проекты, модели, переводы','settings','folders','folder'],
 ['Открыть консольное меню','Расширенные инструменты','settings','advanced','code'],['Найти журнал и загрузки','Текущие и завершённые задачи','tasks','','download']
];
function showActionSearch() {
 modal('Найти действие','<label class="visually-hidden" for="action-query">Что нужно сделать?</label><input id="action-query" placeholder="Например: телефон, модель, ошибки…" autocomplete="off"><div id="action-results" class="action-results"></div>',b('Закрыть','close-modal','ghost'),'commands');
 filterCommands('');$('#action-query').focus();
}
function filterCommands(query) {
 const items=commandList.map((c,i)=>({c,i})).filter(({c})=>c.join(' ').toLowerCase().includes(query.toLowerCase()));
 $('#action-results').innerHTML=items.map(({c,i})=>`<button class="command-result" data-command="choose" data-index="${i}">${icon(c[4])}<span><strong>${e(c[0])}</strong><small>${e(c[1])}</small></span>${icon('arrow')}</button>`).join('')||'<p class="muted">Ничего не найдено.</p>';
}
async function navigate(route,tab='') {
 if(route==='game'&&!chosen()){toast('Сначала открой игру в библиотеке.');route='games';}
 closeModal();view=route;
 if(tab){if(route==='game')gameTab=tab;if(route==='model')modelTab=tab;if(route==='settings')settingsTab=tab;if(route==='help')helpTab=tab;}
 if(route==='game'&&gameTab==='text'&&chosen()?.audited){rowOffset=0;await showRows();}else render();
 $('#main').focus({preventScroll:true});window.scrollTo(0,0);
}
function captureDraft() {
 if(!$('#translation-form'))return;
 translationDraft={model:$('#model-setting').value,backend:$('#backend-setting').value,mode:$('#mode-setting').value,notes:$('#notes-setting').value,glossary:$('#glossary-setting').value};
 $('#draft-status').textContent='Есть несохранённые изменения.';$('#translation-form [data-action="discard-settings"]').disabled=false;
}
document.addEventListener('click',async event=>{
 const tab=event.target.closest('[data-tab]'),command=event.target.closest('[data-command]');if(!tab&&!command)return;
 event.preventDefault();
 try{
  if(tab){const scope=tab.dataset.scope,key=tab.dataset.tab;
   if(scope==='game'){gameTab=key;if(key==='text'&&chosen()?.audited){rowOffset=0;await showRows();}else render();}
   if(scope==='library'){libraryTab=key;render();}if(scope==='model'){modelTab=key;render();}if(scope==='settings'){settingsTab=key;render();}if(scope==='help'){helpTab=key;render();}
   window.scrollTo(0,0);
  }else if(command.dataset.command==='route')await navigate(command.dataset.route,command.dataset.routeTab||'');
  else {const c=commandList[Number(command.dataset.index)];if(Number(command.dataset.index)===0){closeModal();addGame();}else await navigate(c[2],c[3]);}
 }catch(error){toast(error.message);}
});
document.addEventListener('input',event=>{
 if(event.target.id==='action-query')filterCommands(event.target.value);
 if(event.target.closest('#translation-form'))captureDraft();
 if(event.target.dataset.rowId)rowDrafts[selected+':'+event.target.dataset.rowId]=event.target.value;
});
document.addEventListener('change',async event=>{
 if(event.target.closest('#translation-form'))captureDraft();
 if(event.target.id==='report-project'){selected=event.target.value;try{if(!busy())await api('/api/select',{id:selected});}catch(error){toast(error.message);}}
});
document.addEventListener('keydown',event=>{
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();showActionSearch();}
 if(event.key==='Enter'&&event.target.id==='action-query')$('#action-results button')?.click();
 if(event.key==='Enter'&&event.target.id==='row-query')$('#main [data-action="find-rows"]')?.click();
});

function updatesPanel(){
 const u=state.update||{status:'idle'},r=u.release;
 const active=['checking','downloading','preparing','restarting'].includes(u.status);
 return `<section class="panel"><div class="section-bar"><div><span class="status-label purple">GitHub · dev · тестовые сборки</span><h2>Обновления программы</h2><p>Текущая версия: ${e(state.version)}. Проверка выполняется при запуске.</p></div><span class="model-glyph">${icon('refresh')}</span></div>
 <p role="status">${e(u.message||'Проверка ещё не выполнялась.')}</p>
 ${active?(u.progress===null||u.progress===undefined?'<div class="progress indeterminate"><span></span></div>':progress(u.progress)):''}
 ${r?`<p class="muted">Сборка ${e(r.version)} · ${e(r.build_id?.slice(0,8))} · ${fmt(Math.ceil(r.bytes/1024/1024))} МБ</p>${r.notes?`<p style="white-space:pre-wrap">${e(r.notes)}</p>`:''}`:''}
 <div class="button-row">${u.status==='available'?b(icon('download')+' Скачать и перезапустить','update-install','primary',busy()?'disabled':''):''}${b('Проверить обновления','update-check',u.status==='available'?'ghost':'',active?'disabled':'')}</div>
 ${busy()&&!updateBusy()?'<p class="muted">Сначала заверши текущую задачу, затем установи обновление.</p>':''}
 <div class="notice">Обновляются только файлы программы и среды запуска. Модели, игры, проекты, переводы и настройки сохраняются. Во время установки окно закроется и откроется снова.</div>
 ${u.last_result?.status==='failed'?`<div class="notice warning">Последняя установка не завершилась: ${e(u.last_result.message)}. Подробности есть в диагностическом ZIP.</div>`:''}
 <p class="muted small">Интернет нужен только для проверки и загрузки. Без него можно продолжать перевод. Ветка dev содержит сборки в разработке.</p></section>`;
}
