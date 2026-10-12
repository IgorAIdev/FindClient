#!/usr/bin/env python3
"""FindClient: public company discovery and contact collection. Python 3.10+."""
import argparse
import csv
import html
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
import zlib
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlencode, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser

AGENT = 'FindClient/0.1'
STATUSES = ('new', 'called', 'written', 'replied', 'interested', 'closed', 'do_not_contact')

def now():
    return datetime.now(timezone.utc).isoformat()

def domain(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')

def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Only public HTTP(S) URLs are supported')
    if p.port not in (None, 80, 443):
        raise ValueError('Nonstandard ports are not supported')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Private/local addresses are blocked')
    return url

class Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        if domain(req.full_url) != domain(newurl):
            raise ValueError('Cross-domain redirect: review URL manually')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(url, headers=None):
    public_url(url)
    req = Request(url, headers={'User-Agent': AGENT, **(headers or {})})
    with build_opener(Redirect()).open(req, timeout=20) as response:
        data = response.read(2_000_001)
        if data[:2] == b'\x1f\x8b':  # some servers send gzip regardless of Accept-Encoding
            decoder = zlib.decompressobj(31)
            try:
                data = decoder.decompress(data, 2_000_001)
            except zlib.error:
                raise ValueError('Invalid gzip response')
            if decoder.unconsumed_tail:
                raise ValueError('Page exceeds 2 MB')
        if len(data) > 2_000_000:
            raise ValueError('Page exceeds 2 MB')
        return data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')

class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.links, self.parts, self.titles = [], [], []
        self.hidden = 0
        self.in_title = False
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.titles.append(data)

    def contacts(self):
        text = ' '.join(self.parts)
        found = set()
        for address in re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text):
            found.add(('email', address.lower()))
        for link in self.links:
            if link.lower().startswith('mailto:'):
                for address in unquote(link[7:].split('?')[0]).split(','):
                    if re.fullmatch(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', address.strip()):
                        found.add(('email', address.strip().lower()))
            if link.lower().startswith('tel:'):
                phone = re.sub(r'[^\d+]', '', unquote(link[4:]).split(';')[0])
                if 9 <= len(re.sub(r'\D', '', phone)) <= 15:
                    found.add(('phone', phone))
            if urlsplit(link).hostname in ('t.me', 'telegram.me', 'wa.me'):
                found.add(('messenger', link))
            if link.startswith('viber://'):
                found.add(('messenger', link))
        # Conservative international/Ukraine extraction; visible numbers need review.
        for phone in re.findall(r'(?<!\w)(?:\+\d[\d ()-]{8,22}\d|0\d{2}[ ()-]+\d{3}[ -]\d{2}[ -]\d{2})(?!\w)', text):
            normalized = re.sub(r'[^\d+]', '', phone)
            digits = re.sub(r'\D', '', normalized)
            if 10 <= len(digits) <= 15:
                if len(digits) == 10 and digits.startswith('0'):
                    normalized = '+38' + digits
                found.add(('phone', normalized))
        return found

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS companies (
      id INTEGER PRIMARY KEY, domain TEXT UNIQUE, name TEXT, url TEXT,
      campaign TEXT, status TEXT DEFAULT 'new', notes TEXT DEFAULT '',
      last_checked TEXT, error TEXT, role TEXT DEFAULT 'unverified');
    CREATE TABLE IF NOT EXISTS contacts (
      company_id INTEGER REFERENCES companies(id), kind TEXT, value TEXT,
      source TEXT, checked_at TEXT, UNIQUE(company_id,kind,value,source));
    CREATE TABLE IF NOT EXISTS discoveries (
      company_id INTEGER REFERENCES companies(id), source TEXT,
      found_at TEXT, UNIQUE(company_id,source));
    CREATE TABLE IF NOT EXISTS audits (
      company_id INTEGER PRIMARY KEY REFERENCES companies(id), checked_at TEXT,
      score INTEGER, offer TEXT, summary TEXT, details TEXT, error TEXT);
    ''')
    have = {row[1] for row in db.execute('PRAGMA table_info(companies)')}
    for column in ('city', 'site_kind', 'prom_url'):
        if column not in have:
            db.execute('ALTER TABLE companies ADD COLUMN ' + column + ' TEXT')
    return db

def add(db, url, name, campaign, source, key=None):
    if urlsplit(url).scheme not in ('http', 'https') or not domain(url):
        raise ValueError('Invalid company URL')
    key = key or domain(url)
    db.execute('INSERT OR IGNORE INTO companies(domain,name,url,campaign) VALUES(?,?,?,?)',
               (key, name or key, url, campaign))
    ident = db.execute('SELECT id FROM companies WHERE domain=?', (key,)).fetchone()[0]
    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)', (ident, source, now()))
    db.commit()
    return ident

def crawl(db, company, max_pages=4, delay=2):
    origin = '{0.scheme}://{0.netloc}'.format(urlsplit(company['url']))
    try:
        robot = load_robots(origin)
        queue, visited = [company['url']], set()
        errors, succeeded = [], 0
        while queue and len(visited) < max_pages:
            url = queue.pop(0).split('#')[0]
            if url in visited:
                continue
            visited.add(url)
            if not robot.can_fetch(AGENT, url):
                errors.append('robots.txt disallows ' + url)
                continue
            time.sleep(max(delay, robot.crawl_delay(AGENT) or 0))
            try:
                page = Page(fetch(url))
            except (OSError, ValueError) as e:
                errors.append(type(e).__name__ + ': ' + url)
                continue
            succeeded += 1
            for kind, value in page.contacts():
                db.execute('''INSERT INTO contacts VALUES(?,?,?,?,?)
                  ON CONFLICT(company_id,kind,value,source) DO UPDATE SET checked_at=excluded.checked_at''',
                           (company['id'], kind, value, url, now()))
            for link in page.links:
                target = urljoin(url, link)
                if (domain(target) == company['domain'] and urlsplit(target).scheme in ('http','https')
                        and re.search(r'contact|kontakt|kontakti|kontakty|контакт|about|pro-nas', unquote(target), re.I)):
                    if target not in visited and target not in queue:
                        queue.append(target)
        if not succeeded and not errors:
            errors.append('No pages fetched')
        db.execute('UPDATE companies SET last_checked=?,error=? WHERE id=?',
                   (now(), '; '.join(errors) or None, company['id']))
    except (OSError, ValueError) as e:
        db.execute('UPDATE companies SET error=? WHERE id=?', (type(e).__name__ + ': crawl failed', company['id']))
    db.commit()

PROM_HOSTS = ('prom.ua', 'www.prom.ua')
PROM_PROFILE = re.compile(r'^/c(\d+)-[\w%-]+\.html$')
PROM_CATEGORY = re.compile(r'^/[A-Za-z0-9_-]+\.html$')
SOCIAL_HOSTS = ('facebook.com', 'instagram.com', 't.me', 'olx.ua', 'tiktok.com', 'youtube.com', 'linktr.ee', 'vk.com')
FREE_HOSTS = ('prom.ua', 'business.site', 'wixsite.com', 'tilda.ws', 'blogspot.com', 'jimdosite.com',
              'webnode.page', 'weebly.com', 'github.io', 'carrd.co')
WEAK_SITES = ('none', 'prom_hosted', 'social', 'free_host')
PROM_PLATFORM_RE = re.compile(r'(?:src|href)=["\'][^"\']*/cloud-cgi/static/(?:uaprom|vader)-static/', re.I)
SITE_LABELS = {'own': 'свой сайт', 'prom_platform': 'сайт на конструкторе prom.ua (свой домен)', 'none': 'сайта нет', 'prom_hosted': 'только магазин на prom.ua',
               'social': 'только соцсеть/площадка', 'free_host': 'бесплатный конструктор'}

def host_in(host, names):
    return any(host == n or host.endswith('.' + n) for n in names)

def classify_site(site):
    """own | prom_hosted | social | free_host | none. Weak kinds are candidates for a new site."""
    p = urlsplit(site or '')
    host = domain(site or '')
    if p.scheme not in ('http', 'https') or not host:
        return 'none'
    if host_in(host, ('prom.ua',)):
        return 'prom_hosted'
    if host_in(host, SOCIAL_HOSTS):
        return 'social'
    if host_in(host, FREE_HOSTS):
        return 'free_host'
    return 'own'

def prom_url_kind(url):
    """Only whole-page prom.ua profile/category URLs without query strings are fetched.
    This is stricter than prom.ua robots.txt (which stdlib cannot evaluate: wildcards)."""
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in PROM_HOSTS or p.query or p.port not in (None, 443):
        return None
    if PROM_PROFILE.match(p.path):
        return 'profile'
    if PROM_CATEGORY.match(p.path):
        return 'category'
    return None

def prom_company_links(raw, limit=30):
    seen = []
    for path in re.findall(r'href="(/c\d+-[\w%-]+\.html)"', raw):
        link = 'https://prom.ua' + path
        if link not in seen:
            seen.append(link)
    return seen[:limit]

def prom_company(raw, url):
    """Public profile data from the page's own schema.org block and embedded state."""
    info = {'id': PROM_PROFILE.match(urlsplit(url).path).group(1), 'name': '', 'city': '', 'phone': '', 'email': '', 'site': ''}
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('@type') == 'Organization':
            address = data.get('address') if isinstance(data.get('address'), dict) else {}
            info.update(name=str(data.get('name') or ''), city=str(address.get('addressLocality') or ''),
                        phone=str(data.get('telephone') or ''), email=str(data.get('email') or ''))
            break
    match = re.search(r'"webSiteUrl":"((?:[^"\\]|\\.)*)"', raw)
    if match:
        try:
            info['site'] = json.loads('"' + match.group(1) + '"')
        except ValueError:
            pass
    if not info['name']:
        raise ValueError('Not a prom.ua company profile: ' + url)
    return info

def ingest_prom(db, raw, url, campaign, source):
    info = prom_company(raw, url)
    kind = classify_site(info['site'])
    own = kind == 'own'
    ident = add(db, info['site'] if own else url, info['name'], campaign, source,
                None if own else 'prom.ua/c' + info['id'])
    db.execute('UPDATE companies SET city=?,site_kind=?,prom_url=? WHERE id=?',
               (info['city'] or None, kind, url, ident))
    contacts = []
    digits = re.sub(r'\D', '', info['phone'])
    if 10 <= len(digits) <= 15:
        contacts.append(('phone', '+' + digits if info['phone'].lstrip().startswith('+') else '+38' + digits[-10:]))
    if re.fullmatch(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', info['email'].strip()):
        contacts.append(('email', info['email'].strip().lower()))
    if kind == 'social':
        contacts.append(('messenger', info['site']))
    for k, value in contacts:
        db.execute('''INSERT INTO contacts VALUES(?,?,?,?,?)
          ON CONFLICT(company_id,kind,value,source) DO UPDATE SET checked_at=excluded.checked_at''',
                   (ident, k, value, url, now()))
    db.commit()
    return ident, kind

def prom(db, targets, campaign, limit=30, delay=2):
    """Collect companies from prom.ua category pages or single company profiles."""
    for target in targets:
        kind = prom_url_kind(target)
        if not kind:
            raise ValueError('Unsupported prom.ua URL (need https://prom.ua/<category>.html or /c<id>-<name>.html): ' + target)
        time.sleep(delay)
        profiles = prom_company_links(fetch(target), limit) if kind == 'category' else [target]
        for profile in profiles:
            time.sleep(delay)
            try:
                ident, site_kind = ingest_prom(db, fetch(profile), profile, campaign, 'prom: ' + target)
            except (OSError, ValueError) as e:
                print('Skipped', profile, type(e).__name__, flush=True)
                continue
            print('prom.ua', profile, '->', SITE_LABELS[site_kind], flush=True)

class SiteParser(Page):
    def __init__(self, raw):
        self.meta, self.anchors = {}, []
        self.h1 = self.h2 = self.images = self.images_alt = 0
        self.lang = self.canonical = self.jsonld = self.legacy = False
        self._anchor = None
        super().__init__(raw)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        a = {k: (v or '') for k, v in attrs}
        if tag == 'html' and a.get('lang'):
            self.lang = True
        elif tag == 'meta':
            key = (a.get('name') or a.get('property') or '').lower()
            if key:
                self.meta[key] = a.get('content', '')
        elif tag == 'link' and 'canonical' in a.get('rel', '').lower():
            self.canonical = True
        elif tag == 'script' and 'ld+json' in a.get('type', '').lower():
            self.jsonld = True
        elif tag == 'h1':
            self.h1 += 1
        elif tag == 'h2':
            self.h2 += 1
        elif tag == 'img':
            self.images += 1
            self.images_alt += bool(a.get('alt', '').strip())
        elif tag in ('font', 'center', 'marquee', 'blink'):
            self.legacy = True
        elif tag == 'a':
            self._anchor = [a.get('href', ''), []]

    def handle_data(self, data):
        super().handle_data(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'a' and self._anchor is not None:
            self.anchors.append((self._anchor[0], ' '.join(self._anchor[1])))
            self._anchor = None

BLOG_RE = re.compile(r'blog|news|article|stat[yi]|новин|стат[іт]|блог|корисн|полезн|poleznoe|novosti', re.I)

def score_site(raw, url, elapsed=0.0, sitemap_pages=None, year=None):
    """Heuristic 0-100 score from page HTML. Not a replacement for a real audit."""
    year = year or datetime.now(timezone.utc).year
    page = SiteParser(raw)
    text = ' '.join(page.parts)
    words = len(re.findall(r'\w+', text))
    title = ' '.join(page.titles).strip()
    desc = page.meta.get('description', '').strip()
    kinds = {k for k, _ in page.contacts()}
    years = [int(y) for snippet in re.findall(r'(?:©|copyright)[^\n]{0,40}', text, re.I)
             for y in re.findall(r'(?:19|20)\d\d', snippet)]
    blog = any(BLOG_RE.search(href + ' ' + label) for href, label in page.anchors)
    checks = [
        ('https', 10, url.lower().startswith('https://'), 'Нет HTTPS'),
        ('viewport', 10, 'viewport' in page.meta, 'Нет мобильной адаптации (meta viewport)'),
        ('title', 7, 1 if 15 <= len(title) <= 70 else 0.5 if title else 0, 'Заголовок страницы (title) отсутствует или неудачной длины'),
        ('description', 7, 1 if 50 <= len(desc) <= 170 else 0.5 if desc else 0, 'Нет meta description или неудачной длины'),
        ('h1', 6, 1 if page.h1 == 1 else 0.5 if page.h1 > 1 else 0, 'Нет единственного заголовка H1'),
        ('h2', 4, 1 if page.h2 >= 2 else 0.5 if page.h2 else 0, 'Мало подзаголовков H2: слабая структура текста'),
        ('text', 8, min(1, words / 300), 'Мало текста на главной (%d слов)' % words),
        ('alt', 6, page.images_alt / page.images if page.images else 0.5, 'У картинок нет alt-описаний'),
        ('canonical', 3, page.canonical, 'Нет canonical'),
        ('opengraph', 3, 'og:title' in page.meta or 'og:image' in page.meta, 'Нет Open Graph (превью в мессенджерах)'),
        ('jsonld', 5, page.jsonld, 'Нет микроразметки schema.org'),
        ('lang', 3, page.lang, 'Не указан язык страницы (html lang)'),
        ('phone', 6, 1 if 'phone' in kinds else 0.5 if kinds else 0, 'На главной нет телефона/контактов'),
        ('sitemap', 5, bool(sitemap_pages), 'Не найден sitemap.xml'),
        ('blog', 7, blog, 'Нет раздела блога/новостей/статей'),
        ('year', 4, 0.5 if not years else 1 if max(years) >= year - 1 else 0, 'Устаревший год в подвале (сайт не обновляется)'),
        ('speed', 3, 1 if elapsed <= 2 else 0.5 if elapsed <= 4 else 0, 'Медленная загрузка главной (%.1f с)' % elapsed),
        ('legacy', 3, not page.legacy and not re.search(r'swfobject|\.swf\b|jquery[-.]1\.', raw, re.I), 'Устаревшие технологии (Flash/font/jQuery 1.x)'),
    ]
    points = [(code, weight, weight * float(ok), message) for code, weight, ok, message in checks]
    score = round(sum(p[2] for p in points))
    issues = [m for _, w, got, m in sorted(points, key=lambda p: p[1] - p[2], reverse=True) if got < w]
    return {'score': score, 'issues': issues, 'words': words, 'blog': blog, 'sitemap_pages': sitemap_pages,
            'checks': [{'code': c, 'max': w, 'got': round(g, 1)} for c, w, g, _ in points]}

def recommend(kind, result=None):
    """Return (offer, one-line suggestion in Russian)."""
    if kind in WEAK_SITES:
        return 'site', 'Нет собственного сайта (' + SITE_LABELS[kind] + '): предложить сайт-визитку/каталог.'
    score, top = result['score'], '; '.join(result['issues'][:3])
    if kind == 'prom_platform':
        return 'prom_site', ('Сайт на конструкторе prom.ua (%d/100): шаблонный и привязан к подписке. '
                             'Предложить отдельный сайт или SEO-статьи/блог. Главное: %s.' % (score, top))
    if score < 45:
        return 'redesign', 'Сайт слабый (%d/100): предложить переделку/адаптацию. Главное: %s.' % (score, top)
    thin = result['sitemap_pages'] is not None and result['sitemap_pages'] < 15
    if score < 75:
        return 'seo', 'Сайт рабочий (%d/100): предложить SEO-оптимизацию и статьи. Главное: %s.' % (score, top)
    if not result['blog'] or thin:
        return 'content', 'Основа в порядке (%d/100), но мало контента: предложить SEO-статьи/блог.' % score
    return 'none', 'Сайт в хорошем состоянии (%d/100): низкий приоритет.' % score

def load_robots(origin):
    robot = RobotFileParser(origin + '/robots.txt')
    try:
        robot.parse(fetch(robot.url).splitlines())
    except HTTPError as e:
        if e.code != 404:
            raise
        robot.parse([])
    return robot

def audit(db, company, delay=2):
    """Fetch only the home page and sitemap.xml of an existing site and store a rough quality score."""
    kind = company['site_kind'] or 'own'
    score = details = error = None
    if kind in WEAK_SITES:
        offer, summary = recommend(kind)
    else:
        root = '{0.scheme}://{0.netloc}'.format(urlsplit(company['url']))
        try:
            robot = load_robots(root)
            if not robot.can_fetch(AGENT, root + '/'):
                raise ValueError('robots.txt disallows home page')
            time.sleep(max(delay, robot.crawl_delay(AGENT) or 0))
            started = time.monotonic()
            raw = fetch(root + '/')
            elapsed = time.monotonic() - started
            sitemap = None
            declared = [u for u in robot.site_maps() or [] if domain(u) == domain(root)]
            sitemap_url = declared[0] if declared else root + '/sitemap.xml'
            if robot.can_fetch(AGENT, sitemap_url):
                time.sleep(delay)
                try:
                    xml = fetch(sitemap_url)
                    sitemap = len(re.findall(r'<loc>', xml)) if '<loc>' in xml else None
                    if '<sitemapindex' in xml:
                        sitemap = max(sitemap or 0, 15)
                except (OSError, ValueError):
                    pass
            result = score_site(raw, root + '/', elapsed, sitemap)
            if PROM_PLATFORM_RE.search(raw):
                kind = 'prom_platform'
            db.execute('UPDATE companies SET site_kind=? WHERE id=?', (kind, company['id']))
            offer, summary = recommend(kind, result)
            score, details = result['score'], json.dumps(result, ensure_ascii=False)
        except (OSError, ValueError) as e:
            error = type(e).__name__ + ': ' + str(e)[:150]
            offer, summary = 'unreachable', 'Сайт не открылся автоматически (' + error + '): проверить вручную.'
    db.execute('INSERT OR REPLACE INTO audits VALUES(?,?,?,?,?,?,?)',
               (company['id'], now(), score, offer, summary, details, error))
    db.commit()
    return offer, score

def search(db, config):
    key = os.environ.get('BRAVE_SEARCH_API_KEY')
    if not key:
        raise ValueError('Set BRAVE_SEARCH_API_KEY, or use import with a URL list')
    for query in config['queries']:
        params = urlencode({'q': query, 'count': 20})
        result = json.loads(fetch('https://api.search.brave.com/res/v1/web/search?' + params,
                                 {'X-Subscription-Token': key}))
        for item in result.get('web', {}).get('results', []):
            add(db, item['url'], item.get('title', ''), config['name'], 'search: ' + query)
        time.sleep(1)

def export(db, destination):
    rows = db.execute('''SELECT c.id,c.name,c.domain,c.url,c.city,c.site_kind,c.prom_url,c.campaign,c.role,c.status,c.notes,
       a.score AS site_score,a.offer,a.summary,
       t.kind,t.value,t.source,t.checked_at,c.error FROM companies c
       LEFT JOIN audits a ON a.company_id=c.id
       LEFT JOIN contacts t ON t.company_id=c.id ORDER BY c.id,t.kind,t.value''')
    with open(destination, 'w', newline='', encoding='utf-8-sig') as out:
        writer = csv.writer(out)
        writer.writerow([col[0] for col in rows.description])
        for row in rows:
            writer.writerow(["'" + str(v) if str(v or '').startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])

def report(db, destination):
    cards = []
    for c in db.execute('SELECT * FROM companies ORDER BY id'):
        contacts = db.execute('SELECT DISTINCT kind,value,source FROM contacts WHERE company_id=?', (c['id'],))
        details = ''.join('<li>' + html.escape(t['kind'] + ': ' + t['value']) + ' — <a href="' + html.escape(t['source'], quote=True) + '">источник</a></li>' for t in contacts)
        a = db.execute('SELECT * FROM audits WHERE company_id=?', (c['id'],)).fetchone()
        verdict = '<p><b>' + html.escape(SITE_LABELS.get(c['site_kind'] or 'own', '') + (' · ' + c['city'] if c['city'] else '')) + '</b>' + ('<br>' + html.escape(a['summary']) if a else '') + '</p>'
        cards.append('<article><h2>' + html.escape(c['name']) + '</h2><p>' + html.escape(c['status'] + ' · ' + c['role']) + '</p>' + verdict + '<a href="' + html.escape(c['url'], quote=True) + '">' + html.escape(c['domain']) + '</a><ul>' + details + '</ul><p>' + html.escape(c['notes']) + '</p><small>' + html.escape(c['error'] or '') + '</small></article>')
    Path(destination).write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FindClient</title><style>body{font:16px system-ui;background:#f2f5f8;max-width:1000px;margin:30px auto;padding:16px}article{background:white;padding:20px;margin:16px 0;border-radius:14px}a{color:#1764ad}li{overflow-wrap:anywhere;margin:8px 0}small{color:#9b3535}</style><h1>FindClient · Компании и контакты</h1><p>Публичные контакты. Роль компании и актуальность требуют проверки.</p>' + ''.join(cards) + '</html>', encoding='utf-8')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='data/findclient.sqlite3')
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import'); imp.add_argument('file'); imp.add_argument('--campaign', default='films-ua')
    s = sub.add_parser('search'); s.add_argument('config')
    c = sub.add_parser('crawl'); c.add_argument('--limit', type=int, default=20); c.add_argument('--pages', type=int, default=4)
    pr = sub.add_parser('prom'); pr.add_argument('urls', nargs='*'); pr.add_argument('--config'); pr.add_argument('--campaign', default='films-ua'); pr.add_argument('--limit', type=int, default=30)
    au = sub.add_parser('audit'); au.add_argument('--limit', type=int, default=20)
    for command in ('export', 'report'):
        out = sub.add_parser(command); out.add_argument('file')
    u = sub.add_parser('update'); u.add_argument('id', type=int); u.add_argument('--status', choices=STATUSES); u.add_argument('--notes'); u.add_argument('--role', choices=('manufacturer','seller','installer','directory','unverified'))
    sub.add_parser('list')
    a = p.parse_args()
    with connect(a.db) as db:
        if a.command == 'import':
            for entry in json.loads(Path(a.file).read_text(encoding='utf-8')):
                add(db, entry['url'], entry.get('name',''), a.campaign, entry.get('source',entry['url']))
        elif a.command == 'search':
            search(db, json.loads(Path(a.config).read_text(encoding='utf-8')))
        elif a.command == 'crawl':
            if not 1 <= a.limit <= 500 or not 1 <= a.pages <= 20:
                raise ValueError('limit must be 1..500, pages 1..20')
            companies = db.execute("SELECT * FROM companies WHERE status!='do_not_contact' AND domain NOT LIKE 'prom.ua/%' ORDER BY last_checked IS NOT NULL,last_checked,id LIMIT ?", (a.limit,)).fetchall()
            for company in companies:
                print('Collecting:', company['domain'], flush=True)
                crawl(db, company, a.pages)
        elif a.command == 'prom':
            targets = list(a.urls)
            if a.config:
                targets += json.loads(Path(a.config).read_text(encoding='utf-8')).get('prom_pages', [])
            if not targets or not 1 <= a.limit <= 100:
                raise ValueError('give prom.ua URLs or --config with prom_pages; limit must be 1..100')
            prom(db, targets, a.campaign, a.limit)
        elif a.command == 'audit':
            if not 1 <= a.limit <= 200:
                raise ValueError('limit must be 1..200')
            rows = db.execute('''SELECT c.* FROM companies c LEFT JOIN audits a ON a.company_id=c.id
                WHERE c.status!='do_not_contact' ORDER BY a.checked_at IS NOT NULL,a.checked_at,c.id LIMIT ?''', (a.limit,)).fetchall()
            for company in rows:
                offer, score = audit(db, company)
                print('Audit:', company['domain'], '->', offer, score if score is not None else '', flush=True)
        elif a.command == 'export':
            export(db, a.file)
        elif a.command == 'report':
            report(db, a.file)
        elif a.command == 'update':
            if not db.execute('SELECT 1 FROM companies WHERE id=?', (a.id,)).fetchone():
                raise ValueError('Company not found')
            for field in ('status','notes','role'):
                value = getattr(a,field)
                if value is not None:
                    db.execute('UPDATE companies SET ' + field + '=? WHERE id=?', (value,a.id))
        else:
            for row in db.execute('SELECT id,name,city,site_kind,status,error FROM companies'):
                print(dict(row))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
