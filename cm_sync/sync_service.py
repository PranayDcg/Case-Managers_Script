"""
CM Salesforce sync service: keeps every case manager's Convoso campaign matching Salesforce.

Weekdays only (Mon-Fri):
  4:30 PM  posts a preview of today's changes in #cm-uploads (no changes made)
  5:00 PM  runs the sync. If it would change more than MAX_CHANGES leads, it waits for `sync proceed`.
Everything is posted in #cm-uploads, logged to logs/, and every applied change goes in changes.csv.

Slack commands (all start with "sync", so they never collide with the CM upload bot's commands):
  sync help | sync status | sync preview | sync now | sync proceed | sync cancel
  sync stop  (emergency: stops a running sync after the current lead AND pauses the schedule)
  sync hold | sync resume | sync history | sync client <file# or phone>

Started by start_sync.vbs (Startup folder) -> start_sync.bat (restart loop). One instance only (sync.lock).
"""
import csv, json, msvcrt, os, re, sys, threading, time, logging, traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cm_sf_sync as S  # noqa: E402

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

STATE_FILE = HERE / 'state.json'
CHANGES_CSV = HERE / 'changes.csv'
LOCK_FILE = HERE / 'sync.lock'
LOG_DIR = HERE / 'logs'
LOG_DIR.mkdir(exist_ok=True)
CM_ENV = HERE.parent / 'cm' / '.env'          # the CM bot's Slack token + channel (same bot, same channel)
PARENT_ENV = HERE.parent / '.env'             # Asana token (shared by the fleet)
ASANA_EPIC_CM_GID = '1213813165374346'        # same epic the weekly CM upload reports go to
ASANA_USER_GID = '1210889312062695'           # Pranay

PREVIEW_AT = (16, 30)
RUN_AT = (17, 0)
CATCH_UP_UNTIL = (19, 0)
AUTO_SYNC_AT = (18, 0)        # if nobody presses Sync or Reject, the 5 PM plan syncs by itself at 6 PM
AUTO_MIN_WAIT = 30 * 60       # a late plan (PC was asleep) still waits at least 30 min for a Reject      # if the PC was off/asleep at 5 PM, still run when it comes back before 7 PM
PENDING_MAX_AGE_H = 3         # a waiting plan older than this must be recomputed (`sync now`)
POLL = 20

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.FileHandler(LOG_DIR / 'service.log', encoding='utf-8'), logging.StreamHandler(sys.stdout)])
log = logging.getLogger('cm_sync')
S.log = log

env = {**S._read_env_file(PARENT_ENV), **S._read_env_file(CM_ENV), **S._read_env_file(HERE / '.env')}
SLACK_TOKEN, CHANNEL = env.get('CM_SLACK_BOT_TOKEN'), env.get('CM_CHANNEL_ID')
APP_TOKEN = env.get('CM_SLACK_APP_TOKEN')     # xapp-... (Socket Mode). Without it there are no buttons, only typed commands.
# Who may approve / run / resume (QA B7). Anyone may STOP or HOLD: stopping is always safe.
APPROVERS = {u.strip() for u in (env.get('CM_SYNC_APPROVERS') or 'U096VAF6H62').split(',') if u.strip()}   # default: Pranay
APPROVER_CMDS = ('sync proceed', 'sync now', 'sync resume', 'sync cancel')
buttons_on = threading.Event()                # set while the Socket Mode connection is up
ASANA_TOKEN = env.get('ASANA_TOKEN')

import requests  # noqa: E402
http = requests.Session()
http.trust_env = False
http.proxies = {'http': None, 'https': None}

# ── state ──────────────────────────────────────────────────────────────────
DEFAULT_STATE = {'paused': False, 'apply_verified': False, 'last_preview': '', 'last_run': '',
                 'pending': None, 'history': [], 'processed': [], 'started_at': ''}
lock = threading.RLock()
pending_rows = None            # rows of the plan waiting for `sync proceed` (memory only: holds full phones)
stop_event = threading.Event()
busy = threading.Event()


VERIFIED_FLAG = HERE / 'APPLY_VERIFIED'   # created once the one-lead live test passed; deleting it locks live changes again


