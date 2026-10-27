"""
cm_sf_sync.py - CM bot daily sync: Salesforce is the source, Convoso is made to match.

DRY-RUN BY DEFAULT. A normal run only READS (Salesforce SOQL GETs, Convoso search/read
endpoints) and writes a plan file. Nothing is written to Convoso or Salesforce unless
--apply is passed, and --apply is UNTESTED in T-0012 (it was not run).

Usage:
    python -I cm_sf_sync.py                 # dry-run (default): plan_<date>.csv + summary
    python -I cm_sf_sync.py --dry-run       # same, explicit
    python -I cm_sf_sync.py --apply         # UNTESTED. Refuses above MAX_CHANGES without --force

Actions in the plan:
    add          client in SF has no lead in any CM active list  -> insert into the CM's active list
    move         client's lead is in another CM's campaign        -> leads/update list_id (same lead, history kept)
    remove       lead in a CM active list, client not in SF set   -> leads/update list_id to a REMOVE list (decision open)
    update_phone matched lead has a different phone than SF       -> leads/update phone_number
    no_change    matched and correct
    flag_*       needs a human: unmapped CM, no usable phone, no active list, ambiguous match, on DNC
"""

import os, re, sys, csv, json, time, logging, argparse, urllib.request, urllib.error, urllib.parse
import xml.sax.saxutils as xesc
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ── Config (one-line switches) ─────────────────────────────────────────────

# A client is the CM's from Contract Signed until Cancelled, whatever else applies (NSF, EFT). Pranay, 2026-10-27.
CLIENT_STAGES = ('Contract Signed', 'Enrolled', 'Fees Done', 'NSF')
EXCLUDE_NSF = False                # NSF/EFT clients stay with their CM (Pranay, 2026-10-27)
NSF_LISTS = ('12793', '12791')     # NSF LT / GT (see Convoso docs/ARCHITECTURE.md)
MAX_CHANGES = 100                  # --apply refuses above this without --force
UPDATE_PHONES = False              # phone changes are listed, not applied, until reviewed (2026-10-27: a placeholder SF number nearly overwrote a real one)
REMOVE_DEST = {}                   # campaign_id -> inactive list_id. EMPTY ON PURPOSE: open decision (see REPORT.md)
FILE_FIELDS = ('file', 'field_43')  # Convoso lead keys that hold the Leadtrac file number
CONVOSO_PAGE = 100
CALL_PAUSE = 0.15                  # seconds between Convoso calls

# Endpoints that change data. Unreachable unless ALLOW_WRITES is switched on by --apply.
WRITE_ENDPOINTS = {'/v1/leads/insert', '/v1/leads/update', '/v1/leads/delete',
                   '/v1/lists/insert', '/v1/lists/update', '/v1/dnc/insert', '/v1/dnc/delete'}
ALLOW_WRITES = False

SOQL = ("SELECT Id, Leadtrac_File_ID__c, Account.Name, Mobile__c, Phone__c, StageName, "
        "Case_Manager__c, Case_Manager__r.Name FROM Opportunity "
        "WHERE Case_Manager__c != null AND StageName IN (" +
        ",".join("'%s'" % s for s in CLIENT_STAGES) + ")")

log = logging.getLogger('cm_sf_sync')


# ── Secrets: loaded at runtime from the existing files, never printed or copied ──

def _read_env_file(path):
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


SCRATCH = Path(r'C:\Users\PranaySavva\.gemini\antigravity\scratch')
CONVOSO_ENV = SCRATCH / 'Convoso' / '.env'
SF_ENV = SCRATCH / 'vault' / 'salesforce_credentials.env'


def convoso_creds():
    tok = os.environ.get('CONVOSO_API_TOKEN') or _read_env_file(CONVOSO_ENV).get('CONVOSO_API_TOKEN')
    base = os.environ.get('CONVOSO_BASE_URL') or _read_env_file(CONVOSO_ENV).get('CONVOSO_BASE_URL') or 'https://api.convoso.com'
    if not tok:
        raise SystemExit('No Convoso API token available (CONVOSO_API_TOKEN).')
    return tok, base


# ── Phone / text helpers ───────────────────────────────────────────────────

def clean_phone(p):
    """Digits only, leading 1 stripped, 10 digits or ''. Same rule as cm_upload.clean_phone."""
    if p is None:
        return ''
    d = ''.join(c for c in str(p) if c.isdigit())
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    return d if len(d) == 10 else ''


