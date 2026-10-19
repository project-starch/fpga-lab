"""Run one queued job on one board: lease, files, flash, power cycle, load,
capture UART, power off, release.

The sequence is the one verified on both boards: a bitstream is always
flashed NON-volatile and the board power-cycled, then left settle_s seconds
before anything touches JTAG. A volatile flash never brings up the CVA6 debug
TAP on board 2, so it is never used here.
"""

import os
import re
import time

from .client import ConsoleError

RENEW_EVERY_S = 60


class Cancelled(Exception):
    pass


def file_name(path, sha, ext):
    stem = os.path.splitext(os.path.basename(path))[0]
    tag = sha[:12]
    return f'{stem}{ext}' if tag in stem else f'{stem}_{tag}{ext}'


def board_is_free(lease_state, now=None):
    now = time.time() if now is None else now
    if lease_state.get('lease') or lease_state.get('offer'):
        return False
    for r in lease_state.get('reservations') or []:
        if r['start'] <= now + 60 and r['end'] > now:     # booked now or within a minute
            return False
    return True


class JobLog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg):
        with open(self.path, 'a') as fh:
            fh.write(time.strftime('%Y-%m-%d %H:%M:%S ') + msg + '\n')

    def uart(self, text):
        with open(self.path, 'a') as fh:
            fh.write('----- UART -----\n' + text + '\n----- end UART -----\n')


class Runner:
    def __init__(self, db, consoles, boards_cfg, data_dir, sleep=time.sleep, clock=time.time):
        self.db = db
        self.consoles = consoles        # board -> Console
        self.boards_cfg = boards_cfg
        self.data_dir = data_dir
        self.sleep = sleep
        self.clock = clock

    def log_path(self, jid):
        return os.path.join(self.data_dir, 'jobs', f'{jid}.log')

    # ---------------------------------------------------------- selection

    def pick_board(self, job, busy=()):
        """First free board allowed for this job, or (None, reason)."""
        reasons = []
        for b, c in self.consoles.items():
            if b in busy:
                reasons.append(f'board {b}: running a job')
                continue
            if job['board_req'] not in ('any', b):
                continue
            if job['board_req'] == 'any' and not self.db.is_qualified(b, job['bit_sha']):
                reasons.append(f'board {b}: bitstream not qualified here')
                continue
            try:
                st = c.lease()
            except ConsoleError as e:
                why = 'console has no leases yet' if e.status == 404 else e.message
                reasons.append(f'board {b}: {why}')
                continue
            if not board_is_free(st, self.clock()):
                L = st.get('lease')
                reasons.append(f"board {b}: held by {L['holder']}" if L else f'board {b}: booked or offered')
                continue
            return b, None
        return None, '; '.join(reasons) or 'no board matches'

    # ---------------------------------------------------------------- run

    def run(self, job, board):
        jid = job['id']
        c = self.consoles[board]
        log = JobLog(self.log_path(jid))
        settle = float(self.boards_cfg.get(board, {}).get('settle_s', 35))
        self.db.set_job(jid, state='running', board=board, started=self.clock())
        log(f'job {jid} for {job["owner"]} on board {board}')
        token = None
        last_renew = [self.clock()]
        yielded = [False]

        def tick():
            if (self.db.job(jid) or {}).get('state') == 'cancelled':
                raise Cancelled()
            if self.clock() - last_renew[0] < RENEW_EVERY_S or yielded[0]:
                return
            try:
                c.renew(token)
            except ConsoleError as e:
                if e.code == 'yield':
                    # Someone is waiting. A job is one cycle: finish it on the
                    # time already granted, take no more.
                    yielded[0] = True
                    log('someone is waiting: finishing this job without renewing')
                else:
                    raise
            last_renew[0] = self.clock()

        def wait(seconds):
            end = self.clock() + seconds
            while self.clock() < end:
                tick()
                self.sleep(min(1.0, max(0.0, end - self.clock())))

        state, reason, matched = 'failed', None, None
        live = None
        try:
            L = c.take(f'job{jid}:{job["owner"]}', job.get('purpose') or f'job {jid}', kind='agent')
            token = L['token']
            log('lease taken')

            bit_name = file_name(job['bit_path'], job['bit_sha'], '.bit')
            img_name = file_name(job['img_path'], job['img_sha'], '.bin')
            if bit_name not in c.files('bitstreams'):
                log(f'uploading {bit_name}')
                c.upload('bitstreams', job['bit_path'], bit_name, token)
            if img_name not in c.files('images'):
                log(f'uploading {img_name}')
                c.upload('images', job['img_path'], img_name, token)

            st = c.state()
            if st.get('nv_bitstream_sha256') != job['bit_sha']:
                log(f'flashing {bit_name} non-volatile')
                c.flash(bit_name, token)
                c.wait_for('flash_state', timeout=900, tick=tick)
                log('flash done')
            else:
                log('bitstream already resident')

            live = c.socket(token).__enter__()
            live.set_power(False)
            wait(3)
            live.set_power(True)
            log(f'power on; settling {settle:.0f} s')
            wait(settle)

            mark = len(live.text())
            c.load(img_name, job['img_sha'], token)
            c.wait_for('load_state', timeout=1800, tick=tick)
            log('image loaded; capturing UART')

            rx = re.compile(job['until_regex']) if job.get('until_regex') else None
            end = self.clock() + job['timeout_s']
            matched = 0
            while self.clock() < end:
                if rx and rx.search(live.text()[mark:]):
                    matched = 1
                    break
                tick()
                self.sleep(1)
            log.uart(live.text()[mark:])
            if rx:
                state = 'done' if matched else 'failed'
                reason = None if matched else f'no match for /{job["until_regex"]}/ in {job["timeout_s"]} s'
            else:
                state = 'done'
        except Cancelled:
            reason = 'cancelled'
            log('cancelled')
        except ConsoleError as e:
            reason = f'{e.code}: {e.message}'
            log(f'error: {reason}')
        except Exception as e:      # a job must never take the dispatcher down
            reason = f'{type(e).__name__}: {e}'
            log(f'error: {reason}')
        finally:
            if live is not None:
                try:
                    live.set_power(False)
                except Exception as e:
                    log(f'power off failed: {e}')
                live.__exit__(None, None, None)
            if token:
                try:
                    c.release(token)
                    log('lease released')
                except ConsoleError as e:
                    log(f'release failed: {e.message}')
            if self.db.job(jid)['state'] == 'cancelled':
                state = 'cancelled'
            self.db.set_job(jid, state=state, reason=reason, matched=matched,
                            finished=self.clock())
            log(f'job {jid} {state}' + (f' ({reason})' if reason else ''))
        return state
