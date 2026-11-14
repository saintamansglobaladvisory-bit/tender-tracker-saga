import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import json
from datetime import datetime, timedelta, timezone

from scraper import (assess_page, classify_text, extract_notice_blocks, extract_tenders, make_tender_id,
                     merge_history, source_status_entry, update_source_status)

PAGE = 'https://example.gov/marches'


def test_column_header_montant_adjuge_is_not_a_match():
    assert classify_text('Autorité contractante | Fournisseur retenu | Montant adjugé (DJF) | Action') is None


def test_short_acronym_does_not_match_inside_words():
    assert classify_text('Recrutement de consultants pour le projet hydrometrique DRMS-NORD') is None


def test_goods_purchase_for_tax_office_is_rejected():
    text = ('Acquisition de moyens de transport pour la Direction générale des impôts : '
            'lot 4 cinquante quatre (54) motocycles')
    assert classify_text(text) is None


def test_weak_keyword_needs_service_context():
    assert classify_text('Direction générale des impôts - Contacts et horaires') is None
    keywords, confidence = classify_text(
        "Avis à manifestation d'intérêt : assistance technique à la Direction générale des impôts")
    assert keywords == ['impôts']
    assert confidence == 60


def test_strong_keyword_matches_without_accents_and_with_line_breaks():
    keywords, confidence = classify_text('Recrutement d’un consultant : appui à la REFORME\nFISCALE')
    assert 'réforme fiscale' in keywords
    assert confidence == 90


def test_strong_keyword_alone():
    keywords, confidence = classify_text('Public Financial Management Reform Program - Phase II')
    assert keywords == ['public financial management']
    assert confidence == 75


def test_arabic_keyword_with_proclitic():
    result = classify_text('طلب إبداء اهتمام لتقديم خدمات استشارية والإصلاح الضريبي')
    assert result is not None
    assert 'الإصلاح الضريبي' in result[0]


HTML = """
<html><head><title>Portail des marchés publics - finances publiques</title></head>
<body>
  <header><h1>Ministère des Finances publiques - Réforme fiscale</h1></header>
  <nav><ul><li><a href="/dgi">Direction générale des impôts - Administration fiscale</a></li></ul></nav>
  <main>
    <table>
      <tr><th>Référence</th><th>Objet</th><th>Date limite</th></tr>
      <tr><td>AMI-2026-014</td><td><a href="/avis/14">Recrutement d'un cabinet pour l'appui à la réforme fiscale</a></td><td>30/11/2026</td></tr>
      <tr><td>AO-2026-015</td><td><a href="/avis/15">Fourniture de mobilier de bureau</a></td><td>02/12/2026</td></tr>
      <tr><td>AO-2026-016</td><td><a href="/avis/16">Acquisition de véhicules pour la Direction générale des impôts</a></td><td>05/12/2026</td></tr>
    </table>
    <ul class="opportunities">
      <li><h3>Technical assistance on tax administration modernization</h3><a href="https://other.org/eoi/7">Details</a></li>
    </ul>
    <div style="display:none"><p>Consultant pour la gestion des finances publiques (archivé)</p></div>
    <div data-tt-hidden="1"><p>Assistance technique en politique fiscale - modèle caché</p></div>
  </main>
  <footer>Copyright - Gestion des finances publiques</footer>
</body></html>
"""


def test_extracts_each_relevant_notice_with_its_own_link():
    blocks = extract_notice_blocks(HTML, PAGE)
    texts = [b[0] for b in blocks]
    links = [b[1] for b in blocks]
    assert len(blocks) == 2
    assert texts[0].startswith('AMI-2026-014 | Recrutement')
    assert links[0] == 'https://example.gov/avis/14'
    assert 'tax administration' in blocks[1][2]
    assert links[1] == 'https://other.org/eoi/7'


def test_header_nav_footer_and_hidden_content_are_ignored():
    texts = ' '.join(b[0] for b in extract_notice_blocks(HTML, PAGE))
    assert 'Ministère' not in texts
    assert 'Copyright' not in texts
    assert 'archivé' not in texts
    assert 'modèle caché' not in texts


def test_content_wrapped_in_form_or_aside_is_analysed():
    html = """<body><form id="aspnetForm"><aside><table>
      <tr><td>Recrutement d'un consultant en administration fiscale</td></tr>
    </table></aside></form></body>"""
    assert len(extract_notice_blocks(html, PAGE)) == 1


