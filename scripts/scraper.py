#!/usr/bin/env python3
"""
Tender-Tracker SAGA - Scraper Automatisé
Scrape les portails d'appels d'offres publics pour détecter les opportunités en DRM/Fiscalité
"""

import json
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import time
from urllib.parse import urldefrag, urljoin, urlparse
import hashlib
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import warnings
import base64
import secrets
try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ==================== CONFIGURATION ====================
# Mots-clés modifiables depuis le dashboard (scripts/keywords.json) :
# « strong » = thème précis, un seul suffit (sauf achat de biens/travaux) ;
# « weak » = thème générique, retenu seulement avec un terme de prestation intellectuelle.
KEYWORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keywords.json')
KEYWORD_MIN_CHARS = 2
KEYWORD_MAX_CHARS = 120
# Les mots-clés sont rangés par langue (affichage du dashboard) ; la détection les utilise tous
KEYWORD_LANGUAGES = ('fr', 'en', 'es', 'pt', 'ar')

SERVICE_TERMS = [
    'assistance technique', 'consultant', 'consultants', 'consultance',
    "bureau d'études", "bureau d'etudes", 'étude', 'études',
    'formation', 'renforcement des capacités', 'appui', 'accompagnement',
    "manifestation d'intérêt", "manifestations d'intérêt", 'ami',
    'termes de référence', 'prestations intellectuelles', 'expertise',
    'technical assistance', 'consultancy', 'consulting', 'advisory',
    'capacity building', 'expression of interest', 'expressions of interest',
    'eoi', 'terms of reference', 'study', 'assessment', 'diagnostic',
    'evaluation', 'training', 'individual consultant', 'firm',
    'asistencia técnica', 'consultoría', 'assistência técnica', 'consultoria',
    'مساعدة فنية', 'استشارات', 'استشاري', 'دراسة',
]

GOODS_WORKS_TERMS = [
    'fourniture', 'fournitures', 'acquisition de', 'achat de', 'travaux',
    'construction', 'réhabilitation', 'véhicules', 'motocycles', 'mobilier',
    'matériel', 'équipements', 'supply of', 'supply and delivery',
    'purchase of', 'vehicles', 'furniture', 'works', 'equipment',
    'adquisición de', 'suministro', 'obras', 'aquisição de', 'fornecimento',
    'توريد', 'أشغال', 'اقتناء',
]

# Libellés de catégorie répétés sur chaque ligne de certains portails : ignorés au matching
IGNORED_PHRASES = [
    'capital raising, debt restructuring, listing and mergers and acquisitions',  # Zimbabwe (PRAZ)
]

MAX_TENDERS_PER_SOURCE = 50
# Les avis qui ne sont plus affichés (expirés, écartés) sont purgés après ce délai
HISTORY_RETENTION_DAYS = 180
MIN_BLOCK_CHARS = 20
MAX_BLOCK_CHARS = 1500
DATA_FILE = 'docs/data.json'
# data.json est public (GitHub Pages) : il est chiffré avec le code d'accès du dashboard,
# fourni au workflow par le secret du même nom
DATA_PASSWORD_ENV = 'DASHBOARD_PASSWORD'
ENCRYPTION_FORMAT = 'tt-enc-v1'
KDF_ITERATIONS = 600_000
# Liste des portails surveillés (region, country, url)
SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sources.json')
# Configuration réseau
REQUEST_TIMEOUT = 30
# Certains pare-feux rejettent les versions de Chrome trop anciennes : à garder récent
# (le navigateur Selenium, lui, annonce sa vraie version)
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}

# ==================== FONCTIONS UTILITAIRES ====================

class ConfigError(Exception):
    pass

def data_password(password=None):
    """Code d'accès servant de clé de chiffrement (paramètre, sinon variable d'environnement)."""
    password = password or os.getenv(DATA_PASSWORD_ENV)
    if not password:
        raise ConfigError(f"{DATA_PASSWORD_ENV} non défini : impossible de lire ou d'écrire les données chiffrées")
    return password

def _derive_key(password, salt, iterations):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations, dklen=32)

