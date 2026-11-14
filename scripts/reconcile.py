#!/usr/bin/env python3
"""
Réconcilie le data.json produit par le scraper avec celui de origin/main
quand le dashboard a commité pendant le run (suppression d'alertes).

Usage : python scripts/reconcile.py BASE OURS REMOTE
  BASE   : data.json au début du run
  OURS   : data.json produit par le scraper (réécrit en place)
  REMOTE : data.json de origin/main
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import ConfigError, load_data, save_data


def dashboard_dismissals(base, remote):
    """Ids des avis supprimés depuis le dashboard dans `remote`."""
    remote_ids = {t.get('id') for t in remote.get('tenders', [])}
    # Le dashboard retire l'avis de `tenders` sans toucher à `history`
    dismissed = {e['id'] for e in remote.get('history', [])
                 if e.get('status') == 'active' and e['id'] not in remote_ids}
    # Comparaison avec le début du run, seulement si le distant en dérive :
    # un dashboard qui réécrit une version périmée ne doit pas faire écarter des avis récents
    if base and remote.get('last_updated') == base.get('last_updated'):
        dismissed |= {t.get('id') for t in base.get('tenders', [])} - remote_ids
    return dismissed


def reconcile(base, ours, remote):
    """Reporte dans `ours` les suppressions faites depuis le dashboard. Retourne (données, ids écartés)."""
    dismissed = dashboard_dismissals(base, remote)
    history = [dict(e) for e in ours.get('history', [])]
    applied = set()
    for entry in history:
        if entry['id'] in dismissed and entry.get('status') == 'active':
            entry['status'] = 'dismissed'
            applied.add(entry['id'])
    merged = {**ours, 'history': history,
              'tenders': [t for t in ours.get('tenders', []) if t.get('id') not in dismissed]}
    return merged, applied


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    base_path, ours_path, remote_path = argv
    try:
        base, ours, remote = load_data(base_path), load_data(ours_path), load_data(remote_path)
    except ConfigError as e:
        print(f'❌ {e}')
        return 1
    if not ours:
        print(f'❌ {ours_path} absent ou vide')
        return 1
    merged, applied = reconcile(base, ours, remote)
    save_data(merged, ours_path)
    print(f'🔀 Réconciliation : {len(applied)} avis écarté(s) depuis le dashboard pendant le run')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