def test_repeated_category_label_is_ignored():
    row = ('66692 | ZIDA/RFP30/2026 | Consultant to undertake the Feasibility Studies for Beitbridge SEZ | '
           'Capital Raising, Debt Restructuring, Listing and Mergers and Acquisitions Consultancy Services')
    assert classify_text(row) is None
    assert classify_text('Consultant for sovereign debt restructuring strategy') is not None


def test_tender_id_is_stable_between_runs():
    first = extract_tenders(HTML, 'Testland', PAGE)
    second = extract_tenders(HTML, 'Testland', PAGE)
    assert [t['id'] for t in first] == [t['id'] for t in second]
    assert len({t['id'] for t in first}) == len(first)
    assert make_tender_id(PAGE, 'Réforme  FISCALE') == make_tender_id(PAGE, 'reforme fiscale')


def test_tender_format_keeps_dashboard_fields():
    tender = extract_tenders(HTML, 'Testland', PAGE)[0]
    for field in ('id', 'country', 'title', 'url', 'detected_at', 'matched_keywords', 'confidence'):
        assert field in tender
    assert tender['source_url'] == PAGE


# ==================== HISTORIQUE ====================

SRC_A = 'https://a.gov/avis'
SRC_B = 'https://b.gov/avis'
DAY0 = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def notice(source, title):
    return {'id': make_tender_id(source, title), 'country': source, 'title': title, 'url': source,
            'source_url': source, 'detected_at': '', 'matched_keywords': ['tax reform'], 'confidence': 75}


def run(previous, results, day):
    tenders, history, new = merge_history(previous, results, now=DAY0 + timedelta(days=day))
    data = json.loads(json.dumps({'tenders': tenders, 'history': history}))
    return data, new


def test_new_notice_is_notified_once_and_keeps_first_seen():
    n = notice(SRC_A, 'Assistance technique réforme fiscale')
    data, new = run({}, {SRC_A: [n]}, 0)
    assert len(new) == 1 and len(data['tenders']) == 1
    data, new = run(data, {SRC_A: [n]}, 1)
    assert new == []
    assert data['tenders'][0]['first_seen'] == data['tenders'][0]['detected_at'] == (DAY0).isoformat()
    assert data['tenders'][0]['last_seen'] == (DAY0 + timedelta(days=1)).isoformat()


def test_failed_source_keeps_its_notices():
    a, b = notice(SRC_A, 'Avis A tax reform'), notice(SRC_B, 'Avis B tax reform')
    data, _ = run({}, {SRC_A: [a], SRC_B: [b]}, 0)
    data, new = run(data, {SRC_A: [a], SRC_B: None}, 1)
    assert new == []
    assert {t['id'] for t in data['tenders']} == {a['id'], b['id']}


def test_notice_gone_from_page_expires_and_returns_without_notification():
    a = notice(SRC_A, 'Avis A tax reform')
    data, _ = run({}, {SRC_A: [a]}, 0)
    data, _ = run(data, {SRC_A: []}, 1)
    assert data['tenders'] == []
    assert data['history'][0]['status'] == 'expired'
    data, new = run(data, {SRC_A: [a]}, 2)
    assert new == []
    assert data['tenders'][0]['status'] == 'active'


def test_notice_dismissed_from_dashboard_never_comes_back():
    a = notice(SRC_A, 'Avis A tax reform')
    data, _ = run({}, {SRC_A: [a]}, 0)
    data['tenders'] = []  # suppression via le dashboard
    data, new = run(data, {SRC_A: [a]}, 1)
    assert new == [] and data['tenders'] == []
    assert data['history'][0]['status'] == 'dismissed'


def test_inactive_notices_are_purged_after_retention():
    a = notice(SRC_A, 'Avis A tax reform')
    data, _ = run({}, {SRC_A: [a]}, 0)
    data, _ = run(data, {SRC_A: []}, 1)
    data, _ = run(data, {SRC_A: []}, 100)
    assert len(data['history']) == 1
    data, _ = run(data, {SRC_A: []}, 182)
    assert data['history'] == []


def test_notices_of_removed_source_expire():
    a = notice(SRC_A, 'Avis A tax reform')
    data, _ = run({}, {SRC_A: [a]}, 0)
    data, _ = run(data, {SRC_B: []}, 1)
    assert data['tenders'] == []