def encrypt_data(data, password):
    """Enveloppe AES-256-GCM, clé dérivée du code d'accès (PBKDF2-SHA256), lisible par le dashboard (WebCrypto)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    plaintext = json.dumps(data, ensure_ascii=False).encode('utf-8')
    ciphertext = AESGCM(_derive_key(password, salt, KDF_ITERATIONS)).encrypt(iv, plaintext, None)
    b64 = lambda raw: base64.b64encode(raw).decode('ascii')
    return {'format': ENCRYPTION_FORMAT, 'kdf': 'PBKDF2-SHA256', 'iterations': KDF_ITERATIONS,
            'salt': b64(salt), 'iv': b64(iv), 'ciphertext': b64(ciphertext)}

def is_encrypted(data):
    return isinstance(data, dict) and data.get('format') == ENCRYPTION_FORMAT

def decrypt_data(envelope, password):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        salt, iv, ciphertext = (base64.b64decode(envelope[k]) for k in ('salt', 'iv', 'ciphertext'))
        key = _derive_key(password, salt, int(envelope['iterations']))
        return json.loads(AESGCM(key).decrypt(iv, ciphertext, None).decode('utf-8'))
    except InvalidTag:
        raise ConfigError(f"Données illisibles : {DATA_PASSWORD_ENV} ne correspond pas au code utilisé pour les chiffrer")
    except (KeyError, ValueError, TypeError) as e:
        raise ConfigError(f'Enveloppe chiffrée invalide ({e})')

def load_data(path=DATA_FILE, password=None):
    """
    Charge l'état précédent (avis, historique, état des sources), en clair ou chiffré.
    Un fichier illisible arrête le run : repartir de zéro effacerait l'historique.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path} illisible ({e}) : run annulé pour ne pas écraser l'historique")
    if is_encrypted(data):
        data = decrypt_data(data, data_password(password))
    if not isinstance(data, dict):
        raise ConfigError(f"{path} : objet JSON attendu")
    return data

def load_sources(path=SOURCES_FILE):
    """Charge et valide la liste des portails."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            sources = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f'{path} illisible ({e})')
    if not isinstance(sources, list) or not sources:
        raise ConfigError(f'{path} : liste de sources non vide attendue')
    seen = set()
    for i, source in enumerate(sources):
        if not isinstance(source, dict) or not source.get('country') or not source.get('url'):
            raise ConfigError(f'{path} : source n°{i + 1} sans "country" ou "url"')
        if not urlparse(source['url']).scheme.startswith('http'):
            raise ConfigError(f"{path} : URL invalide pour {source['country']}")
        if source['url'] in seen:
            raise ConfigError(f"{path} : URL en double ({source['url']})")
        seen.add(source['url'])
    return sources

def load_keywords(path=KEYWORDS_FILE):
    """Charge et valide les mots-clés (modifiables depuis le dashboard). Retourne (forts, faibles)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            keywords = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f'{path} illisible ({e})')
    if not isinstance(keywords, dict):
        raise ConfigError(f'{path} : objet {{"strong": {{"fr": [...], ...}}, "weak": {{...}}}} attendu')
    seen = {}
    lists = []
    for kind in ('strong', 'weak'):
        by_language = keywords.get(kind)
        if not isinstance(by_language, dict) or any(
                lang not in KEYWORD_LANGUAGES or not isinstance(terms, list) for lang, terms in by_language.items()):
            raise ConfigError(f'{path} : "{kind}" doit associer une langue ({", ".join(KEYWORD_LANGUAGES)}) à une liste')
        terms = [term for lang_terms in by_language.values() for term in lang_terms]
        if kind == 'strong' and not terms:
            raise ConfigError(f'{path} : au moins un mot-clé fort est nécessaire')
        for term in terms:
            if not isinstance(term, str) or not re.search(r'\w', term) \
                    or not KEYWORD_MIN_CHARS <= len(term.strip()) <= KEYWORD_MAX_CHARS:
                raise ConfigError(f'{path} : mot-clé invalide dans "{kind}" ({term!r})')
            norm = normalize_for_match(term)
            if norm in seen:
                raise ConfigError(f'{path} : mot-clé en double ({term!r}, déjà dans "{seen[norm]}")')
            seen[norm] = kind
        lists.append([t.strip() for t in terms])
    return lists[0], lists[1]

def normalize_for_match(text):
    """Minuscules, sans accents/diacritiques, apostrophes et espaces unifiés."""
    if not text:
        return ''
    text = text.replace('’', "'").replace('‘', "'").replace(' ', ' ')
    decomposed = unicodedata.normalize('NFKD', text.casefold())
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return ' '.join(stripped.split())

ARABIC_CHARS = re.compile(r'[؀-ۿ]')

def compile_terms(terms):
    """Regex sur mots entiers ; renvoie (pattern, {forme normalisée: terme original})."""
    lookup = {}
    for term in terms:
        lookup.setdefault(normalize_for_match(term), term)
    alternatives = []
    for norm in sorted(lookup, key=len, reverse=True):
        body = r'\s+'.join(re.escape(part) for part in norm.split(' '))
        if ARABIC_CHARS.search(norm):
            # Proclitiques arabes collés au mot (wa-, fa-, bi-, li-, ka-)
            body = r'[وفبلك]?' + body
        alternatives.append(body)
    pattern = re.compile(r'(?<!\w)(' + '|'.join(alternatives) + r')(?!\w)')
    return pattern, lookup