def looks_fake(p):
    """Placeholder numbers in SF (e.g. 949-xxx-1111): 2 or fewer distinct digits after the area code. Never dial or write them."""
    return bool(p) and len(set(p[3:])) <= 2


def mask(p):
    return ('******' + p[-4:]) if p and len(p) >= 4 else ''


def norm_file(f):
    return re.sub(r'\s+', '', str(f or '')).strip().upper()


# ── Salesforce (READ ONLY: SOAP login for a session, then SOQL GETs) ────────

class SF:
    def __init__(self):
        env = _read_env_file(SF_ENV)
        need = ('PROD_USERNAME', 'PROD_PASSWORD', 'PROD_SECURITY_TOKEN', 'PROD_LOGIN_URL')
        if any(k not in env for k in need):
            raise SystemExit('Salesforce prod credentials not available.')
        body = ('<?xml version="1.0" encoding="utf-8"?><env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/" '
                'xmlns:urn="urn:partner.soap.sforce.com"><env:Body><urn:login><urn:username>%s</urn:username>'
                '<urn:password>%s</urn:password></urn:login></env:Body></env:Envelope>' %
                (xesc.escape(env['PROD_USERNAME']), xesc.escape(env['PROD_PASSWORD'] + env['PROD_SECURITY_TOKEN'])))
        req = urllib.request.Request(env['PROD_LOGIN_URL'].rstrip('/') + '/services/Soap/u/60.0', data=body.encode(),
                                     headers={'Content-Type': 'text/xml', 'SOAPAction': 'login'})
        resp = urllib.request.urlopen(req, timeout=30).read().decode()
        self.sid = re.search(r'<sessionId>(.*?)</sessionId>', resp).group(1)
        self.inst = re.search(r'<serverUrl>(https://[^/]+)', resp).group(1)

    def _get(self, path):
        req = urllib.request.Request(self.inst + path, headers={'Authorization': 'Bearer ' + self.sid})
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            raise SystemExit('SF HTTP %s: %s' % (e.code, e.read().decode()[:300]))

    def query(self, soql):
        """SOQL query via GET only. Follows nextRecordsUrl."""
        out, d = [], self._get('/services/data/v60.0/query?q=' + urllib.parse.quote(soql))
        out += d['records']
        while not d['done']:
            d = self._get(d['nextRecordsUrl'])
            out += d['records']
        return out


def pull_sf_clients(sf):
    rows = sf.query(SOQL)
    clients = []
    for r in rows:
        acct = (r.get('Account') or {}).get('Name') or ''
        cmr = (r.get('Case_Manager__r') or {}).get('Name') or ''
        mobile, phone = clean_phone(r.get('Mobile__c')), clean_phone(r.get('Phone__c'))
        mobile = '' if looks_fake(mobile) else mobile          # 2026-10-27: a placeholder Mobile overwrote a real number
        phone = '' if looks_fake(phone) else phone
        clients.append({
            'opp_id': r['Id'], 'file': norm_file(r.get('Leadtrac_File_ID__c')), 'name': acct,
            'phone': mobile or phone, 'phone_alt': phone if mobile and phone and phone != mobile else '',
            'stage': r.get('StageName') or '', 'cm_id': r.get('Case_Manager__c'), 'cm_name': cmr,
            'phones': {x for x in (mobile, phone) if x},
        })
    return clients


def sf_lookup_by_file(sf, files):
    """Read-only: stage + CM for opps by Leadtrac file id (explains WHY a lead is being removed)."""
    out, files = {}, sorted({f for f in files if f})
    for i in range(0, len(files), 150):
        chunk = files[i:i + 150]
        q = ("SELECT Leadtrac_File_ID__c, StageName, Case_Manager__r.Name FROM Opportunity WHERE Leadtrac_File_ID__c IN (" +
             ",".join("'%s'" % f.replace("'", '') for f in chunk) + ")")
        for r in sf.query(q):
            out[norm_file(r.get('Leadtrac_File_ID__c'))] = (r.get('StageName') or '', (r.get('Case_Manager__r') or {}).get('Name') or '')
    return out


# ── Convoso (READ endpoints unless ALLOW_WRITES) ───────────────────────────