def verified():
    return VERIFIED_FLAG.exists()


def load_state():
    try:
        st = json.loads(STATE_FILE.read_text(encoding='utf-8-sig'))
        return {**DEFAULT_STATE, **st}
    except Exception:
        return dict(DEFAULT_STATE)


def save_state():
    with lock:
        state['processed'] = state['processed'][-300:]
        state['history'] = state['history'][-60:]
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, indent=1), encoding='utf-8')
        os.replace(tmp, STATE_FILE)


state = load_state()


# ── Slack ──────────────────────────────────────────────────────────────────
def slack(method, payload=None, get=False):
    for attempt in range(3):
        try:
            h = {'Authorization': 'Bearer ' + SLACK_TOKEN}
            r = (http.get(f'https://slack.com/api/{method}', headers=h, params=payload or {}, timeout=30) if get
                 else http.post(f'https://slack.com/api/{method}', headers=h, json=payload or {}, timeout=30))
            return r.json()
        except Exception as e:
            log.warning('slack %s failed (%s), retry', method, e)
            time.sleep(3 * (attempt + 1))
    return {'ok': False}


def say_buttons(text, buttons):
    """One message with Slack buttons (when Socket Mode is up). buttons = [(label, action_id, style)].
    Falls back to plain text + typed-command hint when buttons are off."""
    if not buttons_on.is_set():
        return say(text)
    head = text if len(text) <= 2900 else text[:2900] + '\n…(full list in the thread)'
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': head}},
              {'type': 'actions', 'elements': [
                  {'type': 'button', 'text': {'type': 'plain_text', 'text': lab}, 'action_id': aid, 'value': aid,
                   **({'style': st} if st else {})} for lab, aid, st in buttons]}]
    r = slack('chat.postMessage', {'channel': CHANNEL, 'text': text[:3000], 'blocks': blocks})
    return r.get('ts')


def say(text, thread=None):
    """Posts to #cm-uploads (splits long text). Returns the first message ts."""
    first = None
    chunks, cur = [], ''
    for line in text.split('\n'):
        if len(cur) + len(line) > 3500:
            chunks.append(cur); cur = ''
        cur += line + '\n'
    chunks.append(cur)
    for c in chunks:
        if not c.strip():
            continue
        p = {'channel': CHANNEL, 'text': c}
        if thread or first:
            p['thread_ts'] = thread or first
        r = slack('chat.postMessage', p)
        first = first or r.get('ts')
    return first


# ── formatting ─────────────────────────────────────────────────────────────
def per_cm_counts(rows):
    t = defaultdict(Counter)
    for r in rows:
        a = r['action']
        if a == 'add':
            t[r['cm_to']]['new'] += 1
        elif a == 'move':
            t[r['cm_to']]['moved in'] += 1; t[r['cm_from']]['moved out'] += 1
        elif a == 'remove':
            t[r['cm_from']]['removed'] += 1
        elif a == 'update_phone':
            t[r['cm_to']]['phone'] += 1
        elif a == 'no_change':
            t[r['cm_to']]['ok'] += 1
        elif a.startswith('flag'):
            t[r['cm_to'] or r['cm_from'] or '?']['flag'] += 1
    return t


def actionable(rows):
    return [r for r in rows if r['action'] in S.APPLY_ORDER and not r['blocked']]


