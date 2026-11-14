import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import json
from datetime import datetime, timedelta, timezone

from reconcile import main as reconcile_main, reconcile
from scraper import load_data, make_tender_id, merge_history, save_data

SRC = 'https://a.gov/avis'
DAY0 = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def notice(title):
    return {'id': make_tender_id(SRC, title), 'country': 'Testland', 'title': title, 'url': SRC,
            'source_url': SRC, 'detected_at': '', 'matched_keywords': ['tax reform'], 'confidence': 75}


def run(previous, found, day):
    now = DAY0 + timedelta(days=day)
    tenders, history, new = merge_history(previous, {SRC: found}, now=now)
    data = {'tenders': tenders, 'history': history, 'last_updated': now.isoformat(),
            'new_tenders_count': len(new)}
    return json.loads(json.dumps(data))


def ids(items):
    return {t['id'] for t in items}


N1, N2, N3 = notice('Réforme fiscale - lot 1'), notice('Réforme fiscale - lot 2'), notice('Tax reform EOI')


def scenario():
    base = run({}, [N1, N2], 0)
    ours = run(base, [N1, N2, N3], 1)
    # Pendant le run, le dashboard retire N1 de `tenders`
    remote = {**base, 'tenders': [t for t in base['tenders'] if t['id'] != N1['id']]}
    return base, ours, remote


def test_dashboard_dismissal_during_run_is_kept():
    base, ours, remote = scenario()
    merged, applied = reconcile(base, ours, remote)
    assert applied == {N1['id']}
    assert ids(merged['tenders']) == {N2['id'], N3['id']}
    status = {e['id']: e['status'] for e in merged['history']}
    assert status[N1['id']] == 'dismissed'
    # Le run suivant ne fait pas revenir l'avis écarté
    after = run(merged, [N1, N2, N3], 2)
    assert N1['id'] not in ids(after['tenders'])


def test_no_remote_change_keeps_our_result():
    base, ours, _ = scenario()
    assert reconcile(base, ours, base)[0] == ours
    assert reconcile(base, ours, {})[0] == ours


def test_reconcile_is_idempotent_across_push_retries():
    base, ours, remote = scenario()
    once, _ = reconcile(base, ours, remote)
    twice, applied = reconcile(base, once, remote)
    assert twice == once and applied == set()


def test_stale_remote_does_not_dismiss_recent_notices():
    base, ours, _ = scenario()
    # Dashboard ayant réécrit une version antérieure au run précédent (sans N2)
    stale = run({}, [N1], -1)
    merged, applied = reconcile(base, ours, stale)
    assert applied == set()
    assert ids(merged['tenders']) == ids(ours['tenders'])


def test_dismissal_detected_from_remote_history_alone():
    base, ours, remote = scenario()
    remote = {**remote, 'last_updated': 'autre'}
    merged, applied = reconcile(base, ours, remote)
    assert applied == {N1['id']}


def test_command_line_rewrites_our_file(tmp_path):
    base, ours, remote = scenario()
    paths = []
    for name, data in (('base', base), ('ours', ours), ('remote', remote)):
        path = str(tmp_path / f'{name}.json')
        save_data(data, path)
        paths.append(path)
    assert reconcile_main(paths) == 0
    assert ids(load_data(paths[1])['tenders']) == {N2['id'], N3['id']}
    # Fichier distant absent (git show en échec) : rien n'est écarté
    assert reconcile_main([paths[0], paths[1], str(tmp_path / 'absent.json')]) == 0
    assert reconcile_main(paths[:2]) == 2