class Convoso:
    def __init__(self):
        self.token, self.base = convoso_creds()
        import requests
        self.s = requests.Session()
        self.s.trust_env = False                 # Windows system proxy breaks Convoso calls (fleet gotcha)
        self.s.proxies = {'http': None, 'https': None}
        self.calls = Counter()

    def post(self, endpoint, data):
        if endpoint in WRITE_ENDPOINTS and not ALLOW_WRITES:
            raise RuntimeError('WRITE BLOCKED (dry-run): ' + endpoint)
        d = dict(data)
        d['auth_token'] = self.token
        self.calls[endpoint] += 1
        time.sleep(CALL_PAUSE)
        tries = 1 if endpoint in WRITE_ENDPOINTS else 3       # a write is never retried (could double-insert)
        for attempt in range(tries):
            try:
                return self.s.post(self.base + endpoint, data=d, timeout=40).json()
            except Exception as e:                       # network / bad json: retry reads, then report
                if attempt == tries - 1:
                    return {'success': False, 'error': str(e)}
                time.sleep(2 * (attempt + 1))

    def read(self, endpoint, data):
        """A read that must succeed: a failed read would make real leads look absent (-> wrong adds/removes)."""
        r = self.post(endpoint, data)
        if not isinstance(r, dict) or r.get('success') is False or r.get('error'):
            raise RuntimeError('Convoso read failed (%s): %s' % (endpoint, str(r.get('error') or r.get('text') or r)[:150]))
        return r

    def lists(self, campaign_id):
        r = self.read('/v1/lists/search', {'campaign_id': str(campaign_id)})
        data = r.get('data', [])
        if isinstance(data, dict):
            data = data.get('entries', [])
        return data or []

    def leads(self, list_id):
        out, offset = [], 0
        while True:
            r = self.read('/v1/leads/search', {'list_id': str(list_id), 'limit': str(CONVOSO_PAGE), 'offset': str(offset)})
            data = r.get('data', {})
            entries = data.get('entries', []) if isinstance(data, dict) else (data or [])
            if not entries:
                break
            out += entries
            offset += len(entries)
            if len(entries) < CONVOSO_PAGE:
                break
        return out

    def leads_by_phone(self, phone):
        r = self.post('/v1/leads/search', {'phone_number': phone, 'limit': '50'})
        data = r.get('data', {})
        return data.get('entries', []) if isinstance(data, dict) else (data or [])

    def dnc_campaigns(self, phone):
        r = self.read('/v1/dnc/search', {'phone_number': phone, 'phone_code': '1'})
        data = r.get('data', [])
        if isinstance(data, dict):
            data = data.get('entries', [])
        return {str(e.get('campaign_id', '')) for e in (data or [])}


def lead_file(lead):
    """Custom field 'file' is set by cm_upload as the Leadtrac file number. Field location varies by API shape."""
    # Observed 2026-10-27: the API returns custom fields as field_<n> (field_43 = file, field_19 = client_name,
    # field_20 = case_manager). cm_upload also stores the file number in last_name, so that is the fallback.
    for k in FILE_FIELDS:
        v = lead.get(k)
        if v not in (None, ''):
            return norm_file(v)
    v = lead.get('last_name')
    return norm_file(v) if v and re.fullmatch(r'[0-9A-Za-z\-]{4,}', str(v).strip()) else ''


def pick_active_list(lists):
    """The campaign's current active list = status Y, newest id. Returns (list_or_None, n_active)."""
    act = [l for l in lists if str(l.get('status', '')).upper() in ('Y', '1', 'ACTIVE')]
    if not act:
        return None, 0
    act.sort(key=lambda l: int(str(l.get('id', 0)) or 0), reverse=True)
    return act[0], len(act)


def read_convoso_state(cv, cmmap):
    """campaign_id -> {'active_list': {...}|None, 'n_active': int, 'leads': [lead...]}"""
    state = {}
    for cid in sorted({c['campaign_id'] for c in cmmap['cms'].values()}):
        lists = cv.lists(cid)
        active, n_active = pick_active_list(lists)
        leads = []
        # every ACTIVE list is dialed, so every active list counts as "current state"
        for l in lists:
            if str(l.get('status', '')).upper() in ('Y', '1', 'ACTIVE'):
                for ld in cv.leads(l['id']):
                    ld['_list_id'] = str(ld.get('list_id') or l['id'])
                    leads.append(ld)
        state[cid] = {'active_list': active, 'n_active': n_active, 'leads': leads, 'n_lists': len(lists), 'lists': lists}
        log.info('campaign %s: %d lists, %d active, %d leads in active lists', cid, len(lists), n_active, len(leads))
    return state


