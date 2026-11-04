from db_helpers import connection as db_connection
"""Real local API + Chromium UI regression. Isolated native picker replies are emulated."""
from pathlib import Path
import sys,tempfile,threading,shutil,json,os,time,sqlite3
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'app/tests'))
from test_desktop import fixture,Desktop,Handler,ThreadingHTTPServer,build_patch,write_json,read_json
from playwright.sync_api import sync_playwright,expect

def run():
 with tempfile.TemporaryDirectory() as tmp:
  base=Path(tmp);ws,pid,game,db=fixture(base,'Emberbound');fixture(base,'Вторая история',ready=False)
  shutil.copytree(ROOT/'app/web',ws.root/'app/web')
  for directory in ['src','tools','templates']:
   shutil.copytree(ROOT/'app'/directory,ws.root/'app'/directory,ignore=shutil.ignore_patterns('__pycache__'))
  build_patch(db,db.parent/'patch');ws.save_translation(pid,db.parent/'patch','hy-mt2-7b-q4')
  with db_connection(db) as connection:
   connection.execute("UPDATE segments SET status='pending',translation='' WHERE source_text='Let us go.'")
  write_json(ws.settings/'desktop.json',{'active_project':pid})
  with patch.object(Desktop,'_hardware',lambda s:None):app=Desktop(ws.root)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app;app.server=server
  threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.environ.get('RPTS_TEST_BROWSER'),headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1360,'height':900});errors=[];requests=[]
    picker_requests=[];launch_requests=[]
    def route_job(route):
     data=route.request.post_data_json
     if data['action']=='launch':
      launch_requests.append(data);route.fulfill(status=200,json={'id':'test-launch'})
     else:route.continue_()
    page.route('**/api/job',route_job)
    page.route('**/api/pick-folder',lambda route:picker_requests.append(route))
    page.on('pageerror',lambda ex:errors.append(str(ex)));page.on('request',lambda req:requests.append(req.url))
    page.goto(f'http://127.0.0.1:{server.server_port}/#{app.token}');expect(page.locator('h1')).to_have_text('Библиотека')
    expect(page.locator('article.game-card')).to_have_count(2)
    page.locator('#game-search').fill('Ember');expect(page.locator('article.game-card')).to_have_count(1);page.locator('#game-search').fill('')
    def screenshot(name):
     if os.environ.get('RPTS_SCREENSHOTS'):
      path=Path(os.environ['RPTS_SCREENSHOTS']);path.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(path/name),full_page=False)
    screenshot('01-library.png')
    page.locator('[data-action="select-game"][data-id="'+pid+'"]').first.click();expect(page.locator('h1')).to_have_text('Emberbound')
    expect(page.locator('.tabs [data-scope="game"]')).to_have_count(5);screenshot('02-game.png')
    page.locator('[data-action="launch"]').click()
    expect(page.locator('.launch-choice')).to_have_count(2)
    expect(page.locator('.launch-summary')).to_contain_text('1 из 2')
    screenshot('10-partial-launch.png')
    page.locator('[data-action="launch-current"]').click()
    assert launch_requests[-1]=={'action':'launch','project_id':pid,'params':{'install_current':True}}
    expect(page.locator('#modal')).not_to_be_visible()
    page.locator('[data-action="launch"]').click();page.locator('[data-action="launch-only"]').click()
    assert launch_requests[-1]['params']=={'install_current':False}
    # Choosing a launch mode must not silently skip API errors.
    page.unroute('**/api/job',route_job)
    page.route('**/api/job',lambda route:route.fulfill(status=400,json={'error':'Дождись остановки текущей задачи.'}))
    page.locator('[data-action="launch"]').click();page.locator('[data-action="launch-current"]').click()
    expect(page.locator('#modal-error')).to_have_text('Дождись остановки текущей задачи.')
    expect(page.locator('#modal')).to_be_visible();page.locator('[data-action="close-modal"]').first.click()
    page.unroute('**/api/job');page.route('**/api/job',route_job)
    page.locator('.tabs [data-scope="game"][data-tab="text"]').click();expect(page.locator('#row-0')).to_be_visible()
    page.locator('#row-0').fill('Здравствуй, [name].');page.locator('[data-action="save-row"]').first.click()
    expect(page.locator('#toast')).to_have_text('Правка сохранена. Собери новую версию перевода.');expect(page.locator('#row-0')).to_have_value('Здравствуй, [name].')
    screenshot('03-text.png')
    page.locator('.tabs [data-scope="game"][data-tab="versions"]').click();expect(page.locator('[data-action="install"]')).to_have_count(1)
    page.locator('.tabs [data-scope="game"][data-tab="interface"]').click()
    expect(page.locator('[data-action="translate_ui"]').first).to_be_visible()
    expect(page.locator('[data-action="capture_on"]')).not_to_be_visible()
    expect(page.locator('.interface-stats>div')).to_have_count(2);screenshot('04-interface.png')
    page.locator('.capture-fallback summary').click();expect(page.locator('.capture-step')).to_have_count(4)
    expect(page.locator('[data-action="capture_on"]')).to_be_visible()
    page.locator('.capture-fallback summary').click()
    page.locator('.tabs [data-scope="game"][data-tab="tools"]').click();expect(page.locator('[data-action="profiles"]')).to_be_visible()
    page.locator('#nav [data-view="model"]').click();expect(page.locator('.model-card')).to_have_count(9)
    page.locator('[data-scope="model"][data-tab="parameters"]').click()
    backend=page.locator('.custom-select:has(#backend-setting) [role="combobox"]')
    expect(page.locator('#backend-setting')).not_to_be_visible()
    backend.click();expect(backend).to_have_attribute('aria-expanded','true')
    screenshot('11-custom-choice.png')
    page.evaluate('refresh()')
    expect(backend).to_have_attribute('aria-expanded','true')
    backend.press('End');backend.press('Enter');expect(page.locator('#backend-setting')).to_have_value('cpu')
    mode=page.locator('.custom-select:has(#mode-setting) [role="combobox"]')
    before=page.locator('#mode-setting').input_value();mode.click();mode.press('End');mode.press('Escape')
    expect(page.locator('#mode-setting')).to_have_value(before);expect(mode).to_have_attribute('aria-expanded','false')
    backend.click();backend.press('n');backend.press('Enter');expect(page.locator('#backend-setting')).to_have_value('cuda')
    backend.click();page.locator('[role="option"]:visible').filter(has_text='Процессор').click();expect(page.locator('#backend-setting')).to_have_value('cpu')
    backend.click();backend.press('Tab');expect(backend).to_have_attribute('aria-expanded','false')
    page.locator('#notes-setting').fill('Проверка сохранения указаний.');page.locator('#glossary-setting').fill('{"Alex":"Алекс"}')
    page.locator('#nav [data-view="games"]').click();page.locator('#nav [data-view="model"]').click()
    expect(page.locator('#notes-setting')).to_have_value('Проверка сохранения указаний.')
    expect(page.locator('#backend-setting')).to_have_value('cpu')
    page.locator('[data-action="save-settings"]').click();expect(page.locator('#toast')).to_have_text('Настройки сохранены.')
    assert read_json(ws.settings/'local_ai_settings.json')['backend']=='cpu'
    assert read_json(ws.settings/'translation_config.local_ai.json')['notes']=='Проверка сохранения указаний.'
    screenshot('05-model-settings.png')
    page.keyboard.press('Control+k');page.locator('#action-query').fill('телефон');expect(page.locator('.command-result')).to_have_count(1);page.keyboard.press('Enter')
    expect(page.locator('h1')).to_have_text('Emberbound');expect(page.locator('.capture-step')).to_have_count(4)
    page.locator('#nav [data-view="settings"]').click();expect(page.locator('#notes-setting')).to_have_count(0)
    page.locator('[data-scope="settings"][data-tab="updates"]').click()
    expect(page.locator('[data-action="update-check"]')).to_be_visible()
    app.updater.set(status='available',message='Доступна новая сборка dev.',release={'version':'11.28','build_id':'a'*40,'bytes':123456789,'notes':'Проверка обновлений.'})
    page.evaluate('refresh(true)')
    expect(page.locator('#update-notice')).to_be_visible()
    expect(page.locator('[data-action="update-install"]')).to_be_enabled()
    screenshot('06-updates.png')
    with patch.object(app.updater,'begin',return_value={'status':'downloading'}) as install:
     page.locator('[data-action="update-install"]').click()
     for _ in range(30):
      if install.called:break
      page.wait_for_timeout(100)
     assert install.call_count==1
    app.updater.set(status='downloading',progress=37,message='Скачиваю обновление…')
    page.evaluate('refresh(true)')
    expect(page.locator('[data-action="update-check"]')).to_be_disabled()
    expect(page.locator('#main .progress span')).to_have_attribute('style','width:37%')
    app.updater.set(status='error',message='GitHub недоступен. Можно переводить без интернета.')
    page.evaluate('refresh(true)')
    expect(page.locator('#main')).to_contain_text('Можно переводить без интернета')
    expect(page.locator('[data-action="update-check"]')).to_be_enabled()
    page.locator('#nav [data-view="help"]').click();page.locator('[data-scope="help"][data-tab="diagnostics"]').click()
    expect(page.locator('[data-action="profiles"]')).to_have_count(0);expect(page.locator('[data-action="report"]')).to_be_visible()
    screenshot('07-support.png')
    page.locator('[data-action="report"]').click()
    for _ in range(100):
     if app.job and app.job['status'] not in {'running','waiting_input','stopping'}:break
     page.wait_for_timeout(100)
    assert app.job['status']=='completed',app.job
    expect(page.locator('[data-target="report"]')).to_be_enabled()
    page.locator('#nav [data-view="tasks"]').click();expect(page.locator('.task-detail')).to_contain_text('Диагностический ZIP')
    screenshot('08-tasks.png')
    page.locator('#nav [data-view="games"]').click();page.locator('[data-action="add-game"]').click()
    page.locator('#game-path').fill(str(base/'missing'));page.locator('[data-action="confirm-add"]').click();expect(page.locator('#modal-error')).not_to_be_empty()
    page.locator('[data-action="browse-game"]').click()
    page.wait_for_timeout(100);request=picker_requests[-1];assert request.request.post_data_json['kind']=='game'
    expect(page.locator('.directory-list')).to_have_count(0)
    request.fulfill(status=200,json={'path':None})
    expect(page.locator('#game-path')).to_have_value(str(base/'missing'))
    expect(page.locator('[data-action="browse-game"]')).to_be_enabled()
    page.locator('[data-action="browse-game"]').click();page.wait_for_timeout(100);request=picker_requests[-1]
    chosen_path=str(base/"Папка игры 'Тест'"/'game')
    request.fulfill(status=200,json={'path':chosen_path})
    expect(page.locator('#game-path')).to_have_value(chosen_path)
    page.locator('[data-action="browse-game"]').click();page.wait_for_timeout(100);request=picker_requests[-1]
    request.fulfill(status=400,json={'error':'Ошибка тестового диалога'})
    expect(page.locator('#modal-error')).to_have_text('Ошибка тестового диалога')
    expect(page.locator('[data-action="browse-game"]')).to_be_enabled()
    page.locator('[aria-label="Закрыть"]').click()
    page.locator('[data-action="select-game"][data-id="'+pid+'"]').first.click()
    page.locator('.tabs [data-scope="game"][data-tab="versions"]').click()
    page.locator('[data-action="import-translation"]').click();page.locator('#patch-path').fill(str(db.parent/'patch'))
    page.locator('[data-action="browse-patch"]').click();page.wait_for_timeout(100);request=picker_requests[-1];assert request.request.post_data_json['kind']=='translation'
    request.fulfill(status=200,json={'path':None})
    expect(page.locator('#patch-path')).to_have_value(str(db.parent/'patch'))
    page.locator('[aria-label="Закрыть"]').click()
    page.locator('#nav [data-view="games"]').click()
    other=next(x['id'] for x in app.ws.projects_list() if x['id']!=pid)
    page.locator('[data-action="select-game"][data-id="'+other+'"]').first.click()
    page.locator('[data-action="launch"]').click();expect(page.locator('[data-action="launch-current"]')).to_be_disabled()
    expect(page.locator('[data-action="launch-only"]')).to_be_enabled();page.keyboard.press('Escape')
    assert page.evaluate('document.querySelectorAll("svg:not(.ui-icon)").length')==0
    expect(page.locator('.icon-credit')).to_have_text('Uicons by Flaticon')
    for width,height in [(1360,900),(1024,768),(920,680)]:
     page.set_viewport_size({'width':width,'height':height})
     for destination in ['games','model','settings','help','tasks']:
      page.locator('#nav [data-view="'+destination+'"]').click()
      assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,destination)
     page.locator('#nav [data-view="games"]').click();page.locator('[data-action="select-game"][data-id="'+pid+'"]').first.click()
     for tab in ['overview','text','versions','interface','tools']:
      page.locator('.tabs [data-scope="game"][data-tab="'+tab+'"]').click()
      if tab=='text':expect(page.locator('#row-0')).to_be_visible()
      assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,tab)
     if width==920:screenshot('09-small-window.png')
     page.locator('.tabs [data-scope="game"][data-tab="overview"]').click();page.locator('[data-action="launch"]').click()
     assert page.locator('#modal').evaluate('(d)=>d.scrollWidth<=d.clientWidth')
     page.keyboard.press('Escape')
     page.locator('#nav [data-view="model"]').click()
     page.locator('[data-scope="model"][data-tab="parameters"]').click()
     page.locator('.custom-select:has(#backend-setting) [role="combobox"]').click()
     assert page.locator('.select-menu:visible').evaluate('(d)=>{let r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}')
     page.keyboard.press('Escape')
    assert all(url.startswith(f'http://127.0.0.1:{server.server_port}/') for url in requests),requests
    assert not any('/api/browse' in url for url in requests),requests
    assert sum('/api/update/install' in url for url in requests)==1,requests
    assert not errors,errors
    fallback=browser.new_page(viewport={'width':1024,'height':768})
    fallback.goto(f'http://127.0.0.1:{server.server_port}/#{app.token}')
    fallback.locator('[data-action="add-game"]').click();fallback.locator('#game-path').fill(str(game.parent))
    fallback.locator('[data-action="browse-game"]').click()
    expect(fallback.locator('#modal-error')).to_contain_text('доступен в Windows')
    expect(fallback.locator('#game-path')).to_have_value(str(game.parent))
    expect(fallback.locator('[data-action="browse-game"]')).to_be_enabled()
    expect(fallback.locator('.directory-list')).to_have_count(0)
    fallback.close()
    browser.close()
    print(json.dumps({'result':'passed','checks':['library/search','5 game tabs','manual edit + markers','versions','static-first phone/UI + collapsed capture fallback','partial launch choice + no-ready + API failure','custom menus: keyboard/mouse/typeahead/Escape/Tab + saved settings','9 models','parameters in one place','draft survives navigation','save settings','Ctrl+K search','dev updater: notification, install action, progress, offline error','support separate from project tools','real diagnostic job','tasks/log','invalid game path','native bridge: game/import/cancel/error/Unicode path','no custom directory UI','one icon family + attribution','all pages at 1360/1024/920','no external requests','no JavaScript errors']},ensure_ascii=False))
  finally:server.shutdown();server.server_close()
if __name__=='__main__':run()
