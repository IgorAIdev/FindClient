import csv
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import findclient as f

class FindClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = f.connect(str(Path(self.tmp.name)/'test.db'))
        self.ident = f.add(self.db,'https://example.com','Example','test','manual')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def company(self):
        return self.db.execute('SELECT * FROM companies').fetchone()

    def test_contacts(self):
        page = f.Page('<script>fake@example.com</script><a href="mailto:sales@example.com?subject=Hi">mail</a><a href="tel:+380963431112">call</a><a href="https://t.me/example">TG</a>')
        self.assertEqual(page.contacts(), {('email','sales@example.com'),('phone','+380963431112'),('messenger','https://t.me/example')})

    def test_duplicate_preserves_work(self):
        self.db.execute("UPDATE companies SET status='called',notes='keep'")
        f.add(self.db,'https://www.example.com/contact','New title','other','search')
        self.assertEqual(self.db.execute('SELECT count(*) FROM companies').fetchone()[0],1)
        self.assertEqual(self.company()['notes'],'keep')
        self.assertEqual(self.company()['status'],'called')

    @patch('findclient.time.sleep')
    @patch('findclient.fetch')
    def test_crawl_and_dedup(self, fetch, sleep):
        fetch.side_effect = lambda url: 'User-agent: *\nAllow: /' if url.endswith('/robots.txt') else '<a href="mailto:hi@example.com">Email</a>'
        f.crawl(self.db,self.company())
        f.crawl(self.db,self.company())
        self.assertEqual(self.db.execute('SELECT count(*) FROM contacts').fetchone()[0],1)
        self.assertIsNone(self.company()['error'])

    @patch('findclient.fetch',return_value='User-agent: *\nDisallow: /')
    def test_robots(self,fetch):
        f.crawl(self.db,self.company())
        self.assertEqual(fetch.call_count,1)
        self.assertIn('disallows',self.company()['error'])

    def test_export_and_html(self):
        self.db.execute('UPDATE companies SET name=?,notes=?',('<script>alert(1)</script>','=1+1'))
        dest=Path(self.tmp.name)/'contacts.csv'
        f.export(self.db,dest)
        with dest.open(encoding='utf-8-sig') as stream:
            row=list(csv.DictReader(stream))[0]
        self.assertEqual(row['notes'],"'=1+1")
        report=Path(self.tmp.name)/'report.html'
        f.report(self.db,report)
        self.assertNotIn('<script>',report.read_text())

    @patch('findclient.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))])
    def test_private_target(self,_):
        with self.assertRaises(ValueError):
            f.public_url('http://localhost/')

    def prom_page(self, site, name='Тент Тест', phone='+380 (93) 440-27-52'):
        org = {'@type': 'Organization', 'name': name, 'telephone': phone, 'email': 'Sales@Example.com',
               'address': {'addressLocality': 'Киев'}}
        state = '"company":{"id":777,"webSiteUrl":' + (json.dumps(site) if site else 'null') + '}'
        return ('<script type="application/ld+json">' + json.dumps(org, ensure_ascii=False) + '</script>'
                '<script>window.x={' + state + '}</script>')

    def test_classify_site(self):
        self.assertEqual(f.classify_site(None), 'none')
        self.assertEqual(f.classify_site('javascript:alert(1)'), 'none')
        self.assertEqual(f.classify_site('https://shop.prom.ua/'), 'prom_hosted')
        self.assertEqual(f.classify_site('https://www.facebook.com/x'), 'social')
        self.assertEqual(f.classify_site('https://x.wixsite.com/a'), 'free_host')
        self.assertEqual(f.classify_site('https://tentkiev.com.ua/'), 'own')

    def test_prom_url_rules(self):
        self.assertEqual(f.prom_url_kind('https://prom.ua/c3777612-tentkiev.html'), 'profile')
        self.assertEqual(f.prom_url_kind('https://prom.ua/Myagkoe-steklo.html'), 'category')
        self.assertIsNone(f.prom_url_kind('https://prom.ua/Myagkoe-steklo.html?page=2'))
        self.assertIsNone(f.prom_url_kind('https://prom.ua/company-contacts'))
        self.assertIsNone(f.prom_url_kind('https://evil.example/c1-a.html'))
        self.assertIsNone(f.prom_url_kind('http://prom.ua/c1-a.html'))

    def test_prom_category_links(self):
        raw = '<a href="/c1-a.html">1</a><a href="/c1-a.html">dup</a><a href="/p5-item.html">item</a><a href="/c2-b.html">2</a>'
        self.assertEqual(f.prom_company_links(raw), ['https://prom.ua/c1-a.html', 'https://prom.ua/c2-b.html'])

    def test_prom_profile_own_site_merges_by_domain(self):
        self.db.execute("UPDATE companies SET status='called',notes='keep'")
        raw = self.prom_page('https://www.example.com/')
        ident, kind = f.ingest_prom(self.db, raw, 'https://prom.ua/c777-test.html', 'prom', 'prom: cat')
        self.assertEqual((ident, kind), (self.ident, 'own'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM companies').fetchone()[0], 1)
        self.assertEqual((self.company()['status'], self.company()['notes'], self.company()['city']), ('called', 'keep', 'Киев'))
        contacts = {tuple(r) for r in self.db.execute('SELECT kind,value FROM contacts')}
        self.assertEqual(contacts, {('phone', '+380934402752'), ('email', 'sales@example.com')})

    def test_prom_profile_without_own_site(self):
        for site in (None, 'https://shop.prom.ua/'):
            f.ingest_prom(self.db, self.prom_page(site), 'https://prom.ua/c777-test.html', 'prom', 'prom: cat')
        row = self.db.execute("SELECT * FROM companies WHERE domain='prom.ua/c777'").fetchone()
        self.assertEqual(row['site_kind'], 'prom_hosted')
        self.assertEqual(row['url'], 'https://prom.ua/c777-test.html')
        self.assertEqual(self.db.execute("SELECT count(*) FROM companies WHERE domain LIKE 'prom.ua/%'").fetchone()[0], 1)

    def test_not_a_profile(self):
        with self.assertRaises(ValueError):
            f.prom_company('<html></html>', 'https://prom.ua/c1-a.html')

    GOOD = ('<html lang="uk"><head><title>Виробник тентів ПВХ у Києві — ціни</title>'
            '<meta name="viewport" content="width=device-width"><meta name="description" content="' + 'Тенти ПВХ на замовлення, '*3 + '">'
            '<link rel="canonical" href="/"><meta property="og:title" content="x"><script type="application/ld+json">{}</script>'
            '</head><body><h1>Тенти</h1><h2>A</h2><h2>B</h2><a href="/blog/">Статті</a>'
            '<img src="a.jpg" alt="тент"><p>+380 93 440 27 52 ' + 'слово '*320 + '© 2026</p></body></html>')
    POOR = '<html><head><title>x</title></head><body><center><font>Тенти</font></center><img src="a.gif"></body></html>'

    def test_score_site(self):
        good = f.score_site(self.GOOD, 'https://example.com/', 0.5, 40, 2026)
        poor = f.score_site(self.POOR, 'http://example.com/', 5, None, 2026)
        self.assertEqual(sum(c['max'] for c in good['checks']), 100)
        self.assertTrue(90 <= good['score'] <= 100)
        self.assertLess(poor['score'], 25)
        self.assertIn('Нет HTTPS', poor['issues'])
        self.assertEqual(f.recommend('own', good)[0], 'none')
        self.assertEqual(f.recommend('own', poor)[0], 'redesign')

    def test_new_products_link_is_not_a_blog(self):
        page = '<a href="/ua/new_products">Новинки</a>'
        self.assertFalse(f.score_site(page, 'https://e.com/')['blog'])
        self.assertTrue(f.score_site('<a href="/ua/news">Новини</a>', 'https://e.com/')['blog'])

    def test_recommend_levels(self):
        mid = {'score': 60, 'issues': ['a'], 'sitemap_pages': 40, 'blog': True}
        self.assertEqual(f.recommend('own', mid)[0], 'seo')
        self.assertEqual(f.recommend('own', {**mid, 'score': 80, 'blog': False})[0], 'content')
        self.assertEqual(f.recommend('own', {**mid, 'score': 80, 'sitemap_pages': 5})[0], 'content')
        self.assertEqual(f.recommend('social')[0], 'site')

    @patch('findclient.fetch')
    def test_audit_weak_site_needs_no_fetch(self, fetch):
        f.ingest_prom(self.db, self.prom_page(None), 'https://prom.ua/c777-test.html', 'prom', 'prom: cat')
        company = self.db.execute("SELECT * FROM companies WHERE domain='prom.ua/c777'").fetchone()
        self.assertEqual(f.audit(self.db, company), ('site', None))
        fetch.assert_not_called()

    @patch('findclient.time.sleep')
    @patch('findclient.fetch')
    def test_audit_site(self, fetch, sleep):
        pages = {'https://example.com/robots.txt': 'User-agent: *\nAllow: /',
                 'https://example.com/': self.GOOD,
                 'https://example.com/sitemap.xml': '<urlset>' + '<loc>x</loc>' * 30 + '</urlset>'}
        fetch.side_effect = lambda url: pages[url]
        offer, score = f.audit(self.db, self.company())
        self.assertEqual(offer, 'none')
        self.assertGreaterEqual(score, 90)
        row = self.db.execute('SELECT * FROM audits').fetchone()
        self.assertEqual(json.loads(row['details'])['sitemap_pages'], 30)

    @patch('findclient.time.sleep')
    @patch('findclient.fetch')
    def test_audit_detects_prom_platform_site(self, fetch, sleep):
        home = self.GOOD.replace('</head>', '<script src="/cloud-cgi/static/uaprom-static/js/adv_teasers.js"></script></head>')
        pages = {'https://example.com/robots.txt': '', 'https://example.com/': home, 'https://example.com/sitemap.xml': ''}
        fetch.side_effect = lambda url: pages[url]
        self.assertEqual(f.audit(self.db, self.company())[0], 'prom_site')
        self.assertEqual(self.company()['site_kind'], 'prom_platform')

    @patch('findclient.time.sleep')
    @patch('findclient.fetch', side_effect=OSError('boom'))
    def test_audit_unreachable(self, fetch, sleep):
        self.assertEqual(f.audit(self.db, self.company()), ('unreachable', None))
        self.assertIn('boom', self.db.execute('SELECT error FROM audits').fetchone()[0])

    @patch('findclient.time.sleep')
    @patch('findclient.fetch', return_value='User-agent: *\nDisallow: /')
    def test_audit_respects_robots(self, fetch, sleep):
        self.assertEqual(f.audit(self.db, self.company())[0], 'unreachable')
        self.assertEqual(fetch.call_count, 1)

    def test_gzip_response_is_decoded(self):
        class Response(io.BytesIO):
            headers = type('H', (), {'get_content_charset': lambda self: 'utf-8'})()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        opener = type('O', (), {'open': lambda self, req, timeout=0: Response(gzip.compress('<loc>я</loc>'.encode()))})()
        with patch('findclient.public_url'), patch('findclient.build_opener', return_value=opener):
            self.assertEqual(f.fetch('https://example.com/sitemap.xml'), '<loc>я</loc>')

    def test_old_database_is_migrated(self):
        path = Path(self.tmp.name) / 'old.db'
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE companies (id INTEGER PRIMARY KEY, domain TEXT UNIQUE, name TEXT, url TEXT, campaign TEXT, status TEXT DEFAULT "new", notes TEXT DEFAULT "", last_checked TEXT, error TEXT, role TEXT DEFAULT "unverified")')
        old.execute("INSERT INTO companies(domain,name,url,campaign) VALUES('a.ua','A','https://a.ua','x')")
        old.commit(); old.close()
        db = f.connect(str(path))
        self.assertIsNone(db.execute('SELECT site_kind FROM companies').fetchone()[0])
        db.close()

    def test_position_and_visibility(self):
        urls = ['https://a.ua/', 'https://www.example.com/x', 'https://shop.example.com/']
        self.assertEqual(f.position(urls, 'example.com'), 2)
        self.assertIsNone(f.position(urls, 'other.ua'))
        self.assertEqual([f.visibility_points(p) for p in (None, 1, 3, 4, 10, 11, 20)], [0, 1, 1, 0.6, 0.6, 0.3, 0.3])

    @patch('findclient.time.sleep')
    @patch('findclient.brave_results')
    def test_seo_visibility(self, brave, sleep):
        self.db.execute("UPDATE companies SET city='Киев'")
        answers = {'тент': ['https://example.com/'], 'тент Киев': ['https://x.ua/'] * 4 + ['https://example.com/a'],
                   'site:example.com': ['https://example.com/'] * 3}
        brave.side_effect = lambda key, query, **kw: [{'url': u} for u in answers[query]]
        with patch.dict('os.environ', {'BRAVE_SEARCH_API_KEY': 'k'}):
            f.seo(self.db, {'seo_queries': ['тент']})
        row = self.db.execute('SELECT * FROM audits').fetchone()
        self.assertEqual((row['visibility'], row['indexed']), (80, '1-19'))  # (1 + 0.6) / 2
        self.assertEqual(self.db.execute('SELECT position FROM rankings WHERE query=?', ('тент Киев',)).fetchone()[0], 5)
        self.assertEqual(brave.call_args.kwargs, {'country': 'UA'})

    def test_seo_needs_key_and_queries(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                f.seo(self.db, {'seo_queries': ['x']})
        with patch.dict('os.environ', {'BRAVE_SEARCH_API_KEY': 'k'}):
            with self.assertRaises(ValueError):
                f.seo(self.db, {})

    @patch('findclient.time.sleep')
    @patch('findclient.fetch')
    def test_audit_keeps_search_visibility(self, fetch, sleep):
        self.db.execute('INSERT INTO audits(company_id,visibility,indexed) VALUES(?,?,?)', (self.ident, 55, '20+'))
        pages = {'https://example.com/robots.txt': '', 'https://example.com/': self.GOOD, 'https://example.com/sitemap.xml': ''}
        fetch.side_effect = lambda url: pages[url]
        f.audit(self.db, self.company())
        row = self.db.execute('SELECT * FROM audits').fetchone()
        self.assertEqual((row['visibility'], row['indexed']), (55, '20+'))
        self.assertIsNotNone(row['score'])

    def test_keyword_ideas_from_listings(self):
        other = f.add(self.db, 'https://other.ua', 'Other', 'test', 'manual')
        for ident, name in ((self.ident, 'Тент ПВХ 650 для прицепа'), (other, 'Тент ПВХ на Газель &amp;quot;Plandeka&amp;quot;'), (other, 'Мягкое стекло')):
            self.db.execute('INSERT INTO listings VALUES(?,?)', (ident, name))
        ideas = dict(f.keyword_ideas(self.db))
        self.assertEqual(ideas['тент пвх'], 2)
        self.assertNotIn('мягкое стекло', ideas)  # one seller only
        self.assertFalse({'quot', 'plandeka', 'amp'} & set(ideas))

    def test_prom_profile_stores_listing_names(self):
        raw = self.prom_page('https://www.example.com/') + '<script type="application/ld+json">{"@type":"Product","name":"Тент ПВХ"}</script>'
        f.ingest_prom(self.db, raw, 'https://prom.ua/c777-test.html', 'prom', 'prom: cat')
        self.assertEqual([r[0] for r in self.db.execute('SELECT name FROM listings')], ['Тент ПВХ'])

    def test_old_audits_table_is_migrated(self):
        path = Path(self.tmp.name) / 'old2.db'
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE audits (company_id INTEGER PRIMARY KEY, checked_at TEXT, score INTEGER, offer TEXT, summary TEXT, details TEXT, error TEXT)')
        old.commit(); old.close()
        db = f.connect(str(path))
        db.execute('SELECT visibility,indexed FROM audits')
        db.close()

if __name__ == '__main__':
    unittest.main()