def read_nsf_phones(cv):
    ph = set()
    for lid in NSF_LISTS:
        for ld in cv.leads(lid):
            p = clean_phone(ld.get('phone_number'))
            if p:
                ph.add(p)
    return ph


# ── The plan (pure function: no IO, unit-tested in test_plan.py) ───────────

def build_plan(clients, state, cmmap, nsf_phones=frozenset(), exclude_nsf=EXCLUDE_NSF):
    """
    clients: SF client dicts. state: campaign_id -> {'active_list','leads'}. Returns (rows, stats).
    Each row: dict(action, reason, cm_from, cm_to, camp_from, camp_to, lead_id, file, phone_masked, stage, name,
                   target_list, blocked)
    """
    cm_by_id = cmmap['cms']
    camp_name = defaultdict(lambda: '?')
    for c in cm_by_id.values():
        camp_name[c['campaign_id']] = c['name'] if not c.get('retired') else c['name'] + ' (retired)'

    rows, stats = [], Counter()

    def row(action, reason='', **kw):
        base = dict(action=action, reason=reason, cm_from='', cm_to='', camp_from='', camp_to='', lead_id='',
                    file='', phone_masked='', stage='', name='', target_list='', blocked='')
        base.update(kw)
        rows.append(base)
        stats[action] += 1

    # ---- index SF clients (after exclusions) ----
    sf_by_file, sf_by_phone, sf_set = {}, defaultdict(list), []
    dup_files = Counter(c['file'] for c in clients if c['file'])
    for c in clients:
        if exclude_nsf and c['phone'] and c['phone'] in nsf_phones:
            stats['excluded_nsf'] += 1
            c['_nsf'] = True
            continue
        sf_set.append(c)
        if c['file']:
            sf_by_file.setdefault(c['file'], c)
        if c['phone']:
            sf_by_phone[c['phone']].append(c)

    # ---- index Convoso leads in CM active lists ----
    conv_by_file, conv_by_phone = defaultdict(list), defaultdict(list)
    all_leads = []
    for cid, st in state.items():
        for ld in st['leads']:
            ld['_campaign'] = cid
            ld['_phone'] = clean_phone(ld.get('phone_number'))
            ld['_file'] = lead_file(ld)
            all_leads.append(ld)
            if ld['_file']:
                conv_by_file[ld['_file']].append(ld)
            if ld['_phone']:
                conv_by_phone[ld['_phone']].append(ld)

    matched_by_file = matched_by_phone = 0
    used_lead_ids, handled_clients = set(), set()

    # ---- walk SF clients: file-id matches claim their lead first, phone fallbacks only get what is left ----
    sf_set.sort(key=lambda c: 0 if (c['file'] and c['file'] in conv_by_file) else 1)
    for c in sf_set:
        if c['cm_id'] in cmmap.get('ignore', {}):
            continue  # test accounts (e.g. Adarsha): not a real CM, skipped silently
        cm = cm_by_id.get(c['cm_id'])
        base = dict(file=c['file'], phone_masked=mask(c['phone']), stage=c['stage'], name=c['name'])
        if cm is None:
            row('flag_unmapped_cm', 'SF case manager %s (%s) has no campaign in cm_map.json' % (c['cm_name'], c['cm_id']),
                cm_to=c['cm_name'], **base)
            continue
        if cm.get('retired'):
            row('flag_unmapped_cm', 'SF case manager %s is retired in cm_map.json' % c['cm_name'], cm_to=c['cm_name'], **base)
            continue
        to_camp = cm['campaign_id']
        to_list = ((state.get(to_camp) or {}).get('active_list') or {}).get('id')

        if dup_files[c['file']] > 1 and c['file']:
            stats['dup_file_in_sf'] += 1

        # match: file id first, phone fallback
        lead, how = None, ''
        cands = conv_by_file.get(c['file'], []) if c['file'] else []
        if len(cands) == 1:
            lead, how = cands[0], 'file'
        elif len(cands) > 1:
            # Several Convoso leads carry this file id (duplicate rows in an old file, co-applicant phones...).
            # Keep the one whose phone equals SF's; the others are flagged for a human, NEVER auto-removed.
            # keeper = the lead with the SF phone, preferring the one already in the right campaign, then the oldest.
            same_phone = sorted([l for l in cands if l['_phone'] == c['phone']],
                                key=lambda l: (0 if l['_campaign'] == to_camp else 1, int(str(l.get('id') or 0) or 0)))
            if same_phone:
                lead, how = same_phone[0], 'file+phone'
                for extra in cands:
                    if extra is lead:
                        continue
                    used_lead_ids.add(id(extra))
                    xk = dict(lead_id=str(extra.get('id') or ''), camp_from=extra['_campaign'],
                              cm_from=camp_name[extra['_campaign']], cm_to=cm['name'], camp_to=to_camp,
                              file=c['file'], phone_masked=mask(extra['_phone']), stage=c['stage'], name=c['name'])
                    if extra['_phone'] == c['phone']:
                        # same client AND same phone twice: a true duplicate, dialed from two campaigns. Remove the extra.
                        row('remove', 'duplicate: same file id and phone as lead %s (kept)' % (lead.get('id') or ''),
                            target_list=str(REMOVE_DEST.get(extra['_campaign'], '')),
                            blocked='' if REMOVE_DEST.get(extra['_campaign']) else 'remove destination list not decided', **xk)
                    else:
                        row('flag_duplicate_lead', 'file id %s has %d Convoso leads; this one has a different phone, left untouched'
                            % (c['file'], len(cands)), **xk)
            else:
                for extra in cands:
                    used_lead_ids.add(id(extra))
                row('flag_ambiguous', 'file id %s matches %d Convoso leads and none has the SF phone' % (c['file'], len(cands)),
                    cm_to=cm['name'], camp_to=to_camp, **base)
                continue
        if lead is None and c['phone']:
            pc = [l for l in conv_by_phone.get(c['phone'], []) if id(l) not in used_lead_ids]
            if len(pc) == 1:
                lead, how = pc[0], 'phone'
            elif len(pc) > 1:
                # several leads share the phone: prefer the one already in the right campaign
                right = [l for l in pc if l['_campaign'] == to_camp]
                if len(right) == 1:
                    lead, how = right[0], 'phone(campaign)'
                else:
                    row('flag_ambiguous', 'phone matches %d Convoso leads' % len(pc), cm_to=cm['name'], camp_to=to_camp, **base)
                    continue

        if lead is None:
            if not c['phone']:
                row('flag_no_phone', 'SF client has no usable Mobile/Phone and no file match in Convoso',
                    cm_to=cm['name'], camp_to=to_camp, **base)
                continue
            if conv_by_phone.get(c['phone']):
                # this phone already belongs to a lead in a CM active list that another SF client claimed (household /
                # two files, one phone). Adding would make Convoso dial the same number twice, so a human decides.
                row('flag_shared_phone', 'phone already on a lead (file %s) in a CM active list claimed by another client'
                    % (conv_by_phone[c['phone']][0]['_file'] or '?'), cm_to=cm['name'], camp_to=to_camp, **base)
                continue
            blocked = '' if to_list else 'no active list in campaign %s' % to_camp
            row('add', 'new client, no lead in any CM active list', cm_to=cm['name'], camp_to=to_camp,
                target_list=str(to_list or ''), blocked=blocked, **base)
            continue

        used_lead_ids.add(id(lead))
        if how.startswith('file'):
            matched_by_file += 1
        else:
            matched_by_phone += 1
        lid = str(lead.get('id') or lead.get('lead_id') or '')
        cur_camp = lead['_campaign']
        cur_cm = camp_name[cur_camp]
        common = dict(lead_id=lid, cm_from=cur_cm, cm_to=cm['name'], camp_from=cur_camp, camp_to=to_camp, **base)

        if cur_camp != to_camp:
            blocked = '' if to_list else 'no active list in campaign %s' % to_camp
            row('move', 'case manager changed (%s)' % how, target_list=str(to_list or ''), blocked=blocked, **common)
        elif c['phone'] and lead['_phone'] and lead['_phone'] not in c.get('phones', {c['phone']}):
            # only when Convoso's number is NONE of the client's SF numbers (Mobile or Phone)
            row('update_phone', 'SF phone differs from Convoso lead (%s)' % how, **common)
        else:
            row('no_change', 'matched by %s' % how, **common)

    # ---- walk Convoso leads that nothing in SF claimed -> remove ----
    for ld in all_leads:
        if id(ld) in used_lead_ids:
            continue
        cid = ld['_campaign']
        nsf = bool(exclude_nsf and ld['_phone'] and ld['_phone'] in nsf_phones)
        row('remove', 'NSF client (phone on an NSF list): the NSF bot dials them' if nsf
            else 'lead in CM active list, client not in SF client set',
            lead_id=str(ld.get('id') or ld.get('lead_id') or ''), cm_from=camp_name[cid], camp_from=cid,
            file=ld['_file'], phone_masked=mask(ld['_phone']),
            name=(str(ld.get('first_name', '')) + ' ' + str(ld.get('last_name', ''))).strip(),
            target_list=str(REMOVE_DEST.get(cid, '')),
            blocked='' if REMOVE_DEST.get(cid) else 'remove destination list not decided')

    stats['sf_clients'] = len(clients)
    stats['sf_clients_in_set'] = len(sf_set)
    stats['convoso_leads_in_cm_active_lists'] = len(all_leads)
    stats['matched_by_file'] = matched_by_file
    stats['matched_by_phone'] = matched_by_phone
    matched = matched_by_file + matched_by_phone
    stats['match_rate_pct'] = round(100.0 * matched / max(1, len(sf_set)), 1)
    return rows, stats