def test_migration_from_old_format_drops_legacy_detections():
    legacy = {'tenders': [{'id': 'old', 'country': 'Djibouti', 'title': 'RÉPUBLIQUE DE DJIBOUTI'}]}
    a = notice(SRC_A, 'Avis A tax reform')
    data, new = run(legacy, {SRC_A: [a]}, 0)
    assert [t['id'] for t in data['tenders']] == [a['id']]
    assert len(new) == 1


# ==================== ÉTAT DES SOURCES ====================

LONG_TEXT = 'Avis de marchés publics en cours. ' * 30


def page(title, body):
    return f'<html><head><title>{title}</title></head><body>{body}</body></html>'


def test_assess_normal_page():
    assert assess_page(page('Marchés publics', f'<p>{LONG_TEXT}</p>'))[0] == 'ok'


def test_assess_cloudflare_challenge_en_and_fr():
    body = '<p>Checking your browser before accessing the site.</p><script src="https://challenges.cloudflare.com/x.js"></script>'
    assert assess_page(page('Just a moment...', body))[0] == 'blocked'
    assert assess_page(page('Un instant...', '<p>Vérification</p>'))[0] == 'blocked'


def test_assess_anti_devtools_page_is_blocked():
    body = '<p>Inspect is not allowed. Developer tools are not permitted on this application.</p>'
    assert assess_page(page('Electronic Government Procurement', body))[0] == 'blocked'


def test_assess_chrome_network_error_page():
    html = ('<html><head><title>site.gov</title></head><body class="neterror">'
            '<p>Ce site est inaccessible</p><div>ERR_TIMED_OUT</div></body></html>')
    state, detail, _ = assess_page(html)
    assert (state, detail) == ('unreachable', 'net::ERR_TIMED_OUT')


def test_assess_not_found_and_empty_pages():
    assert assess_page(page('Page not found | gov.vg', '<p>Sorry</p>'))[0] == 'not_found'
    assert assess_page(page('COLEPS', '<div id="app"></div>'))[0] == 'empty'


def test_assess_ignores_hidden_text_for_emptiness():
    html = page('Portail', f'<div data-tt-hidden="1">{LONG_TEXT}</div>')
    assert assess_page(html)[0] == 'empty'


def test_consecutive_failures_and_last_ok_are_tracked():
    sources = [{'country': 'Testland', 'url': SRC_A}]
    ok = source_status_entry('ok', '', 0)
    ko = source_status_entry('timeout', 'Chargement > 30 s', 0)
    s1 = update_source_status({}, {SRC_A: ok}, sources, now=DAY0)
    s2 = update_source_status(s1, {SRC_A: ko}, sources, now=DAY0 + timedelta(days=1))
    s3 = update_source_status(s2, {SRC_A: ko}, sources, now=DAY0 + timedelta(days=2))
    assert s3[SRC_A]['consecutive_failures'] == 2
    assert s3[SRC_A]['last_ok'] == DAY0.isoformat()
    assert s3[SRC_A]['label'] == 'Timeout' and s3[SRC_A]['country'] == 'Testland'
    s4 = update_source_status(s3, {SRC_A: ok}, sources, now=DAY0 + timedelta(days=3))
    assert s4[SRC_A]['consecutive_failures'] == 0


def test_failing_since_marks_the_start_of_the_outage():
    sources = [{'country': 'Testland', 'url': SRC_A}]
    ok = source_status_entry('ok', '', 0)
    ko = source_status_entry('not_found', 'HTTP 404', 0)
    status = update_source_status({}, {SRC_A: ok}, sources, now=DAY0)
    assert status[SRC_A]['failing_since'] is None
    for day in (1, 2, 5):
        status = update_source_status(status, {SRC_A: ko}, sources, now=DAY0 + timedelta(days=day))
    assert status[SRC_A]['failing_since'] == (DAY0 + timedelta(days=1)).isoformat()
    status = update_source_status(status, {SRC_A: ok}, sources, now=DAY0 + timedelta(days=6))
    assert status[SRC_A]['failing_since'] is None
    # Statut écrit avant l'ajout du champ : le scan précédent sert de début de panne
    legacy = {SRC_A: {'state': 'timeout', 'consecutive_failures': 4, 'checked_at': DAY0.isoformat()}}
    status = update_source_status(legacy, {SRC_A: ko}, sources, now=DAY0 + timedelta(days=1))
    assert status[SRC_A]['failing_since'] == DAY0.isoformat()


