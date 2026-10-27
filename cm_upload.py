"""
CM Weekly Upload — split file by case manager, create lists, upload leads, activate.

Usage (called from slack_watcher.py, not directly):
    import cm_upload
    summary = cm_upload.parse_file(filepath)
    result  = cm_upload.upload_all(summary)
    report  = cm_upload.activate(result)
"""

import os, re, csv, json, time, logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import requests
import openpyxl
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────────────────────────

CM_DIR = Path(__file__).resolve().parent
load_dotenv(CM_DIR / '.env')
load_dotenv(CM_DIR.parent / '.env')

CONVOSO_TOKEN = os.getenv('CONVOSO_API_TOKEN')
CONVOSO_BASE  = os.getenv('CONVOSO_BASE_URL', 'https://api.convoso.com')
ASANA_TOKEN   = os.getenv('ASANA_TOKEN')
ASANA_PROJECT_GID = os.getenv('ASANA_PROJECT_GID', '1211459088696662')
ASANA_EPIC_CM_GID = '1213813165374346'
ASANA_USER_GID    = '1210889312062695'  # Pranay

# Case Manager → Campaign ID mapping (update when CMs change)
CM_CAMPAIGNS = {
    'Alondra Alva':       '4135',
    'Alquinton Smith':    '4999',
    'Kalyssa Acosta':     '4997',
    'Darrell Fair':       '4985',
    'Maximiliano Muller': '4991',
    'Eli Paredes':        '5003',
    'Patricia Tercier':   '5007',
    'Sammaurae Barr':     '4993',
    'Wanicka cassagnol':  '4995',
    'Stefan Rivera':      '6181',
    'Joshua Cardenas':    '6269',
    'Ebonee Wright':      '6871',
}


def _norm_cm(name):
    """Normalize a CM name for matching: lowercase + collapse internal whitespace + strip.
    Makes the campaign lookup forgiving of casing/spacing drift in Oscar's file (e.g.
    'Stefan Rivera' vs 'stefan rivera' vs 'Stefan  Rivera' all match). Routing only —
    the original spelling is still what gets stored/displayed on the lead in Convoso."""
    return re.sub(r'\s+', ' ', str(name)).strip().lower()


# Normalized index of CM_CAMPAIGNS, built once. Keys today are all distinct after
# normalization, so no collisions; if two ever collide, the later one wins (and that
# would be a data problem to fix in CM_CAMPAIGNS, not here).
_CM_CAMPAIGNS_NORM = {_norm_cm(k): v for k, v in CM_CAMPAIGNS.items()}


def campaign_for_cm(cm_name):
    """Case/space-insensitive lookup of a CM's campaign id. Returns the id or None."""
    return _CM_CAMPAIGNS_NORM.get(_norm_cm(cm_name))


log = logging.getLogger('cm_upload')

# ── Session with proxy bypass ───────────────────────────────────────────────

session = requests.Session()
session.trust_env = False
session.proxies = {'http': None, 'https': None}


# ── Helpers ─────────────────────────────────────────────────────────────────

def clean_phone(phone):
    """Strip formatting, remove leading 1. Returns 10-digit string or ''."""
    if phone is None:
        return ''
    phone = str(phone)
    # Strip Slack tel: auto-link format: <tel:8433608471|(843) 360-8471>
    if '<tel:' in phone:
        phone = re.sub(r'<tel:([^|>]+)[^>]*>', r'\1', phone)
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits if len(digits) == 10 else ''


def convoso_post(endpoint, data):
    """POST to Convoso API with auth token."""
    data['auth_token'] = CONVOSO_TOKEN
    resp = session.post(f'{CONVOSO_BASE}{endpoint}', data=data, timeout=30)
    return resp.json()


def list_name_for_cm(cm_name, date_str):
    """Generate list name like 'Alondra list 04-09-26'."""
    first_name = cm_name.split()[0]
    return f'{first_name} list {date_str}'


# ── Step 1: Parse file ─────────────────────────────────────────────────────