# ── Output ─────────────────────────────────────────────────────────────────

PLAN_COLS = ['action', 'reason', 'cm_from', 'cm_to', 'camp_from', 'camp_to', 'lead_id', 'file', 'phone_masked',
             'stage', 'name', 'target_list', 'blocked']


def write_plan(rows, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in PLAN_COLS})


def per_cm_summary(rows):
    t = defaultdict(Counter)
    for r in rows:
        if r['action'] in ('add', 'update_phone', 'no_change'):
            t[r['cm_to']][r['action']] += 1
        elif r['action'] == 'move':
            t[r['cm_to']]['move_in'] += 1
            t[r['cm_from']]['move_out'] += 1
        elif r['action'] == 'remove':
            t[r['cm_from']]['remove'] += 1
        elif r['action'].startswith('flag'):
            t[r['cm_to'] or r['cm_from'] or '?'][r['action']] += 1
    cols = ['add', 'move_in', 'move_out', 'remove', 'update_phone', 'no_change']
    lines = ['| CM | ' + ' | '.join(cols) + ' | flags |', '|---|' + '---|' * (len(cols) + 1)]
    for cm in sorted(t):
        flags = sum(v for k, v in t[cm].items() if k.startswith('flag'))
        lines.append('| %s | ' % cm + ' | '.join(str(t[cm][c]) for c in cols) + ' | %d |' % flags)
    return '\n'.join(lines)