def summary_text(title, rows, results=None):
    rows = [r for r in rows if not (r['action'] in S.APPLY_ORDER and r['blocked'])] + \
        [dict(r, action='blocked') for r in rows if r['action'] in S.APPLY_ORDER and r['blocked']]   # QA B10
    t = per_cm_counts(rows)
    lines = [f'*{title}*', '']
    for cm in sorted(t, key=str.lower):
        c = t[cm]
        if not any(c[k] for k in ('new', 'moved in', 'moved out', 'removed', 'phone')):
            lines.append(f'{cm} — no changes')
            continue
        bits = [f"{c['new']} new", f"{c['moved in']} moved in", f"{c['moved out']} moved out", f"{c['removed']} removed"]
        if c['phone']:
            bits.append(f"{c['phone']} phone updated")
        lines.append(f'{cm} — ' + ', '.join(bits))
    tot = Counter(r['action'] for r in rows)
    lines += ['──────────────',
              f"Total: {tot['add']} new · {tot['move']} moved · {tot['remove']} removed · {tot['update_phone']} phone updated"]
    if results is not None:
        fails = sum(v for k, v in results.items() if k.endswith('_fail'))
        skipped = sum(v for k, v in results.items() if 'skipped' in k)
        lines[-1] += (f' · *{fails} failed*' if fails else ' · 0 failed') + (f' · {skipped} skipped' if skipped else '')
        if results.get('stopped_before'):
            lines.append(f"🛑 Stopped by `sync stop`: {results['stopped_before']} changes NOT done")
    flags = Counter(r['action'] for r in rows if r['action'].startswith('flag'))
    if flags:
        names = {'flag_dnc': 'on campaign DNC, not added', 'flag_unmapped_cm': 'case manager has no campaign',
                 'flag_ambiguous': 'ambiguous match', 'flag_duplicate_lead': 'duplicate lead, different phone',
                 'flag_shared_phone': 'phone shared by two clients', 'flag_no_phone': 'no phone in Salesforce'}
        lines.append('⚠️ Needs a person: ' + '; '.join(f'{n} {names.get(k, k)}' for k, n in flags.items()) + ' (details in thread)')
    blocked = [r for r in rows if r['action'] == 'blocked']
    if blocked:
        lines.append(f'⚠️ {len(blocked)} changes blocked: ' + '; '.join(sorted({r["blocked"] for r in blocked}))[:300])
    return '\n'.join(lines)


def detail_text(rows):
    lab = {'add': 'NEW', 'move': 'MOVED', 'remove': 'REMOVED', 'update_phone': 'PHONE'}
    out = ['Details:']
    for a in S.APPLY_ORDER:
        for r in rows:
            if r['action'] != a:
                continue
            who = f"{r['name'] or '?'} (file {r['file'] or '-'})"
            if a == 'move':
                out.append(f"{lab[a]}  {who}: {r['cm_from']} → {r['cm_to']}")
            elif a == 'remove':
                out.append(f"{lab[a]}  {who} from {r['cm_from']}: {r['reason']}")
            elif a == 'add':
                out.append(f"{lab[a]}  {who} → {r['cm_to']} ({r['stage']})")
            else:
                out.append(f"{lab[a]}  {who} ({r['cm_to']}) ...{r['phone_masked'][-4:]}")
            if r['blocked']:
                out[-1] += f"  [BLOCKED: {r['blocked']}]"
    for r in rows:
        if r['action'].startswith('flag'):
            out.append(f"CHECK  {r['name'] or '?'} (file {r['file'] or '-'}) {r['cm_to'] or r['cm_from']}: {r['reason']}")
    return '\n'.join(out) if len(out) > 1 else ''


# ── tracking ───────────────────────────────────────────────────────────────
CH_COLS = ['at', 'run', 'action', 'result', 'error', 'name', 'file', 'cm_from', 'cm_to', 'camp_from', 'camp_to',
           'lead_id', 'target_list', 'stage', 'phone_masked', 'reason']


def record(run_id):
    new = not CHANGES_CSV.exists()
    f = open(CHANGES_CSV, 'a', newline='', encoding='utf-8')
    w = csv.DictWriter(f, fieldnames=CH_COLS)
    if new:
        w.writeheader()

    def on_result(r, ok, msg):
        w.writerow({'at': datetime.now().isoformat(timespec='seconds'), 'run': run_id, 'action': r['action'],
                    'result': 'ok' if ok else 'FAILED', 'error': msg, **{k: r.get(k, '') for k in CH_COLS[5:]}})
        f.flush()
    return on_result, f


def asana_report(title, text):
    if not ASANA_TOKEN:
        return None
    try:
        r = http.post(f'https://app.asana.com/api/1.0/tasks/{ASANA_EPIC_CM_GID}/subtasks',
                      headers={'Authorization': 'Bearer ' + ASANA_TOKEN},
                      json={'data': {'name': title, 'notes': text.replace('*', ''), 'assignee': ASANA_USER_GID}}, timeout=30).json()
        gid = (r.get('data') or {}).get('gid')
        return f'https://app.asana.com/0/0/{gid}' if gid else None
    except Exception as e:
        log.error('asana: %s', e)
        return None


