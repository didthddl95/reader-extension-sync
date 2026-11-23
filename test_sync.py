import json
from pathlib import Path
import tempfile
import unittest

import sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.custom = {'id': 99, 'name': 'Custom', 'version': '1.0.0',
                       'sourceCodeUrl': 'https://example.com/custom.js'}
        (self.root / 'index.json').write_bytes(sync.encoded([self.custom]))
        self.row = {'id': 1, 'name': 'Fixture', 'version': '1.0.0',
                    'sourceCodeUrl': sync.ORIGIN + '/mangayomi/manga/a.js?v=1.0.0',
                    'iconUrl': sync.ORIGIN + '/mangayomi/icon.png'}
        extra = dict(self.row, id=2, sourceCodeUrl=sync.ORIGIN + '/mangayomi/extra/manga/a.js')
        self.data = {sync.FEEDS[0]: sync.encoded([self.row]),
                     sync.FEEDS[1]: sync.encoded([extra]),
                     self.row['sourceCodeUrl']: b'const a = 1;',
                     extra['sourceCodeUrl']: b'const a = 2;'}

    def plan(self):
        return sync.plan(self.root, '42', self.data.__getitem__)

    def test_custom_preserved_paths_separate_raw_urls_valid_and_idempotent(self):
        files, deletes, count, custom = self.plan()
        self.assertEqual((count, custom), (2, 1))
        rows = json.loads(files['index.json'])
        self.assertEqual(rows[0], self.custom)
        self.assertIn('upstream%2Fmanga%2Fa.js/raw?ref=main', rows[1]['sourceCodeUrl'])
        self.assertIn('upstream/extra/manga/a.js', files)
        self.assertEqual(rows[1]['iconUrl'], self.row['iconUrl'])
        sync.apply(self.root, files, deletes)
        self.assertEqual(self.plan()[0], files)

    def test_same_version_code_change_gets_update_version(self):
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        self.data[self.row['sourceCodeUrl']] = b'const a = 3;'
        files, deletes, *_ = self.plan()
        self.assertEqual(json.loads(files['index.json'])[1]['version'], '1.0.0.1')
        sync.apply(self.root, files, deletes)
        self.assertEqual(self.plan()[0], files)

    def test_failed_fetch_and_bad_js_leave_checkout_untouched(self):
        before = (self.root / 'index.json').read_bytes()
        self.data.pop(self.row['sourceCodeUrl'])
        with self.assertRaises(KeyError):
            self.plan()
        self.data[self.row['sourceCodeUrl']] = b'const = {'
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual((self.root / 'index.json').read_bytes(), before)
        self.assertFalse((self.root / 'upstream').exists())

    def test_id_collision_empty_feed_and_downgrade_rejected(self):
        original = self.data[sync.FEEDS[0]]
        for replacement in [[], [dict(self.row, id=99)]]:
            self.data[sync.FEEDS[0]] = sync.encoded(replacement)
            with self.assertRaises(ValueError):
                self.plan()
        self.data[sync.FEEDS[0]] = original
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        self.data[sync.FEEDS[0]] = sync.encoded([dict(self.row, version='0.9.0')])
        with self.assertRaises(ValueError):
            self.plan()

    def test_external_asset_and_path_traversal_rejected(self):
        for url in ['https://evil.example/a.js', sync.ORIGIN + '/mangayomi/%2e%2e/key.js',
                    sync.ORIGIN + '/mangayomi/a%5cb.js']:
            with self.assertRaises(ValueError):
                sync.asset_path(url)
        with self.assertRaises(ValueError):
            sync.apply(self.root, {'../escape': b'bad'}, set())

    def test_only_managed_removed_files_deleted(self):
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        self.data[sync.FEEDS[1]] = sync.encoded([dict(self.row, id=3)])
        files, deletes, *_ = self.plan()
        self.assertEqual(deletes, {'upstream/extra/manga/a.js'})
        self.assertEqual(json.loads(files['index.json'])[0], self.custom)


if __name__ == '__main__':
    unittest.main()