def test_removed_source_is_dropped_from_status():
    ok = source_status_entry('ok', '', 0)
    previous = update_source_status({}, {SRC_A: ok, SRC_B: ok}, [{'url': SRC_A}, {'url': SRC_B}], now=DAY0)
    assert list(update_source_status(previous, {SRC_A: ok}, [{'url': SRC_A}], now=DAY0)) == [SRC_A]


# ==================== RÉCUPÉRATION ET PAGINATION ====================

import scraper
from scraper import find_next_page_url, scrape_portal

LISTING = ('<html><body><table>'
           '<tr><td><a href="/avis/{n}">Recrutement d\'un consultant en administration fiscale lot {n}</a></td></tr>'
           '</table>{pager}</body></html>')


def listing(n, pager=''):
    return LISTING.format(n=n, pager=pager) + ('<p>' + 'Portail national des marchés publics. ' * 10 + '</p>')


def test_next_page_link_detection_variants():
    assert find_next_page_url('<a rel="next" href="?page=2">2</a>', PAGE) == PAGE + '?page=2'
    assert find_next_page_url('<ul><li class="pager-next"><a href="/marches?page=1">›</a></li></ul>', PAGE) \
        == 'https://example.gov/marches?page=1'
    assert find_next_page_url('<a href="/p/2">Suivant »</a>', PAGE) == 'https://example.gov/p/2'
    assert find_next_page_url('<a aria-label="Next page" href="/p/2"><i></i></a>', PAGE) == 'https://example.gov/p/2'
    assert find_next_page_url('<a href="/p/2">Siguiente</a>', PAGE) == 'https://example.gov/p/2'


def test_next_page_link_ignores_unrelated_or_unsafe_links():
    assert find_next_page_url('<a href="/news/2">Article suivant : la réforme</a>', PAGE) is None
    assert find_next_page_url('<a href="https://other.org/p/2">Next</a>', PAGE) is None
    assert find_next_page_url('<a href="javascript:__doPostBack(1)">Next</a>', PAGE) is None
    assert find_next_page_url('<li class="next disabled"><a href="/p/2">Next</a></li>', PAGE) is None
    assert find_next_page_url('<a href="#">›</a>', PAGE) is None
    # Navigation entre sections (AfDB / Drupal) : ni <link rel=next> d'en-tête, ni URL sans numéro
    afdb = ('<head><link rel="next" href="/procurement/new-procurement-policy"></head>'
            '<body><a rel="next" href="/procurement/framework">Procurement framework</a></body>')
    assert find_next_page_url(afdb, PAGE) is None


def fake_fetcher(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        result = pages.get(url, ('error', 'inconnu', '', 0))
        return result if isinstance(result, tuple) else ('ok', '', result, len(result))
    return fetch, calls


def test_scrape_follows_pagination_and_dedupes(monkeypatch):
    p2 = 'https://example.gov/marches?page=2'
    p3 = 'https://example.gov/marches?page=3'
    fetch, calls = fake_fetcher({
        PAGE: listing(1, '<a rel="next" href="?page=2">2</a>'),
        p2: listing(2, '<a rel="next" href="?page=3">3</a>'),
        p3: listing(2, '<a rel="next" href="?page=4">4</a>'),
    })
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('fake', fetch)])
    assert calls == [PAGE, p2, p3]
    assert len(tenders) == 2
    assert {t['source_url'] for t in tenders} == {PAGE}
    assert status['pages'] == 3 and status['complete'] is True and status['notices'] == 2


def test_failed_pagination_marks_source_incomplete(monkeypatch):
    monkeypatch.setattr(scraper, 'RETRY_DELAY', 0)
    fetch, _ = fake_fetcher({PAGE: listing(1, '<a rel="next" href="?page=2">2</a>')})
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('fake', fetch)])
    assert len(tenders) == 1
    assert status['state'] == 'ok' and status['complete'] is False
    assert 'pagination interrompue' in status['detail']


def test_transient_error_is_retried(monkeypatch):
    monkeypatch.setattr(scraper, 'RETRY_DELAY', 0)
    results = [('timeout', 'lent', '', 0), ('ok', '', listing(1), 500)]
    calls = []

    def flaky(url):
        calls.append(url)
        return results.pop(0)
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('fake', flaky)])
    assert len(calls) == 2 and len(tenders) == 1 and status['state'] == 'ok'