def set_keywords(strong, weak):
    global STRONG_KEYWORDS, WEAK_KEYWORDS, TARGET_KEYWORDS, STRONG_RE, WEAK_RE
    STRONG_KEYWORDS, WEAK_KEYWORDS = list(strong), list(weak)
    TARGET_KEYWORDS = STRONG_KEYWORDS + WEAK_KEYWORDS
    STRONG_RE = compile_terms(STRONG_KEYWORDS)
    # Liste faible vide : motif qui ne correspond à rien
    WEAK_RE = compile_terms(WEAK_KEYWORDS) if WEAK_KEYWORDS else (re.compile(r'(?!x)x'), {})

# Fichier invalide : signalé par main() (et par les tests) plutôt qu'à l'import
try:
    set_keywords(*load_keywords())
    KEYWORDS_ERROR = None
except ConfigError as e:
    KEYWORDS_ERROR = e
SERVICE_RE = compile_terms(SERVICE_TERMS)
GOODS_RE = compile_terms(GOODS_WORKS_TERMS)
IGNORED_PHRASES_NORMALIZED = [normalize_for_match(p) for p in IGNORED_PHRASES]

def find_terms(normalized_text, compiled):
    pattern, lookup = compiled
    found = []
    for match in pattern.finditer(normalized_text):
        key = ' '.join(match.group(1).split())
        if key not in lookup and ARABIC_CHARS.search(key):
            key = key[1:]
        term = lookup.get(key)
        if term and term not in found:
            found.append(term)
    return found

def classify_text(text):
    """
    Décide si un bloc de texte est une opportunité pertinente.
    Retourne (mots-clés matchés, score de confiance) ou None.
    """
    norm = normalize_for_match(text)
    for phrase in IGNORED_PHRASES_NORMALIZED:
        norm = norm.replace(phrase, ' ')
    strong = find_terms(norm, STRONG_RE)
    weak = find_terms(norm, WEAK_RE)
    if not strong and not weak:
        return None

    has_service = bool(find_terms(norm, SERVICE_RE))
    has_goods = bool(find_terms(norm, GOODS_RE))

    if has_goods and not has_service:
        return None
    if strong:
        return strong + weak, (90 if has_service else 75)
    if has_service:
        return weak, 60
    return None

# ==================== EXTRACTION DES AVIS ====================

EXCLUDED_TAGS = ['script', 'style', 'noscript', 'template', 'svg', 'head',
                 'header', 'nav', 'footer', 'select', 'button']
EXCLUDED_ROLES = {'navigation', 'banner', 'contentinfo', 'menu', 'menubar', 'search'}
PRIMARY_BLOCK_TAGS = ['tr', 'li', 'article', 'dt', 'dd']
CARD_CLASS_RE = re.compile(
    r'(tender|notice|avis|opportun|offre|appel|march|procurement|result|card|item|listing|post|entry)',
    re.I)
SECONDARY_BLOCK_TAGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'a']
HIDDEN_ATTR = 'data-tt-hidden'

# Marque les éléments non rendus pour les exclure de l'analyse du DOM
MARK_HIDDEN_JS = """
document.querySelectorAll('body *').forEach(function (el) {
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || el.getClientRects().length === 0) {
        el.setAttribute('%s', '1');
    }
});
""" % HIDDEN_ATTR