# ── --apply (UNTESTED in T-0012; never run) ────────────────────────────────

def apply_plan(cv, rows, force=False):
    """UNTESTED. Executes the plan. Writes happen only here, and only after ALLOW_WRITES is switched on in main()."""
    actionable = [r for r in rows if r['action'] in ('add', 'move', 'remove', 'update_phone') and not r['blocked']]
    n = len(actionable)
    if n > MAX_CHANGES and not force:
        raise SystemExit('REFUSING: %d changes > MAX_CHANGES=%d. Re-run with --force after reviewing the plan.' % (n, MAX_CHANGES))
    done = Counter()
    for r in actionable:
        a = r['action']
        try:
            if a == 'add':
                phone = r.get('_phone_full')  # full phone is only held in memory for --apply (see run())
                if not phone:
                    done['skipped_no_phone'] += 1
                    continue
                if r['camp_to'] in cv.dnc_campaigns(phone):          # DNC'd leads are not re-added
                    done['skipped_dnc'] += 1
                    continue
                res = cv.post('/v1/leads/insert', {'list_id': r['target_list'], 'duplicate_check': 'NO', 'phone_code': '1',
                                                   'phone_number': phone, 'first_name': r['name'], 'last_name': r['file'],
                                                   'client_name': r['name'], 'case_manager': r['cm_to'], 'file': r['file']})
            elif a == 'move':
                # same lead, new list: call history survives (no delete + re-insert)
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'list_id': r['target_list'], 'case_manager': r['cm_to']})
            elif a == 'remove':
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'list_id': r['target_list']})
            elif a == 'update_phone':
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'phone_number': r.get('_phone_full', '')})
            done[a + ('_ok' if res.get('success') else '_fail')] += 1
        except Exception as e:                                       # never abort half way silently
            log.error('apply %s lead %s failed: %s', a, r.get('lead_id'), e)
            done[a + '_error'] += 1
    return done


APPLY_ORDER = ('add', 'move', 'update_phone', 'remove')   # removes last


def _scrub(text):
    """Error text can echo the submitted number: drop any run of 7+ digits (QA B13)."""
    return re.sub(r'\d{7,}', '[number]', text)[:200]


