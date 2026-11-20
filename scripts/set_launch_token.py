"""
Enregistre le token qui permet au bouton « Lancer l'analyse » du dashboard de lancer le workflow.

Le token est chiffré avec le code d'accès du dashboard (même format que docs/data.json) dans
docs/launch-token.json : le dépôt étant public, il ne doit jamais y figurer en clair.
À relancer quand le token expire ou quand le code d'accès (secret DASHBOARD_PASSWORD) change.

Token attendu : « fine-grained », limité à ce dépôt, permission Actions en lecture et écriture.
Le token et le code d'accès sont demandés sans être affichés ni enregistrés.
"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper  # noqa: E402

TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'launch-token.json')


def main():
    token = getpass.getpass('Token GitHub (ne s’affiche pas) : ').strip()
    if not token.startswith('github_pat_'):
        print('Échec : le token doit être un token « fine-grained » (il commence par github_pat_).')
        return 1
    password = getpass.getpass("Code d'accès du dashboard (ne s'affiche pas) : ")
    if password != getpass.getpass('Code d’accès, une seconde fois : '):
        print('Échec : les deux codes saisis sont différents.')
        return 1
    data_file = os.path.join(os.path.dirname(TOKEN_FILE), 'data.json')
    if os.path.exists(data_file):
        # Un code différent de celui des données rendrait le bouton inutilisable
        try:
            scraper.load_data(data_file, password)
        except scraper.ConfigError:
            print("Échec : ce code n'ouvre pas docs/data.json. Utilisez le code d'accès du dashboard.")
            return 1
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(scraper.encrypt_data({'token': token}, password), f, indent=2)
        f.write('\n')
    print(f'Token chiffré enregistré dans {TOKEN_FILE}.')
    return 0


if __name__ == '__main__':
    code = main()
    input('Terminé. Appuyez sur Entrée pour fermer.')
    sys.exit(code)
