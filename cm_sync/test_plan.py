"""Offline test of build_plan(): synthetic data only, no network, no secrets.  Run: python -I test_plan.py"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cm_sf_sync as S

cmmap = json.loads((Path(__file__).resolve().parent / 'cm_map.json').read_text(encoding='utf-8'))
A, B, K = '005Pe00000EwucsIAB', '005Pe00000EvTd7IAF', '005Pe00000UXhEbIAL'   # Alondra 4135, Alquinton 4999, Kalyssa 4997


def cl(file, phone, cm, stage='Enrolled', name='N'):
    return dict(opp_id='o' + file, file=file, name=name, phone=phone, phone_alt='', stage=stage, cm_id=cm, cm_name='x')


def ld(i, lst, phone, file='', camp=None):
    return dict(id=i, list_id=lst, phone_number=phone, file=file, first_name='F', last_name='L')


state = {'4135': {'active_list': {'id': '100'}, 'leads': [
            ld(1, '100', '5550000001', 'F1'),      # correct -> no_change
            ld(2, '100', '5550000002', 'F2'),      # CM moved to Alquinton -> move
            ld(3, '100', '5550000003', 'F3'),      # client cancelled / not in SF -> remove
            ld(4, '100', '5550000004', 'F4'),      # phone differs -> update_phone
            ld(5, '100', '5550000005', '')]},      # no file id, phone match -> no_change via phone
         '4999': {'active_list': {'id': '200'}, 'leads': []},
         '4997': {'active_list': None, 'leads': []}}       # Kalyssa: no active list

clients = [
    cl('F1', '5550000001', A),
    cl('F2', '5550000002', B),
    cl('F4', '5550000099', A),                    # SF phone changed
    cl('F5', '5550000005', A),                    # file id differs, phone matches lead 5
    cl('F6', '5550000006', B),                    # brand new -> add
    cl('F7', '5550000007', K),                    # new, campaign has no active list -> add (blocked)
    cl('F8', '5550000008', '005XX0000000NEWCM'),  # unknown CM (e.g. a new hire) -> flag_unmapped_cm
    cl('F9', '', A),                              # no phone, no match -> flag_no_phone
    cl('F10', '5550000010', A),                   # on NSF -> excluded
]
rows, st = S.build_plan(clients, state, cmmap, nsf_phones={'5550000010'}, exclude_nsf=True)  # the switch, tested ON
by = {}
for r in rows:
    by.setdefault(r['action'], []).append(r)

def check(name, cond):
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        check.bad += 1
check.bad = 0

check('no_change F1 and phone-matched lead 5', sorted(r['lead_id'] for r in by['no_change']) == ['1', '5'])
check('move: lead 2 from 4135 to 4999 list 200, same lead id (no delete/re-insert)',
      len(by['move']) == 1 and by['move'][0]['lead_id'] == '2' and by['move'][0]['camp_from'] == '4135'
      and by['move'][0]['camp_to'] == '4999' and by['move'][0]['target_list'] == '200')
check('remove: lead 3, blocked because destination undecided', len(by['remove']) == 1 and by['remove'][0]['lead_id'] == '3'
      and by['remove'][0]['blocked'])
check('update_phone: lead 4', len(by['update_phone']) == 1 and by['update_phone'][0]['lead_id'] == '4')
adds = {r['file']: r for r in by['add']}
check('add F6 into list 200 and F7 blocked (no active list)', set(adds) == {'F6', 'F7'} and adds['F6']['target_list'] == '200'
      and not adds['F6']['blocked'] and 'no active list' in adds['F7']['blocked'])
check('unmapped CM flagged, not dropped', len(by['flag_unmapped_cm']) == 1 and by['flag_unmapped_cm'][0]['file'] == 'F8')
check('no phone flagged', len(by['flag_no_phone']) == 1)
check('NSF client excluded from set', st['excluded_nsf'] == 1 and not any(r['file'] == 'F10' for r in rows))
check('every SF client in set accounted for exactly once',
      sum(1 for r in rows if r['file'] in {c['file'] for c in clients} and r['action'] != 'remove') == 8)
check('masked phones only in plan rows', all('5550' not in r['phone_masked'] for r in rows))

# ---- default (Pranay 2026-10-27): NSF/EFT clients stay with their CM until Cancelled ----
rows_d, st_d = S.build_plan(clients, state, cmmap, nsf_phones={'5550000010'})
check('default keeps NSF client with its CM', st_d['excluded_nsf'] == 0 and any(r['file'] == 'F10' for r in rows_d))
check("NSF stage is in CLIENT_STAGES", 'NSF' in S.CLIENT_STAGES and 'Cancelled' not in S.CLIENT_STAGES)

# ---- test accounts in cm_map 'ignore' are skipped silently ----
rows_i, _ = S.build_plan([cl('T1', '5559990001', '005Pe00000GtpiDIAR')], state, cmmap)
check('ignored test account (Adarsha) produces no row at all', not any(r['file'] == 'T1' for r in rows_i))

# ---- phones: placeholder SF numbers are never used; Convoso number equal to ANY SF number is not an update ----
check('placeholder number detected', S.looks_fake('9491111111') and not S.looks_fake('9375550759'))
c_alt = cl('P1', '9491111111', A); c_alt['phone'] = '9375557759'; c_alt['phones'] = {'9375557759'}
st_p = {k: dict(v, leads=list(v['leads'])) for k, v in state.items()}
rows_p, _ = S.build_plan([c_alt], st_p, cmmap)
check('Convoso number = SF Phone__c is not a phone update', not any(r['action'] == 'update_phone' and r['file'] == 'P1' for r in rows_p))

# ---- scenario 2: duplicate leads for one file id, ambiguity, NSF lead left in a CM list ----
state2 = {'4135': {'active_list': {'id': '100'}, 'leads': [
            ld(11, '100', '5551110001', 'D1'), ld(12, '100', '5551110002', 'D1'),   # same file, SF phone = ...01
            ld(13, '100', '5551110003', 'D2'), ld(14, '100', '5551110004', 'D2'),   # same file, SF phone matches neither
            ld(15, '100', '5551110010', 'N1')]},                                       # NSF client still in CM list
          '4999': {'active_list': {'id': '200'}, 'leads': []}}
rows2, st2 = S.build_plan([cl('D1', '5551110001', A), cl('D2', '5551110099', A), cl('N1', '5551110010', A)],
                          state2, cmmap, nsf_phones={'5551110010'}, exclude_nsf=True)
b2 = {}
for r in rows2:
    b2.setdefault(r['action'], []).append(r)
check('dup leads: keep SF-phone lead (no_change 11), extra 12 flagged not removed',
      [r['lead_id'] for r in b2.get('no_change', [])] == ['11'] and [r['lead_id'] for r in b2.get('flag_duplicate_lead', [])] == ['12'])
check('ambiguous file id flagged once and its leads are NOT removed', len(b2.get('flag_ambiguous', [])) == 1
      and not any(r['lead_id'] in ('13', '14') for r in b2.get('remove', [])))
check('NSF client in a CM list is removed with an NSF reason',
      [r['lead_id'] for r in b2.get('remove', [])] == ['15'] and b2['remove'][0]['reason'].startswith('NSF'))

# ---- scenario 2b: same client + same phone in the OLD campaign (lower id) and the RIGHT campaign ----
state2b = {'4135': {'active_list': {'id': '100'}, 'leads': [ld(31, '100', '5553330001', 'X1')]},
           '4999': {'active_list': {'id': '200'}, 'leads': [ld(32, '200', '5553330001', 'X1')]}}
rows2b, _ = S.build_plan([cl('X1', '5553330001', B)], state2b, cmmap)
b2b = {}
for r in rows2b:
    b2b.setdefault(r['action'], []).append(r)
check('same file+phone in old and right campaign: keep right one, remove old duplicate, NO move',
      [r['lead_id'] for r in b2b.get('no_change', [])] == ['32'] and [r['lead_id'] for r in b2b.get('remove', [])] == ['31']
      and 'move' not in b2b and 'duplicate' in b2b['remove'][0]['reason'])

# ---- scenario 3: two SF clients share one phone; only one lead exists ----
state3 = {'4135': {'active_list': {'id': '100'}, 'leads': [ld(21, '100', '5552220001', 'H1')]},
          '4999': {'active_list': {'id': '200'}, 'leads': []}}
rows3, _ = S.build_plan([cl('H2', '5552220001', B), cl('H1', '5552220001', A)], state3, cmmap)   # H2 listed first on purpose
b3 = {r['action']: r for r in rows3}
check('shared phone: first client matched, second flagged (not added)',
      b3.get('no_change', {}).get('file') == 'H1' and b3.get('flag_shared_phone', {}).get('file') == 'H2' and 'add' not in b3)

# writes are blocked unless --apply
class Fake(S.Convoso):
    def __init__(self):
        self.token, self.base, self.calls = 'x', 'http://127.0.0.1:9', S.Counter()
try:
    Fake().post('/v1/leads/update', {'lead_id': '1'})
    check('write endpoint blocked in dry-run', False)
except RuntimeError as e:
    check('write endpoint blocked in dry-run: ' + str(e), 'WRITE BLOCKED' in str(e))

# MAX_CHANGES rail
many = [dict(action='move', blocked='', lead_id=str(i), target_list='1', cm_to='x') for i in range(S.MAX_CHANGES + 1)]
try:
    S.apply_plan(Fake(), many, force=False)
    check('apply refuses above MAX_CHANGES without --force', False)
except SystemExit as e:
    check('apply refuses above MAX_CHANGES without --force', 'REFUSING' in str(e))

print('\nFAILED: %d' % check.bad if check.bad else '\nALL PASSED')
sys.exit(1 if check.bad else 0)