def apply_rows(cv, rows, should_stop=lambda: False, on_result=lambda r, ok, msg: None):
    """Executes the actionable rows in APPLY_ORDER, one lead at a time. Caller switches ALLOW_WRITES on.
    should_stop() is checked before every lead (the Slack `sync stop` command). Returns a Counter."""
    done = Counter()
    todo = [r for a in APPLY_ORDER for r in rows if r['action'] == a and not r['blocked']]
    for r in todo:
        if should_stop():
            done['stopped_before'] = len(todo) - sum(v for k, v in done.items() if k != 'stopped_before')
            break
        a = r['action']
        try:
            if a == 'add':
                phone = r.get('_phone_full')
                if not phone:
                    done['add_skipped'] += 1; on_result(r, False, 'skipped: no phone'); continue
                if r['camp_to'] in cv.dnc_campaigns(phone):
                    done['add_skipped_dnc'] += 1; on_result(r, False, 'skipped: on campaign DNC'); continue
                res = cv.post('/v1/leads/insert', {'list_id': r['target_list'], 'duplicate_check': 'NO', 'phone_code': '1',
                                                   'phone_number': phone, 'first_name': r['name'], 'last_name': r['file'],
                                                   'client_name': r['name'], 'case_manager': r['cm_to'], 'file': r['file']})
            elif a == 'move':
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'list_id': r['target_list'], 'case_manager': r['cm_to']})
            elif a == 'remove':
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'list_id': r['target_list']})
            else:
                if not r.get('_phone_full'):
                    done['phone_skipped'] += 1; on_result(r, False, 'skipped: no SF phone'); continue
                res = cv.post('/v1/leads/update', {'lead_id': r['lead_id'], 'phone_number': r['_phone_full']})
            ok = bool(res.get('success'))
            done[a + ('_ok' if ok else '_fail')] += 1
            on_result(r, ok, '' if ok else _scrub(str(res.get('text') or res.get('error') or 'no success flag')))
        except Exception as e:                                    # one bad lead never stops the rest
            done[a + '_fail'] += 1
            on_result(r, False, _scrub(str(e)))
    return done


# ── Main ───────────────────────────────────────────────────────────────────

def newest_inactive_list(lists):
    """Where removed leads go: the campaign's newest inactive list that is OLDER than its active list (decided 2026-10-27).
    Nothing new is created. A newer inactive list may be one the CM upload bot just made and is about to activate (QA B4)."""
    act = [int(str(l.get('id', 0)) or 0) for l in lists if str(l.get('status', '')).upper() in ('Y', '1', 'ACTIVE')]
    ceiling = max(act) if act else float('inf')
    ina = [l for l in lists if str(l.get('status', '')).upper() not in ('Y', '1', 'ACTIVE')
           and int(str(l.get('id', 0)) or 0) < ceiling]
    ina.sort(key=lambda l: int(str(l.get('id', 0)) or 0), reverse=True)
    return str(ina[0]['id']) if ina else ''