# ── the jobs ───────────────────────────────────────────────────────────────
SANITY_DROP = 0.6   # refuse if SF clients or Convoso leads fell below 60% of the last good run (QA B2)


def compute(apply):
    rows, stats, cv = S.compute(apply=apply)
    sf_n, cv_n = stats.get('sf_clients', 0), stats.get('convoso_leads_in_cm_active_lists', 0)
    last = state.get('last_counts') or {}
    if sf_n == 0 or (last.get('sf') and sf_n < SANITY_DROP * last['sf']) or (last.get('cv') and cv_n < SANITY_DROP * last['cv']):
        raise SystemExit(f"sanity check failed: Salesforce gave {sf_n} clients (last run {last.get('sf', '?')}) and Convoso "
                         f"{cv_n} leads (last run {last.get('cv', '?')}). A partial read would cause wrong removals")
    with lock:
        state['last_counts'] = {'sf': sf_n, 'cv': cv_n}
        save_state()
    stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    S.write_plan(rows, LOG_DIR / f'plan_{stamp}.csv')
    return rows, cv


def job_preview(label='Preview'):
    rows, _ = compute(apply=False)
    n = len(actionable(rows))
    title = f"🔎 CM Sync {label} — {datetime.now():%a %m/%d %I:%M %p} (planned, nothing changed yet)"
    ts = say(summary_text(title, rows) + (f'\n_{n} changes planned. ' + (
        f'Over {S.MAX_CHANGES}: the run will wait for `sync proceed`._' if n > S.MAX_CHANGES else 'Runs automatically at 5:00 PM._')))
    d = detail_text(rows)
    if d:
        say(d, thread=ts)


def job_run(trigger, wait_for_buttons=False):
    """Compute the plan. Scheduled (wait_for_buttons): post it with Sync / Reject buttons and auto-sync at 6 PM.
    Manual `sync now`: apply straight away. Either way, a locked service never writes."""
    global pending_rows
    with lock:                                   # a fresh run always replaces an older waiting plan (QA B5)
        pending_rows = None
        state['pending'] = None
        save_state()
    rows, cv = compute(apply=True)
    if stop_event.is_set():                      # `sync stop` typed while the plan was being computed (QA B1)
        say('🛑 Stop received while planning: nothing was changed.')
        _hist(trigger, 'stopped before applying', Counter())
        return
    todo = actionable(rows)
    n = len(todo)
    when = f'{datetime.now():%a %m/%d %I:%M %p}'
    if n == 0:
        ts = say(summary_text(f'✅ CM Sync — {when}: everything already matches Salesforce', rows))
        _hist(trigger, 'no changes', Counter())
        return
    if not verified():
        wait_for_buttons, why = True, 'Live changes are locked (one-lead test), so it will NOT auto-sync. '
    else:
        why = ''
    if wait_for_buttons:
        now = datetime.now()
        auto_at = max(now.replace(hour=AUTO_SYNC_AT[0], minute=AUTO_SYNC_AT[1], second=0, microsecond=0),
                      now + timedelta(seconds=AUTO_MIN_WAIT))
        with lock:
            pending_rows = rows
            state['pending'] = {'at': now.isoformat(timespec='seconds'), 'n': n, 'trigger': trigger,
                                'auto_at': auto_at.isoformat(timespec='seconds') if verified() else ''}
            save_state()
        text = summary_text(f'⏸️ CM Sync — {when}: ready, nothing changed yet', rows) + '\n' + why
        if verified():
            text += f"*Syncs automatically at {auto_at:%I:%M %p}* unless someone rejects it. Details in the thread. "
        if buttons_on.is_set():
            ts = say_buttons(text, [('✅ Sync now', 'cm_sync_proceed', 'primary'), ('❌ Reject', 'cm_sync_cancel', 'danger')])
        else:
            ts = say(text + 'Type `sync proceed` to sync now or `sync cancel` to reject.')
        d = detail_text(rows)
        if d:
            say(d, thread=ts)
        _hist(trigger, f'waiting ({n}), auto at 6 PM', Counter())
        return
    _apply(rows, cv, trigger)


