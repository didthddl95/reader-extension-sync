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

    def test_duplicate_upstream_ids_and_all_empty_feeds_rejected(self):
        original = self.data[sync.FEEDS[1]]
        self.data[sync.FEEDS[1]] = self.data[sync.FEEDS[0]]
        with self.assertRaises(ValueError):
            self.plan()
        self.data[sync.FEEDS[1]] = original
        self.data[sync.FEEDS[0]] = sync.encoded([])
        self.data[sync.FEEDS[1]] = sync.encoded([])
        with self.assertRaises(ValueError):
            self.plan()

    def test_custom_collision_keeps_custom_and_allocates_stable_local_identity(self):
        self.data[sync.FEEDS[0]] = sync.encoded([dict(self.row, id=99)])
        files, deletes, count, custom = self.plan()
        rows = json.loads(files['index.json'])
        mapped = rows[1]['id']
        self.assertEqual(rows[0], self.custom)
        self.assertNotEqual(mapped, 99)
        self.assertEqual((count, custom), (2, 1))
        self.assertIn(str(mapped).encode(), files['upstream/manga/a.js'])
        sync.apply(self.root, files, deletes)
        self.assertEqual(self.plan()[0], files)
        # Removal drops only the managed entry/file, not its identity history.
        self.data[sync.FEEDS[0]] = sync.encoded([])
        removed, deletes, *_ = self.plan()
        self.assertEqual(json.loads(removed['index.json'])[0], self.custom)
        self.assertNotIn(mapped, [r['id'] for r in json.loads(removed['index.json'])])
        self.assertIn('upstream/manga/a.js', deletes)
        sync.apply(self.root, removed, deletes)
        self.data[sync.FEEDS[0]] = sync.encoded([dict(self.row, id=99)])
        returned, *_ = self.plan()
        self.assertEqual(json.loads(returned['index.json'])[1]['id'], mapped)
        self.assertEqual(json.loads(returned['index.json'])[0], self.custom)

    def test_reissued_lower_version_remains_newer_than_installed_and_is_idempotent(self):
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        lower = dict(self.row, version='0.9.0')
        self.data[sync.FEEDS[0]] = sync.encoded([lower])
        files, deletes, *_ = self.plan()
        self.assertEqual(json.loads(files['index.json'])[1]['version'], '1.0.0.1')
        sync.apply(self.root, files, deletes)
        self.assertEqual(self.plan()[0], files)
        # Delete/re-add with a reset version still gets an installable update.
        self.data[sync.FEEDS[0]] = sync.encoded([])
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        self.data[sync.FEEDS[0]] = sync.encoded([dict(lower, version='0.8.0')])
        files, *_ = self.plan()
        self.assertEqual(json.loads(files['index.json'])[1]['version'], '1.0.0.2')

    def test_moving_between_feeds_does_not_change_id(self):
        files, deletes, *_ = self.plan()
        sync.apply(self.root, files, deletes)
        self.data[sync.FEEDS[1]] = sync.encoded([self.row])
        self.data[sync.FEEDS[0]] = sync.encoded([])
        files, *_ = self.plan()
        self.assertEqual(json.loads(files['index.json'])[1]['id'], 1)

    def test_future_upstream_id_cannot_take_a_previous_local_identity(self):
        self.data[sync.FEEDS[0]] = sync.encoded([dict(self.row, id=99)])
        files, deletes, *_ = self.plan()
        mapped = json.loads(files['index.json'])[1]['id']
        sync.apply(self.root, files, deletes)
        self.data[sync.FEEDS[0]] = sync.encoded([dict(self.row, id=mapped)])
        files, *_ = self.plan()
        new_id = json.loads(files['index.json'])[1]['id']
        self.assertNotEqual(new_id, mapped)
        state = json.loads(files[sync.MANIFEST])
        self.assertEqual(state['id_map']['99'], mapped)

    def test_bridge_preserves_original_adapter_id_and_other_metadata_without_changing_local_id(self):
        import subprocess
        wrapper = sync.source_identity_bridge({'8000000000': 99})
        # Synthetic fixture only; downloaded extension code is never executed.
        code = b'''class MProvider { get source(){return {id:8000000000,baseUrl:'https://fixture.example'};} }
const source = new MProvider();
''' + wrapper + b'''console.log(JSON.stringify([source.source.id,source.source.baseUrl]));'''
        result = subprocess.run(['node', '-e', code.decode()], check=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout), [99,'https://fixture.example'])

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
