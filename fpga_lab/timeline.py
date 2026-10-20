"""Who had, has and will have each board: one row per board on a time axis.

Past     what the hub saw each minute (usage table): who held the lease
Now      the current lease, to its end
Future   calendar bookings, and queued runs placed where the dispatcher would
         start them -- an ESTIMATE: a run's length is its settle time plus a
         flash/load allowance plus its timeout, which is an upper bound.

Rendered as an inline SVG so the page needs no script or library.
"""

import html
import time

from .owners import colour, owner_of, person

FLASH_LOAD_ALLOWANCE_S = 240      # NV flash ~95 s + image load; generous on purpose


def _merge_minutes(rows):
    """[(minute, holder, kind)] sorted -> [(start_s, end_s, holder, kind)]."""
    segs = []
    for minute, holder, kind in rows:
        t = minute * 60
        if segs and segs[-1][2] == holder and segs[-1][3] == kind and t - segs[-1][1] <= 60:
            segs[-1][1] = t + 60
        else:
            segs.append([t, t + 60, holder, kind])
    return [tuple(s) for s in segs]


def past_segments(db, board, t0, t1):
    rows = db.q("""SELECT minute, holder, kind, owner FROM usage
                   WHERE board=? AND minute>=? AND minute<? AND holder IS NOT NULL
                   ORDER BY minute""", board, int(t0 // 60), int(t1 // 60) + 1)
    segs = _merge_minutes([(r['minute'], r['holder'], (r['kind'], r['owner'])) for r in rows])
    return [(s, e, h, k[0], k[1]) for s, e, h, k in segs]


def estimate_jobs(db, status, boards_cfg, now):
    """Place queued jobs on boards in queue order. Returns
    {job_id: (board, start, end)}; a job with no possible board is absent."""
    free_at = {}
    bookings = {}
    for b in boards_cfg:
        s = status.get(b) or {}
        L = s.get('lease')
        t = now
        if L:
            t = max(t, L.get('until') or now)
        if s.get('offer'):
            t = max(t, s['offer'].get('until') or now)
        free_at[b] = t
        bookings[b] = [(x['start'], x['end']) for x in db.bookings(b, since=now)]

    def first_gap(b, start, dur):
        t = start
        for bs, be in sorted(bookings[b]):
            if t + dur <= bs:
                break
            if be > t:
                t = max(t, be)
        return t

    placed = {}
    for job in db.next_queued():
        cands = [job['board_req']] if job['board_req'] != 'any' else [
            b for b in boards_cfg if db.is_qualified(b, job['bit_sha'])]
        cands = [b for b in cands if b in boards_cfg and (status.get(b) or {}).get('enforce') != 'absent']
        if not cands:
            continue
        dur = (float(boards_cfg[cands[0]].get('settle_s', 35)) + FLASH_LOAD_ALLOWANCE_S
               + job['timeout_s'])
        best = min(cands, key=lambda b: first_gap(b, free_at[b], dur))
        start = first_gap(best, free_at[best], dur)
        placed[job['id']] = (best, start, start + dur)
        free_at[best] = start + dur
    return placed


def build(db, status, boards_cfg, now, back_h=6, ahead_h=24, cfg=None):
    cfg = cfg or {}
    t0, t1 = now - back_h * 3600, now + ahead_h * 3600
    est = estimate_jobs(db, status, boards_cfg, now)
    queued = {j['id']: j for j in db.next_queued()}
    rows = []
    for b in boards_cfg:
        segs = []
        for s, e, holder, kind, owner in past_segments(db, b, t0, now):
            owner = '(unknown)' if kind == 'unknown' else person(owner or holder, cfg)
            segs.append({'start': s, 'end': min(e, now), 'label': holder, 'owner': owner,
                         'type': 'used'})
        L = (status.get(b) or {}).get('lease')
        if L:
            # The usage table covers up to the last poll; draw the rest of the
            # current lease from now to its end.
            segs.append({'start': now, 'end': L['until'], 'label': L['holder'],
                         'owner': owner_of(L, cfg), 'type': 'now'})
        for x in db.bookings(b, since=t0):
            segs.append({'start': x['start'], 'end': x['end'], 'label': x['holder'],
                         'owner': person(x['holder'], cfg), 'type': 'booking',
                         'title': x.get('purpose') or ''})
        for jid, (jb, s, e) in est.items():
            if jb == b:
                segs.append({'start': s, 'end': e, 'label': f"#{jid} {queued[jid]['owner']}",
                             'owner': person(queued[jid]['owner'], cfg), 'type': 'job-est'})
        rows.append({'board': b, 'segs': segs})
    return {'t0': t0, 't1': t1, 'now': now, 'rows': rows, 'estimates': est}


def owners_in(tl):
    seen = []
    for row in tl['rows']:
        for s in row['segs']:
            o = s.get('owner')
            if o and o not in seen and s['end'] > tl['t0'] and s['start'] < tl['t1']:
                seen.append(o)
    return seen


def render_svg(tl, width=960, row_h=34, label_w=78, cfg=None, db=None):
    t0, t1, now = tl['t0'], tl['t1'], tl['now']
    plot_w = width - label_w - 10
    top = 26
    height = top + row_h * len(tl['rows']) + 8

    def x(t):
        return label_w + (min(max(t, t0), t1) - t0) / (t1 - t0) * plot_w

    out = [f'<svg class="tl" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Board timeline from {time.strftime("%H:%M", time.localtime(t0))} '
           f'to {time.strftime("%H:%M %a", time.localtime(t1))}">',
           '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
           'patternTransform="rotate(45)"><rect width="6" height="6" class="tl-book-bg"/>'
           '<line x1="0" y1="0" x2="0" y2="6" class="tl-book-line"/></pattern></defs>']
    # hour grid
    step = 3600 if (t1 - t0) <= 36 * 3600 else 6 * 3600
    t = (int(t0) // step + 1) * step
    while t < t1:
        lt = time.localtime(t)
        xx = x(t)
        out.append(f'<line x1="{xx:.1f}" y1="{top - 4}" x2="{xx:.1f}" y2="{height - 6}" class="tl-grid"/>')
        if lt.tm_hour % (3 if step == 3600 else 12) == 0:
            lab = time.strftime('%a %H:%M' if lt.tm_hour == 0 else '%H:%M', lt)
            out.append(f'<text x="{xx:.1f}" y="{top - 9}" class="tl-tick" text-anchor="middle">{lab}</text>')
        t += step
    for i, row in enumerate(tl['rows']):
        y = top + i * row_h
        out.append(f'<text x="4" y="{y + row_h / 2 + 4:.1f}" class="tl-row">Board #{html.escape(row["board"])}</text>')
        out.append(f'<rect x="{label_w}" y="{y + 4}" width="{plot_w}" height="{row_h - 8}" class="tl-lane"/>')
        for s in sorted(row['segs'], key=lambda s: s['start']):
            if s['end'] <= t0 or s['start'] >= t1:
                continue
            x0, x1 = x(s['start']), x(s['end'])
            w = max(x1 - x0, 2)
            cls = 'tl-' + s['type'].replace('_', '-')
            col = colour(s.get('owner'), cfg, db)
            who = s.get('owner')
            tip = ((f"{who} · " if who and who != s['label'] else '') +
                   f"{s['label']}: {time.strftime('%a %H:%M', time.localtime(s['start']))}"
                   f"–{time.strftime('%H:%M', time.localtime(s['end']))}")
            if s['type'] == 'job-est':
                tip += ' (estimated)'
            if s.get('title'):
                tip += f" · {s['title']}"
            if s['type'] == 'booking':
                style = f'fill:{col};fill-opacity:.18;stroke:{col};stroke-width:1.5'
            elif s['type'] == 'job-est':
                style = f'fill:{col};fill-opacity:.12;stroke:{col};stroke-width:1.5;stroke-dasharray:4 3'
            elif s['type'] == 'used':
                style = f'fill:{col};fill-opacity:.6'
            else:
                style = f'fill:{col}'
            out.append(f'<g><title>{html.escape(tip)}</title>'
                       f'<rect x="{x0:.1f}" y="{y + 6}" width="{w:.1f}" height="{row_h - 12}" rx="3" class="{cls}" style="{style}"/>')
            if w > 46:
                text = s.get('owner') or s['label']
                lab = text if len(text) * 7 < w - 8 else text[:max(1, int((w - 8) / 7) - 1)] + '…'
                tcls = 'tl-seg-text' + (' on-fill' if s['type'] in ('used', 'now') else '')
                out.append(f'<text x="{x0 + 5:.1f}" y="{y + row_h / 2 + 4:.1f}" class="{tcls}">{html.escape(lab)}</text>')
            out.append('</g>')
    xn = x(now)
    out.append(f'<line x1="{xn:.1f}" y1="{top - 4}" x2="{xn:.1f}" y2="{height - 4}" class="tl-now"/>')
    out.append(f'<text x="{xn + 3:.1f}" y="{height - 2}" class="tl-now-text">now</text>')
    out.append('</svg>')
    return '\n'.join(out)