def job_run_scheduled(trigger):
    job_run(trigger, wait_for_buttons=True)       # raises on failure, so last_run is only set after a run that worked
    with lock:
        state['last_run'] = datetime.now().strftime('%Y-%m-%d')
        save_state()


def _apply(rows, cv, trigger):
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
    n = len(actionable(rows))
    say_buttons(f'▶️ CM Sync applying {n} changes (new and moves first, removals last). Stop it at any time'
                + (':' if buttons_on.is_set() else ' with `sync stop`.'), [('🛑 Stop', 'cm_sync_stop', 'danger')])
    global pending_rows
    if not verified():                           # the lock is checked here too (QA criterion 1)
        say('🔒 Live changes are locked until the one-lead test passes. Nothing was changed.')
        return
    if stop_event.is_set():
        say('🛑 Stop received: nothing was changed.')
        return
    with lock:
        pending_rows = None
        state['pending'] = None
        save_state()
    on_result, fh = record(run_id)
    S.ALLOW_WRITES = True
    try:
        results = S.apply_rows(cv, rows, should_stop=stop_event.is_set, on_result=on_result)
    finally:
        S.ALLOW_WRITES = False
        fh.close()
    title = f"✅ CM Sync done — {datetime.now():%a %m/%d %I:%M %p}"
    if results.get('stopped_before'):
        title = f"🛑 CM Sync STOPPED — {datetime.now():%a %m/%d %I:%M %p}"
    text = summary_text(title, rows, results)
    url = asana_report(f"CM Sync — {datetime.now():%m/%d/%Y}", text + '\n\n' + detail_text(rows))
    ts = say(text + (f'\n📝 Asana report: {url}' if url else ''))
    failed_lines = _failed_lines(run_id)
    if failed_lines:
        say('❌ Failed changes (nothing else was affected):\n' + '\n'.join(failed_lines[:60]), thread=ts)
    d = detail_text(rows)
    if d:
        say(d, thread=ts)
    _hist(trigger, 'stopped' if results.get('stopped_before') else 'applied', results)


def _failed_lines(run_id):
    out = []
    try:
        for r in csv.DictReader(open(CHANGES_CSV, encoding='utf-8')):
            if r['run'] == run_id and r['result'] == 'FAILED':
                out.append(f"{r['action']} {r['name']} (file {r['file']}): {r['error']}")
    except Exception:
        pass
    return out


def _hist(trigger, outcome, results):
    with lock:
        state['history'].append({'at': datetime.now().isoformat(timespec='minutes'), 'trigger': trigger, 'outcome': outcome,
                                 'results': dict(results)})
        save_state()


def run_in_background(fn, *args):
    """Jobs run in a thread so `sync stop` and `sync status` still answer while a sync is running."""
    if busy.is_set():
        say('⏳ A sync job is already running. Type `sync status` to check, or `sync stop` to stop it.')
        return False

    def wrap():
        try:
            fn(*args)
        except SystemExit as e:
            log.error('job failed: %s', e)
            say(f'❌ CM Sync failed: {e}. Nothing (more) was changed. A scheduled run retries every 20 min until 7 PM; or type `sync now`.')
        except Exception as e:
            log.error('job crashed: %s\n%s', e, traceback.format_exc())
            say(f'❌ CM Sync failed: {type(e).__name__}: {str(e)[:200]}. Nothing (more) was changed. Type `sync now` to retry.')
        finally:
            busy.clear()
    stop_event.clear()                           # a stop typed during this job (planning or applying) now counts (QA B1)
    busy.set()                                   # set before the thread starts (QA B18)
    threading.Thread(target=wrap, daemon=True).start()
    return True


