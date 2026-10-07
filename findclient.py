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
    ''')
    return db

def add(db, url, name, campaign, source):
    if urlsplit(url).scheme not in ('http', 'https') or not domain(url):
        raise ValueError('Invalid company URL')
    db.execute('INSERT OR IGNORE INTO companies(domain,name,url,campaign) VALUES(?,?,?,?)',
               (domain(url), name or domain(url), url, campaign))
    ident = db.execute('SELECT id FROM companies WHERE domain=?', (domain(url),)).fetchone()[0]
    db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?,?)', (ident, source, now()))
    db.commit()
    return ident

def crawl(db, company, max_pages=4, delay=2):
    origin = '{0.scheme}://{0.netloc}'.format(urlsplit(company['url']))
    robot = RobotFileParser(origin + '/robots.txt')
    try:
        try:
            robot.parse(fetch(robot.url).splitlines())
        except HTTPError as e:
            if e.code == 404:
                robot.parse([])
            else:
                raise
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
    rows = db.execute('''SELECT c.id,c.name,c.domain,c.url,c.campaign,c.role,c.status,c.notes,
       t.kind,t.value,t.source,t.checked_at,c.error FROM companies c
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
        cards.append('<article><h2>' + html.escape(c['name']) + '</h2><p>' + html.escape(c['status'] + ' · ' + c['role']) + '</p><a href="' + html.escape(c['url'], quote=True) + '">' + html.escape(c['domain']) + '</a><ul>' + details + '</ul><p>' + html.escape(c['notes']) + '</p><small>' + html.escape(c['error'] or '') + '</small></article>')
    Path(destination).write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FindClient</title><style>body{font:16px system-ui;background:#f2f5f8;max-width:1000px;margin:30px auto;padding:16px}article{background:white;padding:20px;margin:16px 0;border-radius:14px}a{color:#1764ad}li{overflow-wrap:anywhere;margin:8px 0}small{color:#9b3535}</style><h1>FindClient · Компании и контакты</h1><p>Публичные контакты. Роль компании и актуальность требуют проверки.</p>' + ''.join(cards) + '</html>', encoding='utf-8')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', default='data/findclient.sqlite3')
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import'); imp.add_argument('file'); imp.add_argument('--campaign', default='films-ua')
    s = sub.add_parser('search'); s.add_argument('config')
    c = sub.add_parser('crawl'); c.add_argument('--limit', type=int, default=20); c.add_argument('--pages', type=int, default=4)
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
            companies = db.execute("SELECT * FROM companies WHERE status!='do_not_contact' ORDER BY last_checked IS NOT NULL,last_checked,id LIMIT ?", (a.limit,)).fetchall()
            for company in companies:
                print('Collecting:', company['domain'], flush=True)
                crawl(db, company, a.pages)
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
            for row in db.execute('SELECT id,name,status,error FROM companies'):
                print(dict(row))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