def _clean_dom(soup):
    for tag in soup(EXCLUDED_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        style = (tag.get('style') or '').lower().replace(' ', '')
        if (tag.has_attr(HIDDEN_ATTR) or tag.has_attr('hidden')
                or tag.get('aria-hidden') == 'true'
                or (tag.get('role') or '').lower() in EXCLUDED_ROLES
                or 'display:none' in style or 'visibility:hidden' in style):
            tag.decompose()

def _block_text(tag):
    if tag.name == 'tr':
        cells = [' '.join(c.get_text(' ', strip=True).split()) for c in tag.find_all(['td', 'th'])]
        return ' | '.join(c for c in cells if c)
    return ' '.join(tag.get_text(' ', strip=True).split())

def _block_link(tag, page_url):
    links = [tag] if tag.name == 'a' else tag.find_all('a', href=True)
    for link in links:
        href = (link.get('href') or '').strip()
        if href and not href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            return urljoin(page_url, href)
    return page_url

def _is_card(tag):
    if tag.name not in ('div', 'section'):
        return False
    marker = ' '.join(tag.get('class') or []) + ' ' + (tag.get('id') or '')
    return bool(CARD_CLASS_RE.search(marker))

def _innermost(matches):
    """Garde les blocs les plus spécifiques : retire ceux qui contiennent un autre match."""
    ids = {id(tag) for tag, _ in matches}
    kept = []
    for tag, text in matches:
        if any(id(desc) in ids for desc in tag.find_all(True)):
            continue
        kept.append((tag, text))
    return kept

def extract_notice_blocks(html, page_url):
    """
    Découpe une page en avis individuels (ligne de tableau, item de liste, carte…)
    contenant des mots-clés pertinents. Retourne [(texte, lien, mots-clés, confiance)].
    """
    soup = BeautifulSoup(html, 'html.parser')
    _clean_dom(soup)
    body = soup.body or soup

    def matching(tags):
        out = []
        for tag in tags:
            text = _block_text(tag)
            if MIN_BLOCK_CHARS <= len(text) <= MAX_BLOCK_CHARS and classify_text(text):
                out.append((tag, text))
        return out

    primary = _innermost(matching(body.find_all(PRIMARY_BLOCK_TAGS) + body.find_all(_is_card)))
    primary_ids = {id(tag) for tag, _ in primary}

    def inside_primary(tag):
        return any(id(parent) in primary_ids for parent in tag.parents)

    secondary = _innermost([
        (tag, text) for tag, text in matching(body.find_all(SECONDARY_BLOCK_TAGS))
        if not inside_primary(tag)
    ])

    results = []
    seen = set()
    for tag, text in primary + secondary:
        key = normalize_for_match(text)
        if key in seen:
            continue
        seen.add(key)
        keywords, confidence = classify_text(text)
        results.append((text, _block_link(tag, page_url), keywords, confidence))
    return results

# ==================== ÉTAT DES SOURCES ====================

SOURCE_STATE_LABELS = {
    'ok': 'OK',
    'empty': 'Page vide',
    'blocked': 'Bloqué',
    'not_found': 'Introuvable',
    'timeout': 'Timeout',
    'unreachable': 'Injoignable',
    'error': 'Erreur',
}
MIN_PAGE_CHARS = 300
# Alerte webhook au N-ième échec consécutif d'une source (une seule fois par panne),
# même seuil que PERSISTENT_FAILURE_RUNS du dashboard
SOURCE_ALERT_FAILURES = 3
BLOCKED_TITLE_RE = re.compile(
    r'just a moment|un instant|attention required|access denied|acces refuse|request rejected|'
    r'\b403\b|forbidden|verify you are human|checking your browser|ddos|captcha|security check')
BLOCKED_BODY_RE = re.compile(
    r'verify you are human|checking your browser|enable javascript and cookies|'
    r'access denied|request rejected|you have been blocked|captcha|cf-chl|ddos protection|'
    r'developer tools are not permitted')
CHROME_NET_ERROR_RE = re.compile(r'(ERR_[A-Z0-9_]{4,})')
NOT_FOUND_RE = re.compile(
    r'\b404\b|page not found|not found|page introuvable|introuvable|'
    r'no encontrada|nao encontrada|page does not exist|n\'existe pas')

def assess_page(html):
    """Diagnostique une page chargée : (état, détail, nb de caractères visibles)."""
    soup = BeautifulSoup(html, 'html.parser')
    title = normalize_for_match(soup.title.get_text(' ') if soup.title else '')
    for tag in soup(['script', 'style', 'noscript', 'template', 'head']):
        tag.decompose()
    for tag in soup.select(f'[{HIDDEN_ATTR}]'):
        tag.decompose()
    text = normalize_for_match(soup.get_text(' '))
    chars = len(text)

    # Page d'erreur réseau de Chrome (servie sans exception Selenium)
    chrome_error = CHROME_NET_ERROR_RE.search(html)
    if chars < 1500 and 'neterror' in html and chrome_error:
        return 'unreachable', 'net::' + chrome_error.group(1), chars
    if BLOCKED_TITLE_RE.search(title) or (chars < 1500 and (
            BLOCKED_BODY_RE.search(text) or 'challenges.cloudflare.com' in html)):
        return 'blocked', f'Protection anti-robot ({title[:60] or "sans titre"})', chars
    if NOT_FOUND_RE.search(title) or (chars < 1500 and NOT_FOUND_RE.search(text[:300])):
        return 'not_found', f'Page introuvable ({title[:60] or "sans titre"})', chars
    if chars < MIN_PAGE_CHARS:
        return 'empty', f'{chars} caractères visibles', chars
    return 'ok', '', chars

def source_status_entry(state, detail, started, text_chars=0, notices=None):
    return {
        'state': state,
        'label': SOURCE_STATE_LABELS[state],
        'detail': detail,
        'duration_s': round(time.monotonic() - started, 1),
        'text_chars': text_chars,
        'notices': notices,
    }

def update_source_status(previous_status, run_status, sources, now=None):
    """Ajoute date du scan, dernier succès, début de la panne et nombre d'échecs consécutifs."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    updated = {}
    for source in sources:
        url = source.get('url')
        if url not in run_status:
            continue
        before = previous_status.get(url, {})
        entry = {'country': source.get('country'), **run_status[url], 'checked_at': now_iso}
        if entry['state'] == 'ok':
            entry['last_ok'] = now_iso
            entry['consecutive_failures'] = 0
            entry['failing_since'] = None
        else:
            entry['last_ok'] = before.get('last_ok')
            entry['consecutive_failures'] = before.get('consecutive_failures', 0) + 1
            if before.get('consecutive_failures'):
                # Statuts antérieurs à ce champ : la panne date au plus tard du scan précédent
                entry['failing_since'] = before.get('failing_since') or before.get('checked_at') or now_iso
            else:
                entry['failing_since'] = now_iso
        updated[url] = entry
    return updated

def make_tender_id(source_url, title):
    return hashlib.sha1(f"{source_url}|{normalize_for_match(title)}".encode('utf-8')).hexdigest()

def extract_tenders(html, country, source_url):
    tenders = []
    detected_at = datetime.now(timezone.utc).isoformat()
    for text, link, keywords, confidence in extract_notice_blocks(html, source_url)[:MAX_TENDERS_PER_SOURCE]:
        title = text if len(text) <= 300 else text[:297] + '...'
        tenders.append({
            'id': make_tender_id(source_url, text),
            'country': country,
            'title': title,
            'url': link,
            'source_url': source_url,
            'detected_at': detected_at,
            'matched_keywords': keywords[:5],
            'confidence': confidence,
        })
    return tenders

# ==================== RÉCUPÉRATION DES PAGES ====================

PAGE_LOAD_TIMEOUT = 45
CONTENT_WAIT_MAX = 20
FETCH_ATTEMPTS = 2
RETRY_DELAY = 5
RETRYABLE_STATES = {'timeout', 'unreachable', 'error'}
MAX_PAGES_PER_SOURCE = 3

NEXT_WORDS = (r'next|next page|suivant|suivante|page suivante|siguiente|pagina siguiente|'
              r'proximo|proxima|seguinte')
NEXT_TEXT_RE = re.compile(r'^(?:(?:' + NEXT_WORDS + r')\s*[›»>→]*|[›>→])$')
NEXT_ATTR_RE = re.compile(r'\b(?:' + NEXT_WORDS + r')\b')

_chromedriver_lock = threading.Lock()
_chromedriver_path = None

def chromedriver_path():
    """Résout le chromedriver une seule fois pour tous les threads."""
    global _chromedriver_path
    with _chromedriver_lock:
        if _chromedriver_path is None:
            try:
                _chromedriver_path = ChromeDriverManager().install()
            except Exception:
                _chromedriver_path = ''
    return _chromedriver_path or None

def new_chrome_driver():
    options = Options()
    for arg in ('--headless', '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-gpu', '--window-size=1920,1080', '--disable-extensions'):
        options.add_argument(arg)
    # N'attend pas images/traceurs : le DOM suffit, le contenu est attendu ensuite
    options.page_load_strategy = 'eager'
    path = chromedriver_path()
    driver = webdriver.Chrome(service=Service(path), options=options) if path \
        else webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(30)
    # Vraie version du navigateur, sans la mention « Headless » repérée par les pare-feux
    user_agent = driver.execute_script('return navigator.userAgent').replace('HeadlessChrome', 'Chrome')
    driver.execute_cdp_cmd('Network.setUserAgentOverride', {'userAgent': user_agent})
    return driver

def wait_for_content(driver):
    """Attend que le texte de la page cesse d'évoluer (chargements AJAX)."""
    deadline = time.monotonic() + CONTENT_WAIT_MAX
    time.sleep(2)
    last, stable = -1, 0
    while time.monotonic() < deadline:
        try:
            length = driver.execute_script('return document.body ? document.body.innerText.length : 0')
        except WebDriverException:
            return
        if length == last and length >= MIN_PAGE_CHARS:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = length
        time.sleep(1)

class SeleniumSession:
    """Un navigateur par source, réutilisé pour les retries et la pagination."""

    def __init__(self):
        self.driver = None

    def fetch(self, url):
        try:
            if self.driver is None:
                self.driver = new_chrome_driver()
        except Exception as e:
            return 'error', f'Lancement navigateur: {type(e).__name__}', '', 0
        try:
            self.driver.get(url)
            wait_for_content(self.driver)
            self.driver.execute_script(MARK_HIDDEN_JS)
            html = self.driver.page_source
        except TimeoutException:
            self.close()
            return 'timeout', f'Chargement > {PAGE_LOAD_TIMEOUT} s', '', 0
        except WebDriverException as e:
            self.close()
            network_error = re.search(r'net::ERR_[A-Z0-9_]+', str(e))
            if network_error:
                return 'unreachable', network_error.group(0), '', 0
            return 'error', type(e).__name__, '', 0
        state, detail, chars = assess_page(html)
        return state, detail, html, chars

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.driver = None

def requests_fetch(url):
    """Récupération HTML simple, sans exécution JavaScript."""
    try:
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError:
            # Certificats invalides fréquents sur les portails publics ; lecture seule de données publiques
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
    except requests.exceptions.Timeout:
        return 'timeout', f'Pas de réponse en {REQUEST_TIMEOUT} s', '', 0
    except requests.exceptions.ConnectionError as e:
        return 'unreachable', type(e).__name__, '', 0
    except requests.exceptions.RequestException as e:
        return 'error', type(e).__name__, '', 0

    if response.status_code == 404:
        return 'not_found', 'HTTP 404', '', 0
    if response.status_code in (401, 403, 429):
        return 'blocked', f'HTTP {response.status_code}', '', 0
    if response.status_code >= 400:
        return 'error', f'HTTP {response.status_code}', '', 0
    if not response.encoding or response.encoding.lower() == 'iso-8859-1':
        response.encoding = response.apparent_encoding
    state, detail, chars = assess_page(response.text)
    return state, detail, response.text, chars

def fetch_with_retry(fetch, url):
    for attempt in range(FETCH_ATTEMPTS):
        result = fetch(url)
        if result[0] not in RETRYABLE_STATES or attempt == FETCH_ATTEMPTS - 1:
            return result
        time.sleep(RETRY_DELAY * (attempt + 1))

def find_next_page_url(html, page_url):
    """Lien « page suivante » (même site), ou None."""
    soup = BeautifulSoup(html, 'html.parser')
    host = urlparse(page_url).netloc
    current = urldefrag(page_url)[0]
    # <link rel="next"> (en-tête) sert aussi à la navigation entre sections : ignoré
    candidates = soup.select('a[rel~=next][href]')
    for link in soup.find_all('a', href=True):
        text = normalize_for_match(link.get_text(' '))
        parent_classes = ' '.join(link.parent.get('class') or []) if link.parent else ''
        if 'disabled' in parent_classes or link.get('aria-disabled') == 'true':
            continue
        label = normalize_for_match(' '.join([
            link.get('aria-label') or '', link.get('title') or '',
            ' '.join(link.get('class') or []), parent_classes]))
        if NEXT_TEXT_RE.match(text) or (len(text) <= 20 and NEXT_ATTR_RE.search(label)):
            candidates.append(link)
    for link in candidates:
        href = (link.get('href') or '').strip()
        if not href or href.startswith(('#', 'javascript:')):
            continue
        absolute = urldefrag(urljoin(page_url, href))[0]
        parsed = urlparse(absolute)
        # Une page de liste suivante porte un numéro (?page=2, /page/2/, ?p=2...)
        if parsed.netloc == host and absolute != current and re.search(r'\d', parsed.path + parsed.query):
            return absolute
    return None

def scrape_portal(country, url, fetchers=None):
    """
    Analyse une source : Selenium puis repli sur requests, avec retries et pagination.
    Retourne (avis ou None si la source n'a pas pu être analysée, statut de la source).
    """
    started = time.monotonic()
    session = None
    if fetchers is None:
        fetchers = []
        if SELENIUM_AVAILABLE:
            session = SeleniumSession()
            fetchers.append(('selenium', session.fetch))
        fetchers.append(('requests', requests_fetch))

    try:
        attempts = []
        primary_state = None
        for method, fetch in fetchers:
            state, detail, html, chars = fetch_with_retry(fetch, url)
            if state == 'ok':
                break
            primary_state = primary_state or state
            attempts.append(f'{method}: {SOURCE_STATE_LABELS[state]}' + (f' ({detail})' if detail else ''))

        if state != 'ok':
            # L'état de la méthode principale (navigateur) est le plus représentatif
            print(f"   {country}: ⚠️ {SOURCE_STATE_LABELS[primary_state]} - {' | '.join(attempts)}")
            return None, source_status_entry(primary_state, ' | '.join(attempts), started, chars)

        tenders = {t['id']: t for t in extract_tenders(html, country, url)}
        pages, complete, visited = 1, True, {url}
        next_url = find_next_page_url(html, url)
        while next_url and next_url not in visited and pages < MAX_PAGES_PER_SOURCE:
            visited.add(next_url)
            page_state, _, page_html, _ = fetch_with_retry(fetch, next_url)
            if page_state != 'ok':
                complete = False
                break
            pages += 1
            for tender in extract_tenders(page_html, country, url):
                tenders.setdefault(tender['id'], tender)
            next_url = find_next_page_url(page_html, next_url)
    finally:
        if session:
            session.close()

    found = list(tenders.values())[:MAX_TENDERS_PER_SOURCE]
    notes = []
    if attempts:
        notes.append(f"via {method} après échec ({' | '.join(attempts)})")
    if not complete:
        notes.append(f'pagination interrompue après {pages} page(s)')
    status = source_status_entry('ok', ' ; '.join(notes), started, chars, len(found))
    status.update({'method': method, 'pages': pages, 'complete': complete})

    pages_info = f' ({pages} pages)' if pages > 1 else ''
    if found:
        keywords_found = ', '.join(found[0]['matched_keywords'][:3])
        print(f"   {country}: ✓ {len(found)} avis{pages_info} - {keywords_found}")
    else:
        print(f"   {country}: −{pages_info}" + (f" [via {method}]" if attempts else ''))
    return found, status

def merge_history(previous_data, scan_results, now=None, retention_days=HISTORY_RETENTION_DAYS,
                  partial_sources=frozenset()):
    """
    Fusionne les résultats du run avec l'historique cumulatif.

    scan_results: {source_url: [avis] ou None si la source a échoué}
    partial_sources: sources lues partiellement (pagination interrompue) : rien n'y expire
    Retourne (avis actifs pour le dashboard, historique, nouveaux avis à notifier).
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    history = {entry['id']: dict(entry) for entry in previous_data.get('history', [])}
    displayed_ids = {t.get('id') for t in previous_data.get('tenders', [])}

    # Un avis actif retiré de `tenders` depuis le dashboard a été écarté par l'utilisateur
    for entry in history.values():
        if entry['status'] == 'active' and entry['id'] not in displayed_ids:
            entry['status'] = 'dismissed'

    new_tenders = []
    for source_url, found in scan_results.items():
        if found is None:
            continue
        found_ids = set()
        for tender in found:
            found_ids.add(tender['id'])
            entry = history.get(tender['id'])
            if entry is None:
                entry = {**tender, 'first_seen': now_iso, 'detected_at': now_iso, 'status': 'active'}
                history[tender['id']] = entry
                new_tenders.append(entry)
            else:
                for field in ('title', 'url', 'matched_keywords', 'confidence', 'country'):
                    entry[field] = tender[field]
                if entry['status'] == 'expired':
                    entry['status'] = 'active'
            entry['last_seen'] = now_iso
        if source_url in partial_sources:
            continue
        for entry in history.values():
            if entry.get('source_url') == source_url and entry['id'] not in found_ids \
                    and entry['status'] == 'active':
                entry['status'] = 'expired'

    # Sources retirées de la configuration : leurs avis ne seront plus revus
    for entry in history.values():
        if entry.get('source_url') not in scan_results and entry['status'] == 'active':
            entry['status'] = 'expired'

    cutoff = now - timedelta(days=retention_days)
    history = {
        tid: entry for tid, entry in history.items()
        if entry['status'] == 'active' or datetime.fromisoformat(entry['last_seen']) >= cutoff
    }

    ordered = sorted(history.values(), key=lambda e: e['first_seen'], reverse=True)
    active = [e for e in ordered if e['status'] == 'active']
    return active, ordered, new_tenders

def send_webhook_notification(new_tenders):
    """
    Envoie une notification Webhook pour les nouvelles opportunités
    Supporte Discord, Slack, et Telegram
    """
    webhook_url = os.getenv('WEBHOOK_URL')
    notification_enabled = os.getenv('NOTIFICATION_ENABLED', 'false').lower() == 'true'

    if not webhook_url or not notification_enabled or not new_tenders:
        return

    detected = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    # Discord limite à 10 embeds par message
    for start in range(0, len(new_tenders), 10):
        batch = new_tenders[start:start + 10]
        payload = {
            'content': f"**{len(new_tenders)} NOUVELLE(S) OPPORTUNITÉ(S) DÉTECTÉE(S)**" if start == 0 else '',
            'embeds': [
                {
                    'title': f"{tender['country']} - {tender['title'][:200]}",
                    'description': f"URL: {tender['url']}\n\nMots-clés: {', '.join(tender.get('matched_keywords', [])[:3])}",
                    'color': 0,
                    'footer': {'text': f"Détecté: {detected} UTC"}
                }
                for tender in batch
            ]
        }
        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
            if response.status_code in [200, 204]:
                print(f"📬 {len(batch)} notification(s) envoyée(s)")
            else:
                print(f"⚠️  Webhook retourné: {response.status_code}")
        except Exception as e:
            print(f"❌ Erreur Webhook: {e}")

def sources_to_alert(source_status, threshold=SOURCE_ALERT_FAILURES):
    """Sources qui viennent d'atteindre exactement `threshold` échecs consécutifs."""
    return [e for e in source_status.values()
            if e.get('state') != 'ok' and e.get('consecutive_failures') == threshold]

def send_source_failure_alert(failing):
    """Signale sur le webhook les sources en échec persistant (même si les notifications d'avis sont coupées)."""
    webhook_url = os.getenv('WEBHOOK_URL')
    if not webhook_url or not failing:
        return
    lines = [f"- {e.get('country')} : {e.get('label', e.get('state'))}"
             + (f" ({e['detail'][:150]})" if e.get('detail') else '')
             for e in failing]
    content = (f"**⚠️ {len(failing)} SOURCE(S) EN ÉCHEC DEPUIS {SOURCE_ALERT_FAILURES} RUNS**\n"
               + '\n'.join(lines))
    try:
        # Discord limite le message à 2000 caractères
        response = requests.post(webhook_url, json={'content': content[:2000]}, timeout=10)
        if response.status_code in [200, 204]:
            print(f"📬 Alerte envoyée pour {len(failing)} source(s) en échec")
        else:
            print(f"⚠️  Webhook retourné: {response.status_code}")
    except Exception as e:
        print(f"❌ Erreur Webhook: {e}")

def save_data(data, path=DATA_FILE, password=None):
    """Écriture chiffrée et atomique : un run interrompu ne laisse jamais un data.json tronqué."""
    envelope = encrypt_data(data, data_password(password))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(envelope, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    print(f"💾 Données sauvegardées: {len(data['tenders'])} détection(s)")

# ==================== MAIN ====================

def main():
    print("=" * 70)
    print("TENDER-TRACKER SAGA - Scraper Multilingue Automatisé")
    print("=" * 70)

    try:
        if KEYWORDS_ERROR:
            raise KEYWORDS_ERROR
        data_password()
        sources = load_sources()
        data = load_data()
    except ConfigError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    previous_tenders = data.get('tenders', [])

    print("\nConfiguration:")
    print(f"  Sources: {len(sources)}")
    print(f"  Tenders antérieurs: {len(previous_tenders)}")
    print(f"  Mots-clés détection: {len(TARGET_KEYWORDS)} (FR, EN, ES, PT, AR)")
    print(f"  Selenium disponible: {'✅ OUI' if SELENIUM_AVAILABLE else '❌ NON'}")

    # Scraper TOUS les portails EN PARALLÈLE
    scan_results = {}
    run_status = {}
    print("\nScraping en parallèle (10 workers, texte visible UNIQUEMENT):")
    print("-" * 70)

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_source = {
            executor.submit(scrape_portal, source.get('country'), source.get('url')): source
            for source in sources
        }
        for future in as_completed(future_to_source):
            source = future_to_source[future]
            url = source.get('url')
            try:
                scan_results[url], run_status[url] = future.result()
            except Exception as e:
                print(f"   {source.get('country')}: ❌ Exception - {type(e).__name__}")
                scan_results[url] = None
                run_status[url] = source_status_entry('error', type(e).__name__, time.monotonic())

    partial = {url for url, status in run_status.items() if status.get('complete') is False}
    active_tenders, history, new_tenders = merge_history(data, scan_results, partial_sources=partial)
    source_status = update_source_status(data.get('source_status', {}), run_status, sources)
    failed = sum(1 for found in scan_results.values() if found is None)

    print("-" * 70)
    print("\nRÉSULTATS:")
    print(f"  Sources analysées: {len(scan_results) - failed} / {len(scan_results)} ({failed} en échec, avis conservés)")
    print(f"  Avis actifs: {len(active_tenders)}")
    print(f"  Nouvelles opportunités: {len(new_tenders)}")
    print(f"  Historique: {len(history)} avis")
    print(f"  Portails avec détections: {len(set(t['country'] for t in active_tenders))}")

    print("\nÉTAT DES SOURCES:")
    for state, label in SOURCE_STATE_LABELS.items():
        entries = [e for e in source_status.values() if e['state'] == state]
        if not entries:
            continue
        print(f"  {label}: {len(entries)}")
        for e in sorted(entries, key=lambda e: -e['consecutive_failures']):
            if state == 'ok':
                if e['detail'] or e.get('pages', 1) > 1:
                    print(f"     - {e['country']}: {e.get('pages', 1)} page(s) {e['detail']}")
            else:
                print(f"     - {e['country']}: {e['detail']} ({e['consecutive_failures']} échec(s) consécutif(s))")

    # Envoyer notifications
    if new_tenders:
        print("\nEnvoi des notifications...")
        send_webhook_notification(new_tenders)
    send_source_failure_alert(sources_to_alert(source_status))

    # Les sources sont recopiées pour le dashboard, qui ne lit que data.json
    updated_data = {
        'sources': sources,
        'tenders': active_tenders,
        'history': history,
        'source_status': source_status,
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'new_tenders_count': len(new_tenders)
    }

    save_data(updated_data)
    print("\n" + "=" * 70)
    print("Scraping terminé avec succès")
    print("=" * 70)

if __name__ == '__main__':
    main()