# ── commands ───────────────────────────────────────────────────────────────
HELP = """*CM Sync commands* (Salesforce → Convoso, weekdays: 4:30 PM preview, 5:00 PM plan with Sync / Reject buttons, auto-sync at 6:00 PM)
`sync status`: last run, next run, paused or not, anything waiting
`sync preview`: show what a sync would change right now (changes nothing)
`sync now`: run the sync now (same rules and limits as 5 PM)
`sync proceed` / `sync cancel`: sync the waiting plan now / reject it (no auto-sync today)
`sync stop`: EMERGENCY: stop a running sync after the current lead, and pause the schedule
`sync hold` / `sync resume`: pause or restart the daily 5 PM run
`sync history`: the last 10 runs
`sync client <file# or phone>`: everything the sync did to that client"""


def next_run_text():
    now = datetime.now()
    d = now
    for _ in range(8):
        cand = d.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
        if cand.weekday() < 5 and cand > now and state.get('last_run') != cand.strftime('%Y-%m-%d'):
            return cand.strftime('%a %m/%d %I:%M %p')
        d = (d + timedelta(days=1)).replace(hour=0, minute=0)
    return '?'


def handle(text, user):
    global pending_rows
    t = re.sub(r'\s+', ' ', text.strip().lower())
    log.info('command from %s: %s', user, t)
    if t in ('sync', 'sync help'):
        say(HELP)
    elif t == 'sync status':
        p = state.get('pending')
        last = state['history'][-1] if state['history'] else None
        say('\n'.join([
            '*CM Sync status*',
            f"Schedule: {'⏸️ PAUSED (`sync resume` to restart)' if state['paused'] else '▶️ on'} · next run: {'-' if state['paused'] else next_run_text()} · weekends skipped",
            f"Running right now: {'yes' if busy.is_set() else 'no'}",
            (f"Waiting: {p['n']} changes since {p['at'][11:16]}" + (f", auto-sync at {p['auto_at'][11:16]}" if p.get('auto_at') else '')) if p else 'Waiting: nothing',
            f"Last run: {last['at'].replace('T', ' ')} ({last['trigger']}): {last['outcome']}" if last else 'Last run: none yet',
            f"Live changes enabled: {'yes' if verified() else 'NO (locked until the one-lead test passes)'}"]))
    elif t == 'sync preview':
        if run_in_background(job_preview, 'Preview (on request)'):
            say('🔎 Building a preview from Salesforce and Convoso (takes a few minutes)...')
    elif t == 'sync now':
        if run_in_background(job_run, f'sync now by <@{user}>'):
            say('▶️ Running the sync now (reading Salesforce and Convoso first, takes a few minutes)...')
    elif t == 'sync proceed':
        p = state.get('pending')
        if not p or pending_rows is None:
            say('Nothing is waiting for approval. Type `sync now` to run a fresh sync.')
        elif (datetime.now() - datetime.fromisoformat(p['at'])).total_seconds() > PENDING_MAX_AGE_H * 3600:
            say(f'The waiting plan is more than {PENDING_MAX_AGE_H} hours old, so Salesforce may have changed. Type `sync now` for a fresh one.')
        elif not verified():
            say('🔒 Live changes are still locked until the one-lead test passes. Ask manager savva 2.')
        else:
            rows = pending_rows
            if run_in_background(lambda: _apply(rows, S.Convoso(), f'proceed by <@{user}>')):
                say(f'👍 Approved by <@{user}>.')   # _apply clears the waiting plan once it starts (QA B6)
    elif t == 'sync cancel':
        with lock:
            had = state.get('pending'); pending_rows = None; state['pending'] = None; save_state()
        say(f'❌ Rejected by <@{user}>: nothing was changed and there is no auto-sync today. Type `sync now` to run one anyway.'
            if had else 'Nothing was waiting.')
    elif t == 'sync stop':
        stop_event.set()
        with lock:
            state['paused'] = True; save_state()
        say(f"🛑 STOP by <@{user}>: {'the running sync stops after the current lead. ' if busy.is_set() else ''}"
            'The daily schedule is PAUSED. Type `sync resume` to restart it.')
    elif t == 'sync hold':
        with lock:
            state['paused'] = True; save_state()
        say(f'⏸️ Daily sync paused by <@{user}>. Type `sync resume` to restart it.')
    elif t == 'sync resume':
        with lock:
            state['paused'] = False; save_state()
        say(f'▶️ Daily sync resumed by <@{user}>. Next run: {next_run_text()}.')
    elif t == 'sync history':
        h = state['history'][-10:]
        say('*Last runs*\n' + ('\n'.join(f"{x['at'].replace('T', ' ')}  {x['trigger']}: {x['outcome']} "
                                          f"{', '.join(f'{k} {v}' for k, v in x['results'].items())}" for x in reversed(h)) or 'none yet'))
    elif t.startswith('sync client '):
        q = re.sub(r'\D', '', t[12:]) or t[12:].strip()
        hits = []
        if CHANGES_CSV.exists():
            for r in csv.DictReader(open(CHANGES_CSV, encoding='utf-8')):
                if q and (q == r['file'] or (len(q) >= 4 and r['phone_masked'][-4:] == q[-4:])):
                    hits.append(f"{r['at'][:16].replace('T', ' ')}  {r['action']} {r['result']}: {r['name']} "
                                f"{r['cm_from'] or ''}{' → ' if r['cm_from'] and r['cm_to'] else ''}{r['cm_to'] or ''} {r['error']}")
        say(f'*Client {q}*\n' + ('\n'.join(hits[-15:]) if hits else 'No sync changes recorded for this client (by file #, or last 4 digits of the phone).'))
    else:
        say('Unknown command. Type `sync help`.')


