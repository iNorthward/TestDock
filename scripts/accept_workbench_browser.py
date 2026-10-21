#!/usr/bin/env python3
"""Exercise the two owned synthetic Pack pages through agent-browser CLI only.

This never submits a business API request. Task transport and clipboard-denial
faults are explicitly synthetic; real DOM events, navigation and keyboard input
are exercised in Chromium. The supplied named session remains open for review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
import uuid


def loopback_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username is not None or parsed.password is not None):
        raise argparse.ArgumentTypeError('Only owned HTTP loopback panel URLs are allowed')
    return value.rstrip('/') + '/'


def accept(inventory_url, schedule_url, *, report, session):
    checks, screenshots = [], []
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def cli(*arguments, source=None):
        result = subprocess.run(['agent-browser', '--session', session, *arguments, '--json'],
                                input=source, capture_output=True, text=True, timeout=45)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError('agent-browser did not return JSON: ' + result.stderr[-1500:]) from error
        if result.returncode or not payload.get('success'):
            raise RuntimeError('agent-browser command failed: ' + json.dumps(payload, ensure_ascii=False)[-2000:])
        return payload.get('data', {})

    def evaluate(source):
        return cli('eval', '--stdin', source=source).get('result')

    def wait(condition):
        cli('wait', '--fn', condition)

    def check(name, passed, evidence=None):
        checks.append({'name': name, 'passed': bool(passed), 'evidence': evidence})
        print(('PASS ' if passed else 'FAIL ') + name, flush=True)
        if not passed:
            raise AssertionError(name)

    def screenshot(name):
        path = report.parent / (name + '.png')
        cli('screenshot', '--full', str(path))
        screenshots.append(str(path))

    def field_state():
        return evaluate("({active:document.activeElement.id,note:document.querySelector('#inventory-note').value,kind:document.querySelector('#inventory-kind').value,scope:document.querySelector('#scope').value})")

    def clean_own_browser_data():
        evaluate("Object.keys(sessionStorage).filter(k=>k.startsWith('platform.workbench.')||k.startsWith('synthetic.workbench.jobs.')).forEach(k=>sessionStorage.removeItem(k));true")
        cli('reload')
        wait("document.querySelector('#rows tr') && document.querySelector('select[id$=kind],select[id$=layout]').options.length===3")

    def instrument_synthetic_poll():
        evaluate("""(() => {
          const original=window.PLATFORMExecution.pollJob;
          window.__syntheticQueries=[];
          window.PLATFORMExecution.pollJob=(fetcher,url,options)=>original((path,init={})=>{
            window.__syntheticQueries.push({path,method:init.method||'GET'});return fetcher(path,init);
          },url,options);return true;
        })()""")

    try:
        check('two distinct owned loopback origins', urlsplit(inventory_url).netloc != urlsplit(schedule_url).netloc)
        cli('open', inventory_url)
        cli('snapshot', '-i')
        cli('set', 'viewport', '1280', '900')
        clean_own_browser_data()
        cli('select', '#inventory-kind', 'boxes')
        cli('fill', '#inventory-note', 'focused-inventory-value')
        state = field_state()
        check('focused text is saved before blur', state['active'] == 'inventory-note' and state['note'] == 'focused-inventory-value', state)
        cli('reload')
        wait("document.querySelector('#inventory-kind').value==='boxes'")
        state = field_state()
        check('focused text and loaded option survive actual reload', state['note'] == 'focused-inventory-value' and state['kind'] == 'boxes', state)

        cli('select', '#scope', 'south')
        wait("document.querySelector('#inventory-kind').options.length===3")
        state = field_state()
        check('new scope clears old identity fields', state['note'] == '' and state['kind'] == '', state)
        cli('fill', '#inventory-note', 'scope-south-only')
        cli('select', '#scope', 'north')
        wait("document.querySelector('#inventory-kind').value==='boxes'")
        state = field_state()
        check('returning to a scope restores only its own fields', state['note'] == 'focused-inventory-value' and state['kind'] == 'boxes', state)
        early = evaluate("""(() => {
          const scope=document.querySelector('#scope');scope.value='south';scope.dispatchEvent(new Event('change',{bubbles:true}));
          scope.value='north';scope.dispatchEvent(new Event('change',{bubbles:true}));
          const note=document.querySelector('#inventory-note');note.focus();note.value='typed-before-options-loaded';note.dispatchEvent(new Event('input',{bubbles:true}));
          const key=Object.keys(sessionStorage).find(k=>k.startsWith('platform.workbench.form.')&&k.endsWith('.north'));
          const record=JSON.parse(sessionStorage.getItem(key));setTimeout(()=>location.reload(),100);
          return {record,selection:document.querySelector('#inventory-kind').value,options:document.querySelector('#inventory-kind').options.length};
        })()""")
        check('input while options are pending retains stored old selection', early['options'] == 1 and early['selection'] == '' and early['record']['values']['inventory-kind'] == 'boxes', early)
        wait("document.querySelector('#inventory-kind').value==='boxes' && document.querySelector('#inventory-note').value==='typed-before-options-loaded'")
        check('early input and pending option survive the following reload', True, field_state())

        cli('select', '#limit', '5')
        wait("document.querySelectorAll('#rows tr').length===5")
        cli('click', '#more')
        wait("document.querySelectorAll('#rows tr').length===9")
        check('opaque-cursor append reaches nine distinct rows', evaluate("document.querySelectorAll('#rows tr').length===9 && new Set([...document.querySelectorAll('#rows tr')].map(n=>n.cells[0].textContent)).size===9"))
        cli('fill', '#search', 'fresh')
        wait("document.querySelectorAll('#rows tr').length===5 && document.querySelector('#rows').textContent.includes('fresh')")
        check('filter change resets the appended page', True, evaluate("document.querySelectorAll('#rows tr').length"))
        cli('select', '#limit', '3')
        wait("document.querySelectorAll('#rows tr').length===3")
        check('page size change resets pagination', True, evaluate("document.querySelectorAll('#rows tr').length"))
        race = evaluate("""(async () => {
          const input=document.querySelector('#search');input.value='slow';input.dispatchEvent(new Event('input',{bubbles:true}));
          await new Promise(r=>setTimeout(r,25));input.value='fast';input.dispatchEvent(new Event('input',{bubbles:true}));
          await new Promise(r=>setTimeout(r,1550));return {text:document.querySelector('#rows').textContent,status:document.querySelector('#list-status').textContent};
        })()""")
        check('late slow query cannot overwrite newer fast results', 'fast' in race['text'] and 'slow' not in race['text'] and '已读取' in race['status'], race)

        exact = evaluate("({display:document.querySelector('#exact-value').textContent,raw:document.querySelector('#exact-value').title,time:document.querySelector('#rows tr').cells[3].textContent})")
        check('exact decimal display keeps raw precision and explicit Shanghai time', exact['display'] == '900719925474099312345.1234567890123456789' and exact['raw'].endswith('89000') and exact['time'] == '2026-10-21 08:01:02', exact)
        evaluate("""(() => {
          const clipboard=navigator.clipboard,nativeWrite=clipboard.writeText.bind(clipboard);
          window.__clipboardWrites=[];window.__nativeClipboardWrite=clipboard.writeText;
          clipboard.writeText=value=>{window.__clipboardWrites.push(value);return nativeWrite(value);};return true;
        })()""")
        cli('click', '#copy')
        wait("document.querySelector('#copy-status').textContent==='已复制。'")
        copied = evaluate('window.__clipboardWrites.at(-1)')
        check('actual browser clipboard API receives the entire raw exact value and resolves with local feedback',
              evaluate("document.querySelector('#copy-status').dataset.kind==='success'") and copied == exact['raw'],
              {'copiedText': copied, 'readbackVerified': False})
        evaluate('navigator.clipboard.writeText=window.__nativeClipboardWrite;delete window.__nativeClipboardWrite;true')
        evaluate("window.__originalClipboard=navigator.clipboard;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw new DOMException('synthetic permission denied','NotAllowedError');}}});true")
        cli('click', '#copy')
        wait("document.querySelector('#copy-status').textContent.includes('synthetic permission denied')")
        check('synthetic permission denial is visible beside copy control', evaluate("document.querySelector('#copy-status').dataset.kind==='warning' && document.querySelector('#copy-status').previousElementSibling.id==='copy'"))
        evaluate("Object.defineProperty(navigator,'clipboard',{configurable:true,value:window.__originalClipboard});delete window.__originalClipboard;true")

        evaluate("window.__expectedOpener=document.querySelector('#rows tr:first-child button');true")
        cli('click', '#rows tr:first-child button')
        wait("document.querySelector('#details-status').textContent==='已读取详情。'")
        confirmed = evaluate("({content:document.querySelector('#details-content').textContent,active:document.activeElement.id,locked:document.body.style.overflow})")
        check('dialog shows only explicit projection and initially focuses close', confirmed['active'] == 'close-details' and confirmed['locked'] == 'hidden' and 'privateSyntheticField' not in confirmed['content'] and '此字段没有被允许展示' not in confirmed['content'], confirmed)
        cli('check', '#details-failure')
        cli('click', '#retry-details')
        wait("document.querySelector('#details-status').textContent.includes('已保留先前确认')")
        check('failed follow-up detail query preserves the confirmed result', evaluate("document.querySelector('#details-content').textContent") == confirmed['content'])
        cli('focus', '#retry-details')
        cli('press', 'Tab')
        check('Tab wraps from last dialog control to first', evaluate("document.activeElement.id") == 'close-details')
        cli('press', 'Shift+Tab')
        check('Shift Tab wraps from first dialog control to last', evaluate("document.activeElement.id") == 'retry-details')
        screenshot('inventory-dialog-desktop')
        cli('press', 'Escape')
        check('Escape closes and restores focus to exact triggering row', evaluate("!document.querySelector('#details').open && document.activeElement===window.__expectedOpener && document.body.style.overflow!=='hidden'"))

        instrument_synthetic_poll()
        cli('check', '#task-failure')
        cli('click', '#start-task')
        wait("document.querySelector('#task-status').textContent.includes('查询中断')")
        unknown = evaluate("({queries:window.__syntheticQueries,task:JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('platform.workbench.task.v1.')))),jobs:JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('synthetic.workbench.jobs.'))))})")
        original_id = unknown['task']['executionId']
        check('synthetic lookup failure retains original execution identity', len(unknown['queries']) == 3 and all(row['method'] == 'GET' for row in unknown['queries']) and len(unknown['jobs']) == 1, unknown)
        cli('reload')
        wait("document.querySelector('#task-status').textContent.includes('发现待确认的原任务')")
        restored = evaluate("({status:document.querySelector('#task-status').textContent,startDisabled:document.querySelector('#start-task').disabled,resumeDisabled:document.querySelector('#resume-task').disabled,jobs:JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('synthetic.workbench.jobs.'))))})")
        check('actual reload restores identity without creating another synthetic task', original_id in restored['status'] and restored['startDisabled'] and not restored['resumeDisabled'] and list(restored['jobs']) == [original_id], restored)
        instrument_synthetic_poll()
        check('restoration itself performs no task query', evaluate("window.__syntheticQueries.length") == 0)
        cli('click', '#resume-task')
        wait("document.querySelector('#task-status').textContent.includes('合成任务已结束')")
        terminal = evaluate("({queries:window.__syntheticQueries,result:JSON.parse(document.querySelector('#task-result').textContent),records:Object.keys(sessionStorage).filter(k=>k.startsWith('platform.workbench.task.v1.'))})")
        check('resume queries the same identity with GET and keeps business incomplete', terminal['result']['reference'] == original_id and terminal['result']['ok'] is None and terminal['result']['status'] == 'incomplete' and all(row['method'] == 'GET' and original_id in row['path'] for row in terminal['queries']) and not terminal['records'], terminal)
        network = cli('network', 'requests', '--method', 'POST')
        check('synthetic UI sends no network business POST', not network.get('requests'), network)

        cli('set', 'viewport', '390', '844')
        mobile = evaluate("({width:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,tables:[...document.querySelectorAll('.wb-table-scroll')].map(n=>({width:n.clientWidth,scroll:n.scrollWidth}))})")
        check('inventory 390px page contains horizontal table overflow locally', mobile['document'] <= 390 and mobile['body'] <= 390 and mobile['tables'][0]['scroll'] > mobile['tables'][0]['width'], mobile)
        screenshot('inventory-mobile')
        cli('click', '#rows tr:first-child button')
        bounds = evaluate("({left:document.querySelector('#details').getBoundingClientRect().left,right:document.querySelector('#details').getBoundingClientRect().right,width:innerWidth,document:document.documentElement.scrollWidth})")
        check('inventory mobile dialog fits viewport', bounds['left'] >= 0 and bounds['right'] <= 390 and bounds['document'] <= 390, bounds)
        screenshot('inventory-dialog-mobile')
        cli('press', 'Escape')

        cli('open', schedule_url)
        cli('snapshot', '-i')
        clean_own_browser_data()
        clean = evaluate("({title:document.querySelector('#schedule-title').value,scope:document.querySelector('#scope').value,keys:Object.keys(sessionStorage),time:document.querySelector('#rows tr').cells[3].textContent,exact:document.querySelector('#exact-value').textContent})")
        check('second Pack has separate fields scope storage and explicit UTC time', clean['title'] == '' and clean['scope'] == 'studio-a' and not any('inventory' in key for key in clean['keys']) and clean['time'] == '2026-10-21 00:01:02' and clean['exact'] == '000900719925474099312345', clean)
        cli('select', '#schedule-layout', 'circle')
        cli('fill', '#schedule-title', 'schedule-own-focused-value')
        cli('reload')
        wait("document.querySelector('#schedule-layout').value==='circle'")
        check('second layout independently persists focused input', evaluate("document.querySelector('#schedule-title').value") == 'schedule-own-focused-value')
        mobile = evaluate("({width:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth})")
        check('schedule 390px page has no horizontal page overflow', mobile['document'] <= 390 and mobile['body'] <= 390, mobile)
        screenshot('schedule-mobile')
        cli('click', '#rows tr:first-child button')
        wait("document.querySelector('#details-status').textContent==='已读取详情。'")
        check('second layout uses shared accessible projected details', evaluate("document.querySelector('#details').open && document.activeElement.id==='close-details' && !document.querySelector('#details-content').textContent.includes('此字段没有被允许展示')"))
        screenshot('schedule-dialog-mobile')
        cli('press', 'Escape')
        cli('set', 'viewport', '1280', '900')
        screenshot('schedule-desktop')
        cli('open', inventory_url)
        wait("document.querySelector('#inventory-kind').value==='boxes'")
        state = field_state()
        check('inventory saved context survives visiting another Pack origin', state['note'] == 'typed-before-options-loaded' and state['kind'] == 'boxes', state)
        screenshot('inventory-desktop')
        error = None
    except Exception as failure:
        error = {'type': type(failure).__name__, 'message': str(failure)}
        try:
            screenshot('browser-failure')
        except Exception:
            pass
    result = {'ok': error is None and all(row['passed'] for row in checks), 'scope': 'real Chromium UI with two owned synthetic Pack panels',
              'realBusinessCasesExecuted': 0, 'session': session, 'inventoryURL': inventory_url, 'scheduleURL': schedule_url,
              'startedAt': int(started), 'durationSeconds': round(time.time()-started, 2), 'checks': checks, 'screenshots': screenshots,
              'limitations': ['Task transport and permission-denial fault are explicitly synthetic; no real business API or acceptance is claimed.',
                              'Clipboard success checks the exact argument and resolved native write; OS clipboard readback is not asserted. Denial is injected only for this synthetic page.']}
    if error:
        result['error'] = error
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory-url', required=True, type=loopback_url)
    parser.add_argument('--schedule-url', required=True, type=loopback_url)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--session', default='workbench-browser-'+uuid.uuid4().hex[:12])
    arguments = parser.parse_args()
    result = accept(arguments.inventory_url, arguments.schedule_url, report=arguments.report, session=arguments.session)
    print(json.dumps({key:value for key,value in result.items() if key!='checks'}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
