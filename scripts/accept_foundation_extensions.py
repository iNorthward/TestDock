#!/usr/bin/env python3
"""Synthetic signed HTTP, capability evidence and two owned Pack UI acceptance.

--serve keeps only the temporary panels alive for browser verification. It never
reads business configuration, starts business clients or calls external services.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from create_project_pack import create
from export_platform import build

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SYNTHETIC_KEY = b'public-synthetic-acceptance-key'


def digest(method, path, body, connection):
    message = method.encode() + b'\n' + path.encode() + b'\n' + hashlib.sha256(body).hexdigest().encode() + b'\n' + connection.encode()
    return hmac.new(SYNTHETIC_KEY, message, hashlib.sha256).hexdigest()


SIGNED_PROBE = r'''
import hashlib,hmac,json,sys
from pathlib import Path
from urllib.parse import urlsplit
root=Path(sys.argv[1]);base=sys.argv[2]
sys.path[:0]=[str(root),str(root/'test-platform'),str(root/'test-platform/common')]
from api_client import ApiSession,JSON_NULL
checks=[]
def check(name,condition):
    checks.append({'name':name,'passed':bool(condition)})
    if not condition:raise AssertionError(name)
class Auth:
    def build_headers(self,token=None,**options):return {'Content-Type':'application/json'}
    def validate_headers(self,headers):pass
class Signer:
    def sign(self,prepared):
        self.prepared=prepared
        parsed=urlsplit(prepared.url);target=parsed.path+('?' + parsed.query if parsed.query else '')
        value=prepared.method.encode()+b'\n'+target.encode()+b'\n'+hashlib.sha256(prepared.body or b'').hexdigest().encode()+b'\n'+prepared.headers['Connection'].encode()
        return {'X-Synthetic-Signature':hmac.new(b'public-synthetic-acceptance-key',value,hashlib.sha256).hexdigest()}
signer=Signer();session=ApiSession(base=base,request_auth=Auth(),request_signer=signer)
cases=[('no body',{}),('empty body',{'raw_body':b''}),('JSON null',{'data':JSON_NULL}),
       ('Unicode and raw JSON types',{'data':{'text':'雪 + %','id':900719925474099312345,'amount':'1.2300','flag':False}}),
       ('exact numeric JSON body',{'raw_body':b'{"amount":12345678901234567890.123456789,"id":9007199254740993}'})]
for label,options in cases:
    status,response=session.request('POST','/echo',with_token=False,**options)
    check('signed wire body: '+label,status==200 and response['signatureValid'] and response['bodySha256']==hashlib.sha256(signer.prepared.body or b'').hexdigest())
status,response=session.get('/echo',params=[('q','a b+c%'),('q','雪')],with_token=False)
check('signed query uses actual encoded path',status==200 and response['signatureValid'] and 'q=a+b%2Bc%25' in response['target'])
status,response=session.get('/deny',with_token=False)
check('authentication rejection is returned without replay',status==401)
status,response=session.get('/attachment',with_token=False)
check('default attachment response keeps only metadata',status==200 and response=={'_binary':True,'_size':5,'_content_type':'application/octet-stream'})
status,response=session.get('/attachment',with_token=False,max_response_bytes=5)
check('bounded attachment matches bytes over HTTP',status==200 and response.content==b'\x00\xffbin')
attachment=response.content
status,response=session.post('/echo',raw_body=attachment,with_token=False)
check('downloaded attachment round trip matches original digest',status==200 and response['bodySha256']==hashlib.sha256(attachment).hexdigest())
status,response=session.get('/attachment',with_token=False,max_response_bytes=4)
check('oversize attachment exposes no partial bytes',status==200 and response['_response_too_large'] is True and response['_max_bytes']==4)
status,response=session.get('/deny',with_token=False,max_response_bytes=1024)
check('bounded call preserves JSON error and HTTP status',status==401 and type(response['signatureValid']) is bool)
status,response=session.get('/redirect',with_token=False)
check('signed same-origin redirect is not followed',status==302)
class Fake:
    def request_prepared(self,prepared,**options):
        self.prepared=prepared;return 200,{'synthetic':True}
fake=Fake();fake_session=ApiSession(base=base,request_auth=Auth(),request_signer=signer,prepared_transport=fake)
fake_session.post('/echo',data={'text':'雪 + %','id':900719925474099312345,'amount':'1.2300','flag':False},with_token=False)
check('prepared fake receives signed bytes unchanged',fake.prepared.body==signer.prepared.body and fake.prepared.url==signer.prepared.url)
print(json.dumps(checks))
'''


def run_acceptance(*, serve=False, state_file=None):
    checks = []
    processes = []
    def stop_panels():
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if not log.closed:
                log.close()
    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    class Echo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.do_POST()
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            path = urlsplit(self.path).path
            valid = hmac.compare_digest(self.headers.get('X-Synthetic-Signature', ''), digest(self.command, self.path, body, self.headers.get('Connection', '')))
            if path == '/attachment' and valid:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.end_headers()
                self.wfile.write(b'\x00\xffbin')
                return
            status = 302 if path == '/redirect' else 401 if path == '/deny' or not valid else 200
            payload = {'signatureValid': valid, 'bodySha256': hashlib.sha256(body).hexdigest(), 'target': self.path}
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            if status == 302:
                self.send_header('Location', '/echo')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    business = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
    thread = threading.Thread(target=business.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='foundation-extensions-') as temporary, ExitStack() as cleanup:
            cleanup.callback(stop_panels)
            directory = Path(temporary)
            build(directory / 'release')
            root = directory / 'release/platform'
            probe = subprocess.run([sys.executable, '-B', '-c', SIGNED_PROBE, str(root),
                                    'http://127.0.0.1:' + str(business.server_port)],
                                   capture_output=True, text=True, timeout=30,
                                   env={**{k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ},
                                        'PLATFORM_ENV_FILE': '/dev/null', 'PYTHONDONTWRITEBYTECODE': '1'})
            if probe.returncode:
                raise RuntimeError('Synthetic signed HTTP probe failed: ' + probe.stderr[-2000:])
            checks.extend(json.loads(probe.stdout))
            panels = {}
            ports = {}
            for page in ('inventory', 'schedule'):
                with socket.socket() as stream:
                    stream.bind(('127.0.0.1', 0))
                    ports[page] = stream.getsockname()[1]
            for pack, page in [('sample_inventory', 'inventory'), ('sample_schedule', 'schedule')]:
                port = ports[page]
                folder = create(pack, 'Synthetic ' + page, port, root=root)
                assets = folder / 'assets'
                assets.mkdir()
                demo = (root / 'examples/workbench_components/demo.js').read_text().replace('demo_inventory', 'sample_inventory').replace('demo_schedule', 'sample_schedule')
                (assets / 'demo.js').write_text(demo)
                shutil.copyfile(root / 'examples/workbench_components/demo.css', assets / 'demo.css')
                html = (root / 'examples/workbench_components' / (page + '.html')).read_text()
                html = html.replace('./demo.js', '/pack-assets/demo.js').replace('src="demo.js"', 'src="/pack-assets/demo.js"')
                html = html.replace('./demo.css', '/pack-assets/demo.css')
                html = html.replace('demo_inventory', 'sample_inventory').replace('demo_schedule', 'sample_schedule')
                other = 'schedule' if page == 'inventory' else 'inventory'
                html = html.replace('./' + other + '.html', 'http://127.0.0.1:' + str(ports[other]))
                (folder / 'panel.html').write_text(html)
                path = folder / 'pack.json'
                manifest = json.loads(path.read_text())
                manifest['panel_html'] = str(Path('packs') / pack / 'panel.html')
                manifest['platform_api']['features'].append('workbench_components')
                if page == 'schedule':
                    manifest['capability_adoption']['items']['http'].update(mode='pack', reason='Synthetic page demonstrates an explicit Pack request implementation')
                path.write_text(json.dumps(manifest, indent=2))
                from platform_capabilities import adoption_report
                from project_tasks import capability_context, record_capability
                declaration = adoption_report(manifest, root=root, pack_id=pack)
                http = next(row for row in declaration['items'] if row['id'] == 'http')
                check(pack + ': declaration does not imply verification', http['verification']['status'] == 'not_run' and http['mode'] == ('core' if page == 'inventory' else 'pack'))
                context = capability_context(pack, 'http', root=root)
                evidence = root / 'artifacts' / pack / 'synthetic-http.json'
                evidence.parent.mkdir(parents=True)
                evidence.write_text(json.dumps({**context, 'ok': True, 'status': 'passed', 'scope': 'synthetic',
                                                'verifiedAt': int(time.time())}))
                record_capability(pack, 'http', evidence, root=root)
                verified = adoption_report(manifest, root=root, pack_id=pack)
                check(pack + ': bound synthetic evidence remains scoped', next(row for row in verified['items'] if row['id'] == 'http')['verification']['scope'] == 'synthetic')
                evidence.write_text('{}')
                stale = adoption_report(manifest, root=root, pack_id=pack)
                check(pack + ': modified evidence is stale', next(row for row in stale['items'] if row['id'] == 'http')['verification']['status'] == 'stale')
                env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
                env.update(PLATFORM_ENV_FILE=str(root / ('.env.' + pack)), PLATFORM_PACKS=pack,
                           PYTHONDONTWRITEBYTECODE='1', TZ='Asia/Shanghai')
                log = (directory / (pack + '.log')).open('w')
                process = subprocess.Popen([sys.executable, '-B', '-u', str(root / 'test-platform/server.py'),
                                            '--pack=' + pack, '--port=' + str(port)],
                                           cwd=root, env=env, stdout=log, stderr=log)
                processes.append((process, log))
                base = 'http://127.0.0.1:' + str(port)
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('Temporary panel failed: ' + Path(log.name).read_text()[-2000:])
                    try:
                        with OPENER.open(base + '/api/session', timeout=1) as response:
                            token = json.load(response)['token']
                        request = urllib.request.Request(base + '/api/platform', headers={'X-TestDock-Token': token})
                        with OPENER.open(request, timeout=2) as response:
                            platform = json.load(response)['data']
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Temporary panel startup timed out')
                        time.sleep(.1)
                check(pack + ': owned platform and capability declaration', platform['pack'] == pack and platform['capabilityAdoption']['pack'] == pack)
                check(pack + ': runtime snapshot initially matches saved sources', platform['runtimeSource']['restartRequired'] is False)
                changed = json.loads(path.read_text())
                changed['capability_adoption']['items']['http']['reason'] += ' (saved declaration changed)'
                path.write_text(json.dumps(changed, indent=2))
                request = urllib.request.Request(base + '/api/platform', headers={'X-TestDock-Token': token})
                with OPENER.open(request, timeout=2) as response:
                    drifted = json.load(response)['data']
                check(pack + ': saved declaration change requires process restart', drifted['runtimeSource']['restartRequired'] is True)
                path.write_text(json.dumps(manifest, indent=2))
                with OPENER.open(base, timeout=2) as response:
                    page_text = response.read().decode()
                check(pack + ': shared components and security client loaded', '/assets/workbench-components.js' in page_text and '/assets/security-client.js' in page_text)
                with OPENER.open(base + '/pack-assets/demo.js', timeout=2) as response:
                    check(pack + ': Pack UI asset available', response.status == 200)
                panels[pack] = {'url': base, 'pack': pack}
            check('two independent Pack processes', len({row['url'] for row in panels.values()}) == 2)
            if state_file:
                Path(state_file).write_text(json.dumps({'panels': panels, 'temporaryRoot': str(root)}, indent=2))
            if serve:
                print(json.dumps({'browserReady': True, 'panels': panels}), flush=True)
                try:
                    while all(process.poll() is None for process, _ in processes):
                        time.sleep(.5)
                    raise RuntimeError('An owned synthetic panel exited during browser acceptance')
                except KeyboardInterrupt:
                    pass
            result = {'ok': all(row['passed'] for row in checks), 'checks': checks,
                      'scope': 'owned loopback and two temporary synthetic Packs', 'realBusinessCasesExecuted': 0}
        return result
    finally:
        stop_panels()
        business.shutdown()
        business.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        result = run_acceptance(serve=args.serve, state_file=args.state)
    except Exception as error:
        result = {'ok': False, 'errorType': type(error).__name__, 'error': str(error), 'realBusinessCasesExecuted': 0}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