def test_fallback_fetcher_used_when_primary_fails(monkeypatch):
    monkeypatch.setattr(scraper, 'RETRY_DELAY', 0)
    blocked, blocked_calls = fake_fetcher({PAGE: ('blocked', 'Protection anti-robot', '', 0)})
    fallback, _ = fake_fetcher({PAGE: listing(1)})
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('selenium', blocked), ('requests', fallback)])
    assert len(blocked_calls) == 1  # un blocage n'est pas retenté
    assert len(tenders) == 1
    assert status['method'] == 'requests' and 'selenium: Bloqué' in status['detail']


def test_all_fetchers_failing_returns_none(monkeypatch):
    monkeypatch.setattr(scraper, 'RETRY_DELAY', 0)
    down, calls = fake_fetcher({})
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('a', down), ('b', down)])
    assert tenders is None and status['state'] == 'error'
    assert len(calls) == 4


def test_failure_state_comes_from_primary_fetcher(monkeypatch):
    blocked, _ = fake_fetcher({PAGE: ('blocked', 'Protection anti-robot', '', 0)})
    empty, _ = fake_fetcher({PAGE: ('empty', '0 caractères visibles', '', 0)})
    tenders, status = scrape_portal('Testland', PAGE, fetchers=[('selenium', blocked), ('requests', empty)])
    assert tenders is None and status['state'] == 'blocked'
    assert 'requests: Page vide' in status['detail']


def test_partial_source_does_not_expire_notices():
    a, b = notice(SRC_A, 'Avis A tax reform'), notice(SRC_A, 'Avis B tax reform')
    data, _ = run({}, {SRC_A: [a, b]}, 0)
    tenders, _, _ = merge_history(data, {SRC_A: [a]}, now=DAY0 + timedelta(days=1), partial_sources={SRC_A})
    assert len(tenders) == 2


# ==================== Configuration et fichiers ====================

import pytest

from scraper import ConfigError, load_data, load_sources, save_data


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding='utf-8')
    return str(path)


def test_corrupted_data_file_stops_the_run(tmp_path):
    path = write(tmp_path, 'data.json', '{"tenders": [')
    with pytest.raises(ConfigError):
        load_data(path)


def test_missing_data_file_starts_empty(tmp_path):
    assert load_data(str(tmp_path / 'absent.json')) == {}


def test_save_data_is_atomic_and_readable(tmp_path):
    path = str(tmp_path / 'docs' / 'data.json')
    save_data({'tenders': [{'id': 'x'}]}, path)
    assert load_data(path) == {'tenders': [{'id': 'x'}]}
    assert not os.path.exists(path + '.tmp')


@pytest.mark.parametrize('content', [
    '[]',
    '{"country": "A"}',
    '[{"country": "A"}]',
    '[{"country": "A", "url": "ftp://a"}]',
    '[{"country": "A", "url": "https://a"}, {"country": "B", "url": "https://a"}]',
    '[{"country": "A", "url": ',
])
def test_invalid_sources_are_rejected(tmp_path, content):
    with pytest.raises(ConfigError):
        load_sources(write(tmp_path, 'sources.json', content))


def test_repository_sources_file_is_valid():
    sources = load_sources()
    assert len(sources) > 50
    assert all(s.get('region') for s in sources)


# ==================== Alertes sources en échec ====================

from scraper import send_source_failure_alert, sources_to_alert


def test_source_alert_only_when_threshold_is_reached():
    sources = [{'country': 'Testland', 'url': SRC_A}]
    ko = source_status_entry('blocked', 'HTTP 403', 0)
    status, alerts = {}, []
    for day in range(5):
        status = update_source_status(status, {SRC_A: ko}, sources, now=DAY0 + timedelta(days=day))
        alerts.append(len(sources_to_alert(status)))
    # Une seule alerte, au 3e échec consécutif, pas les jours suivants
    assert alerts == [0, 0, 1, 0, 0]
    ok = source_status_entry('ok', '', 0)
    status = update_source_status(status, {SRC_A: ok}, sources, now=DAY0 + timedelta(days=5))
    assert sources_to_alert(status) == []


