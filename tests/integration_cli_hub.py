"""The `fpga` CLI and the hub against the real console code (no hardware).
Run: python tests/integration_cli_hub.py <fpga-remote-checkout> <python>"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
repo, py = sys.argv[1], sys.argv[2]
port = 5097
tmp = tempfile.mkdtemp(prefix='lab-cli-')
env = dict(os.environ, XDG_STATE_HOME=tmp, FPGA_AUDIT_LOG=tmp + '/audit.jsonl',
           LEASE_ENFORCE='on', LEASE_HUB_TOKEN='hubsecret', FPGA_HEALTH_STATUS=tmp + '/none.json')
srv = subprocess.Popen([py, os.path.join(repo, 'tests', 'dev_server.py'), str(port)],
                       env=env, stdout=open(tmp + '/srv.log', 'w'), stderr=subprocess.STDOUT)
open(tmp + '/url', 'w').write(f'http://127.0.0.1:{port}/')
open(tmp + '/hubtok', 'w').write('hubsecret')
cfg = {'boards': {'2': {'url_file': tmp + '/url', 'settle_s': 35}}, 'data_dir': tmp + '/data',
       'hub_url': 'http://127.0.0.1:1', 'hub_token_file': tmp + '/hubtok'}
json.dump(cfg, open(tmp + '/cfg.json', 'w'))
cenv = dict(os.environ, FPGA_LAB_CONFIG=tmp + '/cfg.json', HOME=tmp)
res = []


def fpga(*args):
    r = subprocess.run([py, '-m', 'fpga_lab.cli', *args], env=cenv, cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def check(name, cond, out=''):
    res.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  [{out}]'), flush=True)


try:
    for _ in range(60):
        rc, out = fpga('status')
        if 'unreachable' not in out:
            break
        time.sleep(0.5)
    check('status free', 'board 2: free' in out and 'leases on' in out, out)
    rc, out = fpga('take', '2', '--for', '30m', '--why', 'cli test', '--name', 'Sam')
    check('take', rc == 0 and 'yours until' in out, out)
    st = os.stat(tmp + '/.local/state/fpga-lab/leases.json')
    check('token file mode 600', oct(st.st_mode & 0o777) == '0o600')
    rc, out = fpga('status')
    check('status held by Sam', 'held by Sam' in out and 'cli test' in out, out)
    rc, out = fpga('extend', '2')
    check('extend', rc == 0 and 'extended until' in out, out)
    rc, out = fpga('take', '2', '--name', 'Kim')
    check('second take refused', rc == 1 and 'held by' in out, out)
    # hub against the real console: poll + push a booking
    from fpga_lab import config as cm
    os.environ['FPGA_LAB_CONFIG'] = tmp + '/cfg.json'
    from fpga_lab.hub import create_app
    app = create_app(cm.load(), start_threads=False)
    mon = app.config['monitor']
    mon.poll()
    snap = mon.snapshot()['2']
    check('hub sees holder', snap['reachable'] and snap['lease']['holder'] == 'Sam', snap)
    check('hub sees health unknown (no status file)', snap['health'] == 'unknown')
    now = time.time()
    app.config['db'].add_booking('2', 'Kim', 'later', now + 3600, now + 7200)
    mon.push_bookings()
    rc, out = fpga('status')
    check('status shows booking', 'booked Kim' in out, out)
    rc, out = fpga('release', '2')
    check('release', rc == 0 and 'released' in out, out)
    rc, out = fpga('release', '2')
    check('second release fails cleanly', rc == 1, out)
    http = app.test_client()
    page = http.get('/').data.decode()
    check('page renders board', 'Board #2' in page and 'Kim' in page)
    check('page carries no console URL', f':{port}/' not in page)
finally:
    srv.terminate()
    srv.wait(10)
print('RESULT', sum(res), 'of', len(res))
sys.exit(0 if all(res) else 1)
