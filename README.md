# Tender-Tracker SAGA

Veille automatique des appels d'offres liés aux finances publiques (fiscalité, mobilisation des ressources, gestion des finances publiques, dette) sur 75 portails de marchés publics et de bailleurs.

- **Scraper** (GitHub Actions) : analyse les portails chaque jour à 5 h 17 UTC, ou à la demande depuis le dashboard.
- **Dashboard** (GitHub Pages) : affiche les offres détectées, l'état des sources et les mots-clés, protégé par un code d'accès.

## Fonctionnement

1. Le workflow `.github/workflows/scraper.yml` lance les tests, puis `scripts/scraper.py`.
2. Le scraper ouvre chaque portail dans un Chrome sans écran (repli en HTML simple si Chrome échoue), découpe la page en avis et retient ceux qui contiennent les mots-clés.
3. Les résultats sont fusionnés avec l'historique (offre active, expirée ou écartée) et enregistrés dans `docs/data.json`, **chiffré** avec le code d'accès du dashboard.
4. GitHub Pages republie le dashboard ; Discord reçoit les nouvelles offres et les pannes de sources (si le webhook est configuré).

Aucune protection anti-robot n'est contournée : les sources bloquées apparaissent dans la section « À vérifier manuellement » du dashboard.

## Fichiers

| Fichier | Rôle |
|---|---|
| `scripts/sources.json` | Liste des portails surveillés (modifiable depuis l'onglet Sources) |
| `scripts/keywords.json` | Mots-clés forts et faibles, par langue (modifiables depuis l'onglet Mots-clés) |
| `scripts/scraper.py` | Lecture des portails, détection, historique, état des sources, alertes |
| `scripts/reconcile.py` | Fusion avec les suppressions faites depuis le dashboard pendant une analyse |
| `docs/index.html` | Dashboard |
| `docs/data.json` | Données chiffrées, créées par la première analyse |

**Mots-clés forts** : thème précis, un seul suffit pour retenir une offre (sauf achat de fournitures ou de travaux).
**Mots-clés faibles** : thème large, retenus seulement si l'offre porte aussi sur une prestation de service.

## Mise en service

1. **Settings → Pages** : source « Deploy from a branch », branche `main`, dossier `/docs`.
2. **Settings → Secrets and variables → Actions** :
   - `DASHBOARD_PASSWORD` (obligatoire) : code d'accès du dashboard, qui sert aussi de clé de chiffrement des données ;
   - `WEBHOOK_URL` (facultatif) : webhook Discord pour les alertes ;
   - `NOTIFICATION_ENABLED` (facultatif) : `true` pour recevoir les nouvelles offres.
3. **Actions → Tender-Tracker SAGA - Scraper → Run workflow** pour la première analyse.
4. Sur le dashboard, les actions qui modifient le dépôt demandent un token GitHub *fine-grained*, limité à ce dépôt, avec les permissions **Contents** et **Actions** en lecture et écriture.

Changer `DASHBOARD_PASSWORD` rend les données existantes illisibles : la prochaine analyse échoue. Pour changer de code, supprimer `docs/data.json` puis relancer une analyse (l'historique repart de zéro).

## Développement local

```bash
pip install -r scripts/requirements-dev.txt
python -m pytest scripts -q

# Analyse complète (Chrome requis), données chiffrées avec le code fourni
DASHBOARD_PASSWORD='code-de-test' python scripts/scraper.py
python -m http.server 8000 --directory docs
```