def parse_file(filepath):
    """
    Parse xlsx/csv, split by Case Manager.
    Returns dict: {
        'filename': str,
        'date_str': 'MM-DD-YY',
        'cms': {
            'Alondra Alva': {
                'leads': [{'client_name':..., 'file_num':..., 'phone':..., 'cm':...}, ...],
                'count': int,
                'campaign_id': str or None,
            },
            ...
        },
        'total_leads': int,
        'matched_cms': int,
        'unmatched_cms': ['name', ...],
    }
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()

    # Read rows
    rows = []
    if ext == '.xlsx':
        wb = openpyxl.load_workbook(str(filepath), read_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if all_rows:
            rows = all_rows[1:]  # skip header
    elif ext == '.csv':
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            rows = list(reader)
    else:
        raise ValueError(f'Unsupported file type: {ext}')

    # Group by Case Manager
    cm_data = defaultdict(list)
    for row in rows:
        if not row or len(row) < 4:
            continue
        file_num, client_name, case_manager, cell_phone = row[0], row[1], row[2], row[3]
        if not case_manager:
            continue
        cm_name = str(case_manager).strip()
        cm_data[cm_name].append({
            'client_name': str(client_name or '').strip(),
            'file_num': str(file_num or '').strip(),
            'phone': clean_phone(cell_phone),
            'cm': cm_name,
        })

    # Build summary
    date_str = datetime.now().strftime('%m-%d-%y')
    cms = {}
    unmatched = []
    for cm_name, leads in sorted(cm_data.items()):
        campaign_id = campaign_for_cm(cm_name)  # case/space-insensitive match
        if not campaign_id:
            unmatched.append(cm_name)
        cms[cm_name] = {
            'leads': leads,
            'count': len(leads),
            'campaign_id': campaign_id,
        }

    return {
        'filename': filepath.name,
        'date_str': date_str,
        'cms': cms,
        'total_leads': sum(c['count'] for c in cms.values()),
        'matched_cms': sum(1 for c in cms.values() if c['campaign_id']),
        'unmatched_cms': unmatched,
    }


# ── Step 2: Upload all ─────────────────────────────────────────────────────

def upload_all(summary):
    """
    For each CM with a campaign:
      1. Create inactive list
      2. Upload all leads
    Returns updated summary with list_ids and upload counts.
    """
    date_str = summary['date_str']
    results = []

    for cm_name, cm_info in summary['cms'].items():
        campaign_id = cm_info.get('campaign_id')
        if not campaign_id:
            results.append({
                'cm': cm_name,
                'status': 'skipped',
                'reason': 'no campaign',
            })
            continue

        # Create inactive list
        lname = list_name_for_cm(cm_name, date_str)
        log.info(f'Creating list: {lname} in campaign {campaign_id}')
        resp = convoso_post('/v1/lists/insert', {
            'name': lname,
            'campaign_id': campaign_id,
            'status': '0',  # inactive
        })

        if not resp.get('success'):
            results.append({
                'cm': cm_name,
                'status': 'error',
                'reason': f"List creation failed: {resp.get('text', resp)}",
            })
            continue

        list_id = str(resp.get('data', {}).get('list_id') or resp.get('id', ''))
        log.info(f'Created list {list_id} for {cm_name}')

        # Check DNC status (read-only, no clearing)
        on_dnc = 0
        for lead in cm_info['leads']:
            if not lead['phone']:
                continue
            dnc_resp = convoso_post('/v1/dnc/search', {
                'phone_number': lead['phone'],
                'phone_code': '1',
            })
            dnc_entries = dnc_resp.get('data', [])
            if isinstance(dnc_entries, dict):
                dnc_entries = dnc_entries.get('entries', [])
            for entry in (dnc_entries or []):
                if str(entry.get('campaign_id', '')) == campaign_id:
                    on_dnc += 1
                    log.info(f"On DNC: {lead['client_name']} ({lead['phone']}) in campaign {campaign_id}")
                    break
        if on_dnc:
            log.info(f'{cm_name}: {on_dnc} leads on campaign DNC (will not be dialed)')

        # Upload leads — campaign DNC on Convoso side prevents calls
        uploaded = 0
        failed = 0
        dnc_blocked = 0
        for lead in cm_info['leads']:
            lead_data = {
                'list_id': list_id,
                'duplicate_check': 'NO',
                'phone_code': '1',
                'first_name': lead['client_name'],
                'last_name': lead['file_num'],
                'phone_number': lead['phone'],
                'client_name': lead['client_name'],
                'case_manager': lead['cm'],
                'file': lead['file_num'],
            }
            r = convoso_post('/v1/leads/insert', lead_data)
            if r.get('success'):
                uploaded += 1
            else:
                err = r.get('text', '') or ''
                # DNC-blocked inserts aren't real failures — they're already
                # counted in on_dnc and just won't be dialed.
                if 'DNC list' in err:
                    dnc_blocked += 1
                    log.info(f"DNC-blocked insert for {lead['client_name']} ({lead['phone']}): on DNC, won't be dialed")
                else:
                    failed += 1
                    log.warning(f"Insert failed for {lead['client_name']} ({lead['phone']}): {err}")

        results.append({
            'cm': cm_name,
            'status': 'uploaded',
            'list_id': list_id,
            'list_name': lname,
            'campaign_id': campaign_id,
            'uploaded': uploaded,
            'failed': failed,
            'dnc_blocked': dnc_blocked,
            'on_dnc': on_dnc,
            'total': cm_info['count'],
        })

    summary['results'] = results
    return summary


# ── Step 3: Activate ───────────────────────────────────────────────────────

def activate(summary):
    """
    For each campaign that was uploaded:
      - Activate the new list
      - Deactivate ALL other lists in that campaign
    Returns activation report.
    """
    report = []

    for r in summary.get('results', []):
        if r['status'] != 'uploaded':
            continue

        campaign_id = r['campaign_id']
        new_list_id = r['list_id']
        cm_name = r['cm']

        # Get all lists in this campaign
        resp = convoso_post('/v1/lists/search', {'campaign_id': campaign_id})
        all_lists = resp.get('data', [])

        activated = 0
        deactivated = 0
        for lst in all_lists:
            lid = str(lst.get('id', ''))
            current_status = lst.get('status', 'N')

            if lid == new_list_id:
                # Activate new list
                if current_status != 'Y':
                    convoso_post('/v1/lists/update', {
                        'list_id': lid,
                        'status': '1',
                    })
                    activated += 1
            else:
                # Deactivate old lists
                if current_status == 'Y':
                    convoso_post('/v1/lists/update', {
                        'list_id': lid,
                        'status': '0',
                    })
                    deactivated += 1

        report.append({
            'cm': cm_name,
            'list_id': new_list_id,
            'activated': activated,
            'deactivated': deactivated,
            'total_lists': len(all_lists),
        })

    summary['activation'] = report
    return summary


# ── Step 4: Asana report ───────────────────────────────────────────────────

def post_to_asana(summary):
    """Create Asana subtask under EPIC-CM with upload + activation summary."""
    if not ASANA_TOKEN:
        log.warning("Asana token not configured, skipping.")
        return None

    headers = {'Authorization': f'Bearer {ASANA_TOKEN}'}
    today_str = datetime.now().strftime('%m/%d/%Y')

    # Build notes
    lines = [f"CM Weekly Upload — {today_str}", ""]

    # Upload results
    results = summary.get('results', [])
    if results:
        lines.append("Upload Summary:")
        total_uploaded = 0
        total_dnc = 0
        for r in results:
            if r['status'] == 'uploaded':
                total_uploaded += r['uploaded']
                total_dnc += r.get('on_dnc', 0)
                dnc = f", {r.get('on_dnc', 0)} on DNC" if r.get('on_dnc') else ""
                fail = f", {r['failed']} failed" if r['failed'] else ""
                lines.append(f"  {r['cm']}: {r['uploaded']} uploaded to list {r['list_id']}{dnc}{fail}")
            elif r['status'] == 'skipped':
                lines.append(f"  {r['cm']}: skipped ({r['reason']})")
            elif r['status'] == 'error':
                lines.append(f"  {r['cm']}: ERROR - {r['reason']}")
        dnc_line = f", {total_dnc} on DNC (won't be dialed)" if total_dnc else ""
        lines.append(f"\nTotal: {total_uploaded} leads{dnc_line}")

    # Activation results
    activation = summary.get('activation', [])
    if activation:
        lines.append("\nActivation:")
        for a in activation:
            lines.append(f"  {a['cm']}: list {a['list_id']} ACTIVE, {a['deactivated']} old deactivated")

    notes_text = '\n'.join(lines)

    try:
        resp = session.post(
            f'https://app.asana.com/api/1.0/tasks/{ASANA_EPIC_CM_GID}/subtasks',
            headers={**headers, 'Content-Type': 'application/json'},
            json={
                'data': {
                    'name': f'CM Weekly Upload — {today_str}',
                    'notes': notes_text,
                    'assignee': ASANA_USER_GID,
                }
            },
            timeout=30,
        )
        result = resp.json()
        task_gid = result.get('data', {}).get('gid')
        if not task_gid:
            log.error(f"Asana subtask creation failed: {result}")
            return None

        task_url = f"https://app.asana.com/0/{ASANA_PROJECT_GID}/{task_gid}"
        log.info(f"Asana report posted: {task_url}")
        return task_url

    except Exception as e:
        log.error(f"Asana error: {e}")
        return None


# ── DNC across all CM campaigns ───────────────────────────────────────────

def dnc_all_cm(phone):
    """Add a phone number to DNC on ALL CM campaigns."""
    phone = clean_phone(phone)
    if not phone:
        return {'success': False, 'error': 'Invalid phone number'}

    added = []
    already = []
    failed = []

    for cm_name, campaign_id in CM_CAMPAIGNS.items():
        # Check if already on DNC for this campaign
        resp = convoso_post('/v1/dnc/search', {
            'phone_number': phone,
            'phone_code': '1',
        })
        dnc_entries = resp.get('data', [])
        if isinstance(dnc_entries, dict):
            dnc_entries = dnc_entries.get('entries', [])

        already_on = any(
            str(e.get('campaign_id', '')) == campaign_id
            for e in (dnc_entries or [])
        )

        if already_on:
            already.append(cm_name)
            continue

        r = convoso_post('/v1/dnc/insert', {
            'phone_number': phone,
            'phone_code': '1',
            'campaign_id': campaign_id,
        })
        if r.get('success'):
            added.append(cm_name)
            log.info(f"DNC added: {phone} → campaign {campaign_id} ({cm_name})")
        else:
            failed.append(cm_name)
            log.warning(f"DNC insert failed: {phone} → campaign {campaign_id}: {r.get('text', '')}")

    return {
        'success': True,
        'phone': phone,
        'added': added,
        'already': already,
        'failed': failed,
    }


def undnc_all_cm(phone):
    """Remove a phone number from DNC on ALL CM campaigns."""
    phone = clean_phone(phone)
    if not phone:
        return {'success': False, 'error': 'Invalid phone number'}

    removed = []
    not_found = []
    failed = []

    for cm_name, campaign_id in CM_CAMPAIGNS.items():
        resp = convoso_post('/v1/dnc/search', {
            'phone_number': phone,
            'phone_code': '1',
        })
        dnc_entries = resp.get('data', [])
        if isinstance(dnc_entries, dict):
            dnc_entries = dnc_entries.get('entries', [])

        on_this = any(
            str(e.get('campaign_id', '')) == campaign_id
            for e in (dnc_entries or [])
        )

        if not on_this:
            not_found.append(cm_name)
            continue

        r = convoso_post('/v1/dnc/delete', {
            'phone_number': phone,
            'phone_code': '1',
            'campaign_id': campaign_id,
        })
        if r.get('success'):
            removed.append(cm_name)
            log.info(f"DNC removed: {phone} from campaign {campaign_id} ({cm_name})")
        else:
            failed.append(cm_name)
            log.warning(f"DNC delete failed: {phone} from campaign {campaign_id}: {r.get('text', '')}")

    return {
        'success': True,
        'phone': phone,
        'removed': removed,
        'not_found': not_found,
        'failed': failed,
    }