def compute(apply=False):
    """Reads SF + Convoso and builds the plan. Returns (rows, stats, cv). With apply=True, rows that need a
    full phone carry it in '_phone_full' (memory only, never written to disk)."""
    cmmap = json.loads((HERE / 'cm_map.json').read_text(encoding='utf-8'))

    sf = SF()
    clients = pull_sf_clients(sf)
    log.info('SF clients (stages %s, CM set): %d', ', '.join(CLIENT_STAGES), len(clients))

    cv = Convoso()
    state = read_convoso_state(cv, cmmap)
    nsf = read_nsf_phones(cv) if EXCLUDE_NSF else set()
    log.info('NSF phones excluded: %d', len(nsf))
    REMOVE_DEST.clear()
    for cid, st in state.items():
        dest = newest_inactive_list(st.get('lists', []))
        if dest:
            REMOVE_DEST[cid] = dest

    rows, stats = build_plan(clients, state, cmmap, nsf)

    # explain removes: why is this lead leaving? (SF read-only lookup by file id)
    rem_files = [r['file'] for r in rows if r['action'] == 'remove' and r['file']]
    why = sf_lookup_by_file(sf, rem_files) if rem_files else {}
    for r in rows:
        if r['action'] == 'remove':
            if r['reason'].startswith(('NSF', 'duplicate')):
                if r['file'] in why:
                    r['stage'] = why[r['file']][0]
                continue
            if r['file'] in why:
                st, cmn = why[r['file']]
                r['reason'] = 'SF stage now "%s" (CM %s)' % (st, cmn or '-')
                r['stage'] = st
            elif r['file']:
                r['reason'] = 'file id not found in SF'
            else:
                r['reason'] = 'Convoso lead has no file id and no phone match in SF'

    # adds: is the phone already a lead somewhere else, or on campaign DNC? (reads only)
    for r in rows:
        if r['action'] == 'add':
            c = next((x for x in clients if x['file'] == r['file'] and x['file']), None) if r['file'] else                 next((x for x in clients if not x['file'] and mask(x['phone']) == r['phone_masked'] and x['cm_name'] == r['cm_to']), None)
            if c and c['phone']:
                if r['camp_to'] in cv.dnc_campaigns(c['phone']):
                    r['action'] = 'flag_dnc'
                    r['reason'] = 'on campaign %s DNC: not added' % r['camp_to']
                    stats['add'] -= 1
                    stats['flag_dnc'] += 1
                    continue
                elsewhere = cv.leads_by_phone(c['phone'])
                if elsewhere:
                    camps = sorted({str(e.get('campaign_id') or e.get('campaign_name') or '?') for e in elsewhere})
                    r['reason'] += '; phone already a lead elsewhere: ' + ','.join(camps)
                if apply:
                    r['_phone_full'] = c['phone']

    if apply:   # full phones are kept in memory only (never written to the plan CSV, which holds masked phones)
        by_file = {}
        for c in clients:
            if c['file']:
                by_file.setdefault(c['file'], c)       # first wins, same rule as build_plan
        for r in rows:
            if r['action'] == 'update_phone' and r['file'] in by_file:
                r['_phone_full'] = by_file[r['file']]['phone']

    if not UPDATE_PHONES:
        for r in rows:
            if r['action'] == 'update_phone':
                r['blocked'] = 'phone updates are report-only until reviewed'
    return rows, stats, cv


def run(apply=False, force=False):
    global ALLOW_WRITES
    today = datetime.now().strftime('%Y-%m-%d')
    logfile = HERE / 'logs' / ('run_%s.log' % today)
    logfile.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(logfile, encoding='utf-8'), logging.StreamHandler(sys.stdout)])
    log.info('mode=%s  (writes %s)', 'APPLY' if apply else 'DRY-RUN', 'ENABLED' if apply else 'blocked')
    rows, stats, cv = compute(apply=apply)
    plan_path = HERE / 'logs' / ('plan_%s.csv' % today)
    write_plan(rows, plan_path)

    summary = ['# CM sync plan %s (%s)' % (today, 'APPLY' if apply else 'DRY-RUN'), '',
               'SF clients in query: %d | in client set (after NSF exclusion): %d | NSF-excluded: %d'
               % (stats['sf_clients'], stats['sf_clients_in_set'], stats['excluded_nsf']),
               'Convoso leads in CM active lists: %d' % stats['convoso_leads_in_cm_active_lists'],
               'Matched by file id: %d | by phone fallback: %d | match rate: %s%%' %
               (stats['matched_by_file'], stats['matched_by_phone'], stats['match_rate_pct']), '',
               'Actions: ' + ', '.join('%s=%d' % (k, v) for k, v in sorted(stats.items())
                                       if k in ('add', 'move', 'remove', 'update_phone', 'no_change') or k.startswith('flag')), '',
               per_cm_summary(rows), '',
               'Convoso calls made: ' + ', '.join('%s=%d' % kv for kv in sorted(cv.calls.items()))]
    (HERE / ('summary_%s.md' % today)).write_text('\n'.join(summary), encoding='utf-8')
    log.info('\n%s', '\n'.join(summary))
    log.info('plan written: %s', plan_path)

    if apply:
        raise SystemExit('The CLI --apply path is closed (QA B12). Live changes run only through sync_service.py.')
        ALLOW_WRITES = True
        done = apply_plan(cv, rows, force=force)
        log.info('apply result: %s', dict(done))
    else:
        log.info('DRY-RUN complete: zero writes. Write calls attempted: %d', sum(v for k, v in cv.calls.items() if k in WRITE_ENDPOINTS))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='default: read only, writes plan file')
    g.add_argument('--apply', action='store_true', help='UNTESTED: execute the plan against Convoso')
    ap.add_argument('--force', action='store_true', help='allow --apply above MAX_CHANGES')
    a = ap.parse_args()
    run(apply=a.apply, force=a.force)
