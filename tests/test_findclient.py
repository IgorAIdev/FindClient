import csv
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

if __name__ == '__main__':
    unittest.main()