def test_source_alert_is_sent_to_webhook(monkeypatch):
    sent = []

    class Response:
        status_code = 204

    monkeypatch.setattr(scraper.requests, 'post', lambda url, json, timeout: sent.append(json) or Response())
    failing = [{'country': 'Testland', 'state': 'blocked', 'label': 'Bloqué', 'detail': 'HTTP 403',
                'consecutive_failures': 3}]
    monkeypatch.delenv('WEBHOOK_URL', raising=False)
    send_source_failure_alert(failing)
    assert sent == []
    monkeypatch.setenv('WEBHOOK_URL', 'https://discord.example/hook')
    monkeypatch.setenv('NOTIFICATION_ENABLED', 'false')
    send_source_failure_alert(failing)
    assert len(sent) == 1 and 'Testland' in sent[0]['content'] and 'HTTP 403' in sent[0]['content']


# ==================== Chiffrement de data.json ====================

from conftest import TEST_PASSWORD
from scraper import decrypt_data, encrypt_data, is_encrypted, load_keywords


def test_saved_data_is_encrypted_and_readable_with_the_code(tmp_path):
    path = str(tmp_path / 'data.json')
    data = {'tenders': [{'id': 'x', 'title': 'Réforme fiscale – appui'}]}
    save_data(data, path)
    raw = json.load(open(path, encoding='utf-8'))
    assert is_encrypted(raw) and 'Réforme' not in open(path, encoding='utf-8').read()
    assert load_data(path) == data


def test_wrong_or_missing_code_stops_the_run(tmp_path, monkeypatch):
    path = str(tmp_path / 'data.json')
    save_data({'tenders': []}, path)
    with pytest.raises(ConfigError):
        load_data(path, password='mauvais-code')
    monkeypatch.delenv('DASHBOARD_PASSWORD')
    with pytest.raises(ConfigError):
        load_data(path)
    with pytest.raises(ConfigError):
        save_data({'tenders': []}, path)


def test_plain_data_file_is_still_read_for_migration(tmp_path):
    path = write(tmp_path, 'data.json', '{"tenders": [{"id": "x"}]}')
    assert load_data(path) == {'tenders': [{'id': 'x'}]}


def test_each_save_uses_a_fresh_salt_and_iv():
    a, b = encrypt_data({'a': 1}, TEST_PASSWORD), encrypt_data({'a': 1}, TEST_PASSWORD)
    assert a['salt'] != b['salt'] and a['iv'] != b['iv']
    assert decrypt_data(a, TEST_PASSWORD) == {'a': 1}


# ==================== Mots-clés modifiables ====================

def test_repository_keywords_file_is_valid():
    strong, weak = load_keywords()
    assert len(strong) > 20 and len(weak) > 5
    assert scraper.KEYWORDS_ERROR is None


@pytest.mark.parametrize('content', [
    '[]',
    '{"strong": {}, "weak": {}}',
    '{"strong": {"fr": []}, "weak": {}}',
    '{"strong": ["réforme fiscale"], "weak": []}',
    '{"strong": {"fr": ["réforme fiscale"]}}',
    '{"strong": {"de": ["Steuerreform"]}, "weak": {}}',
    '{"strong": {"fr": ["réforme fiscale"], "es": ["Reforme  fiscale"]}, "weak": {}}',
    '{"strong": {"en": ["pfm"]}, "weak": {"en": ["PFM"]}}',
    '{"strong": {"fr": ["x"]}, "weak": {}}',
    '{"strong": {"fr": ["!!"]}, "weak": {}}',
    '{"strong": {"fr": [12]}, "weak": {}}',
])
def test_invalid_keywords_are_rejected(tmp_path, content):
    with pytest.raises(ConfigError):
        load_keywords(write(tmp_path, 'keywords.json', content))


def test_keywords_from_file_drive_the_detection(tmp_path):
    strong, weak = load_keywords(write(tmp_path, 'keywords.json',
                                       '{"strong": {"fr": ["cadastre fiscal"]}, "weak": {}}'))
    previous = (scraper.STRONG_KEYWORDS, scraper.WEAK_KEYWORDS)
    try:
        scraper.set_keywords(strong, weak)
        assert classify_text('Recrutement consultant : cadastre fiscal de Bamako')
        assert classify_text('Assistance technique en réforme fiscale') is None
    finally:
        scraper.set_keywords(*previous)
