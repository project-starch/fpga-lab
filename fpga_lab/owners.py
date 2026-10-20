"""Who is behind a lease: the person an agent runs for.

A lease holder is a free-text label ("board-lane", "job12:diego"). The person
behind it is resolved, in order, from:
  1. the console's Tailscale identity of the client (lease "who"), mapped
     through config "people" (login -> display name) when present;
  2. a hub job's holder "job<N>:<owner>";
  3. config "holder_owner" (holder label -> person), e.g. {"board-lane": "Alexey"};
  4. the holder label itself.
Each person gets a stable colour from a fixed palette.
"""

import hashlib
import re

PALETTE = ['#2f7ed8', '#d9822b', '#2e9d6b', '#b23a8b', '#7a5cc4', '#c23b3b',
           '#3a9fb0', '#8a8d24', '#5b6b7a', '#d05c9a', '#4a7f2c', '#a0662a']
UNKNOWN = '#9aa3a0'

_JOB = re.compile(r'^job\d+:(.+)$')


def person(label, cfg):
    """The person behind a label: a lease holder, a booking holder, a job
    owner. Applied the same way everywhere, so one person is one colour."""
    if not label or label.startswith('('):
        return label
    m = _JOB.match(label)
    if m:
        label = m.group(1)
    return ((cfg or {}).get('holder_owner') or {}).get(label, label)


def owner_of(lease, cfg):
    if not lease:
        return None
    people = (cfg or {}).get('people') or {}
    who = lease.get('who')
    if who:
        return people.get(who, who.split('@')[0])
    return person(lease.get('holder') or '', cfg)


def colour(owner, cfg=None, db=None):
    """Fixed colour from config, else the person's stable slot in the
    palette (db), else a hash (which can collide; used only without a db)."""
    if not owner or owner.startswith('('):
        return UNKNOWN
    fixed = ((cfg or {}).get('colours') or {}).get(owner)
    if fixed:
        return fixed
    if db is not None:
        return PALETTE[db.colour_index(owner) % len(PALETTE)]
    h = int(hashlib.sha256(owner.encode()).hexdigest()[:8], 16)
    return PALETTE[h % len(PALETTE)]