# ── scheduler ──────────────────────────────────────────────────────────────
def tick():
    if state['paused'] or busy.is_set():
        return
    now = datetime.now()
    if now.weekday() >= 5:            # Saturday / Sunday: skipped
        return
    today = now.strftime('%Y-%m-%d')
    hm = (now.hour, now.minute)
    p = state.get('pending')
    if p and p.get('auto_at') and pending_rows is not None and now >= datetime.fromisoformat(p['auto_at']):
        rows = pending_rows
        say('⏰ Nobody rejected the plan, so CM Sync is syncing it now.')
        run_in_background(lambda: _apply(rows, S.Convoso(), 'auto-sync (no reject by the deadline)'))
        return
    if PREVIEW_AT <= hm < RUN_AT and state.get('last_preview') != today:
        with lock:
            state['last_preview'] = today; save_state()
        run_in_background(job_preview)
    elif RUN_AT <= hm < CATCH_UP_UNTIL and state.get('last_run') != today:
        tries = state.get('run_tries', {})
        if tries.get(today, 0) >= 3:
            return
        if tries.get(today) and (now - datetime.fromisoformat(state.get('last_try_at', now.isoformat()))).total_seconds() < 20 * 60:
            return                                # retry a failed run every 20 min (QA B8)
        with lock:
            state['run_tries'] = {today: tries.get(today, 0) + 1}
            state['last_try_at'] = now.isoformat(timespec='seconds')
            save_state()
        run_in_background(job_run_scheduled, 'daily 5 PM' if hm < (17, 15) else 'daily (late or retry)')


# ── Socket Mode: button clicks ─────────────────────────────────────────────
BUTTON_CMDS = {'cm_sync_proceed': 'sync proceed', 'cm_sync_cancel': 'sync cancel', 'cm_sync_stop': 'sync stop'}


def start_buttons():
    """Connects to Slack over Socket Mode so the Proceed / Cancel / Stop buttons work. Reconnects by itself."""
    if not APP_TOKEN:
        log.info('no CM_SLACK_APP_TOKEN: buttons off, typed `sync` commands only')
        return
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient

    def on_request(client, req):
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # ack within 3 s
        if req.type != 'interactive' or (req.payload or {}).get('type') != 'block_actions':
            return
        pl = req.payload
        user = (pl.get('user') or {}).get('id', '')
        for act in pl.get('actions', []):
            cmd = BUTTON_CMDS.get(act.get('action_id'))
            if not cmd:
                continue
            msg = pl.get('message') or {}
            if cmd != 'sync stop':                         # take the buttons off once someone decided
                done = {'sync proceed': f'✅ Sync started by <@{user}>', 'sync cancel': f'❌ Rejected by <@{user}>: no sync today'}[cmd]
                slack('chat.update', {'channel': CHANNEL, 'ts': msg.get('ts'), 'text': (msg.get('text') or '') + '\n' + done,
                                      'blocks': [b for b in msg.get('blocks', []) if b.get('type') != 'actions'] +
                                                [{'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': done}]}]})
            try:
                handle(cmd, user)
            except Exception as e:
                log.error('button %s failed: %s', cmd, e)
                say(f'❌ Button failed: {str(e)[:200]}')

    def run():
        while True:
            try:
                c = SocketModeClient(app_token=APP_TOKEN, web_client=WebClient(token=SLACK_TOKEN))
                c.socket_mode_request_listeners.append(on_request)
                c.connect()
                buttons_on.set()
                log.info('Socket Mode connected: buttons on')
                while c.is_connected():
                    time.sleep(10)
                buttons_on.clear()
                log.warning('Socket Mode disconnected, reconnecting')
            except Exception as e:
                buttons_on.clear()
                log.error('Socket Mode error: %s', e)
            time.sleep(30)
    threading.Thread(target=run, daemon=True).start()


# ── main loop ──────────────────────────────────────────────────────────────
def get_lock():
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT)
    fh = os.fdopen(fd, 'r+')
    try:
        fh.seek(64)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.error('Another CM sync service is already running. Exiting.')
        sys.exit(3)
    fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
    return fh


def main():
    if not SLACK_TOKEN or not CHANNEL:
        log.error('CM Slack token/channel missing in cm/.env'); sys.exit(1)
    _lock = get_lock()  # noqa: F841  (held for the life of the process)
    start_buttons()
    me = slack('auth.test', get=True)
    bot_user = me.get('user_id')
    log.info('CM sync service started, PID %s, bot user %s', os.getpid(), bot_user)
    processed = set(state['processed'])
    first = not state.get('started_at')
    hist = slack('conversations.history', {'channel': CHANNEL, 'limit': '20'}, get=True)
    for m in hist.get('messages', []):           # never replay old messages on (re)start
        processed.add(m.get('ts'))
    with lock:
        state['processed'] = list(processed); state['started_at'] = datetime.now().isoformat(timespec='seconds'); save_state()
    lost = state.get('pending')
    if lost:                                     # only speak up when a restart actually lost something (no noise otherwise)
        with lock:
            state['pending'] = None
            save_state()
        if lost.get('auto_at') and lost['at'][:10] == datetime.now().strftime('%Y-%m-%d'):
            with lock:                           # rebuild today's plan (fresh data); it gets its own 30-min reject window
                state['last_run'] = ''
                save_state()
            say(f"🔄 CM Sync restarted while a plan was waiting ({lost.get('n')} changes). Building a fresh plan; "
                'it will be posted with the buttons again.')
        else:
            say(f"🔄 CM Sync restarted, so the plan that was waiting ({lost.get('n')} changes) was dropped. "
                'Type `sync now` for a fresh one.')
    if first:
        say('👋 *CM Sync is on.* From now on the case manager campaigns follow Salesforce: every weekday a preview at '
            '4:30 PM and the sync at 5:00 PM (weekends skipped). No more file drops needed. Type `sync help` for commands.')
    while True:
        try:
            r = slack('conversations.history', {'channel': CHANNEL, 'limit': '15'}, get=True)
            for m in reversed(r.get('messages', [])):
                ts = m.get('ts')
                if ts in processed:
                    continue
                processed.add(ts)
                txt = (m.get('text') or '').strip()
                if m.get('user') and m.get('user') != bot_user and not m.get('bot_id') and txt.lower().startswith('sync'):
                    try:
                        handle(txt, m.get('user'))
                    except Exception as e:
                        log.error('command failed: %s\n%s', e, traceback.format_exc())
                        say(f'❌ Command failed: {str(e)[:200]}')
            with lock:
                state['processed'] = list(processed)[-300:]
                save_state()
            tick()
        except Exception as e:
            log.error('loop error: %s', e)
        time.sleep(POLL)


if __name__ == '__main__':
    main()
