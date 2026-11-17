"""Tk integration checks against synthetic configuration, no real credentials."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import sync
from test_regressions import fixture


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        data = fixture()
        data['config']['providerConfigRules']['providerRules'].append({
            'providerId': 'other', 'providerName': 'Other', 'config': {
                'access': {'apiKey': 'test-only'}, 'api': {'type': 'openai-chat-completions', 'baseUrl': 'https://example.invalid'},
                'personalModelIds': ['z']}})
        self.path.write_text(json.dumps(data), encoding='utf-8')
        self.patch = patch.object(app, 'DEFAULT_CONFIG', self.path)
        self.patch.start()
        self.root = app.App()
        self.root.update()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        app.Popup.close()
        self.root.destroy()
        self.patch.stop()
        self.tmp.cleanup()
        self.assertEqual([], self.errors)

    def test_loaded_values_and_noop_save(self):
        row = self.root.rows[0]
        self.assertEqual(['custom', 'high'], row['checked'])
        self.assertFalse(self.root._is_dirty())
        self.assertTrue(self.root.on_save())
        cfg = sync.model_rules(sync.load_config(self.path))[0]['config']
        self.assertNotIn('contextWindow', cfg.get('properties', {}))
        self.assertEqual(['custom', 'high'], cfg['optionSpecs']['reasoningLevel']['values'])
        self.assertFalse(self.root._is_dirty())

    def test_manual_clear_survives_fetch(self):
        row = self.root.rows[0]
        row['max'].set('900')
        row['max'].set('')
        row['ctx'].set('100')
        row['ctx'].set('')
        row['checked'] = []
        row['touched'].add('reason')
        self.root._sync_default_drop(row, source='checks')
        self.root.catalog = [{'id': 'a', 'contextWindow': 5000, 'maxOutputTokens': 1000, 'reasoningValues': ['high']}]
        self.root._autofill_rows_from_catalog()
        self.assertEqual('', row['ctx'].get())
        self.assertEqual('', row['max'].get())
        self.assertEqual([], row['checked'])

    def test_change_model_updates_auto_but_keeps_manual(self):
        self.root.add_blank_row()
        row = self.root.rows[-1]
        self.root.catalog = [{'id': 'fresh-a', 'contextWindow': 1000, 'maxOutputTokens': 100},
                             {'id': 'fresh-b', 'contextWindow': 2000, 'maxOutputTokens': 200}]
        row['id'].set('fresh-a')
        self.root.on_model_picked(row)
        self.assertEqual('1000', row['ctx'].get())
        row['max'].set('77')
        row['id'].set('fresh-b')
        self.root.on_model_picked(row)
        self.assertEqual('2000', row['ctx'].get())
        self.assertEqual('77', row['max'].get())

    def test_external_change_blocked_in_ui(self):
        data = sync.load_config(self.path)
        data['config']['providerOrder'].append('external')
        self.path.write_text(json.dumps(data), encoding='utf-8')
        before = self.path.read_bytes()
        self.assertFalse(self.root.on_save())
        self.assertIn('其他窗口', self.root.status.get())
        self.assertEqual(before, self.path.read_bytes())

    def test_cancel_navigation_keeps_draft(self):
        self.root.rows[0]['ctx'].set('1234')
        with patch.object(app.messagebox, 'askyesnocancel', return_value=None) as prompt:
            self.root.select_provider('other')
            self.assertTrue(prompt.called)
        self.assertEqual('p', self.root.provider_id.get())
        self.assertEqual('1234', self.root.rows[0]['ctx'].get())
        self.root.select_provider('p')
        self.assertEqual('1234', self.root.rows[0]['ctx'].get())

    def test_bad_reload_keeps_display_and_error(self):
        self.path.write_text('{bad', encoding='utf-8')
        self.root.reload_click()
        self.assertIn('读取失败', self.root.status.get())
        self.assertEqual('a', self.root.rows[0]['id'].get())

    def test_empty_rows_reused_and_last_delete_is_empty(self):
        self.root.add_blank_row()
        self.root.add_blank_row()
        self.assertEqual(1, sum(not r['id'].get() for r in self.root.rows))
        for row in list(self.root.rows):
            self.root.remove_row(row['frame'])
        self.root.update()
        self.assertEqual([], self.root.rows)
        self.assertTrue(self.root.empty.winfo_ismapped())

    def test_default_not_added_without_confirmation(self):
        row = self.root.rows[0]
        with patch.object(app.messagebox, 'askyesno', return_value=False):
            self.root._on_default_picked(row, '极致')
        self.assertNotIn('ultra', row['checked'])
        with patch.object(app.messagebox, 'askyesno', return_value=True):
            self.root._on_default_picked(row, '极致')
        self.assertIn('ultra', row['checked'])
        self.assertEqual('ultra', self.root._ordered_checks(row)[-1])

    def test_tooltip_single_instance_and_short_layout(self):
        self.root.catalog_ids = ['accounts/fireworks/models/' + 'very-long-model-' * 5]
        self.root.refresh_row_catalog()
        row = self.root.rows[0]
        row['id'].set(self.root.catalog_ids[0])
        self.root._update_name_col()
        tip = row['drop'].label._model_tip
        for _ in range(10):
            self.root._update_name_col()
        self.assertIs(tip, row['drop'].label._model_tip)
        self.assertEqual(row['id'].get(), tip.text())
        self.root.geometry('1280x820')
        self.root.update()
        self.assertTrue(row['compact'])
        self.assertLessEqual(row['right'].winfo_rootx() + row['right'].winfo_width(),
                             self.root.canvas.winfo_rootx() + self.root.canvas.winfo_width())

    def test_dialog_noop_preserves_auto_source_and_cleared_map(self):
        self.root.add_blank_row()
        row = self.root.rows[-1]
        row['id'].set('fresh-a')
        self.root.catalog = [{'id': 'fresh-a', 'contextWindow': 1000, 'maxOutputTokens': 100},
                             {'id': 'fresh-b', 'contextWindow': 2000}]
        self.root.on_model_picked(row)
        row['meta']['reasoningMap'] = 'old-map'
        row['reasoningMap'] = None
        row['mapChanged'] = True
        dialog = app.ModelEditDialog(self.root, row)
        self.assertEqual('', dialog.map_text.get('1.0', 'end').strip())
        dialog._save()
        self.assertEqual(set(), row['touched'])
        row['id'].set('fresh-b')
        self.root.on_model_picked(row)
        self.assertEqual('2000', row['ctx'].get())
        self.assertIsNone(row['reasoningMap'])

    def test_dialog_noop_keeps_unknown_order(self):
        row = self.root.rows[0]
        dialog = app.ModelEditDialog(self.root, row)
        dialog._save()
        self.assertEqual(['custom', 'high'], row['checked'])
        self.assertFalse(self.root._is_dirty())

    def test_dialog_cancel_and_fixed_footer(self):
        row = self.root.rows[0]
        dialog = app.ModelEditDialog(self.root, row)
        self.root.update()
        dialog.geometry('800x600')
        self.root.update()
        self.assertLessEqual(dialog.dialog_footer.winfo_rooty() + dialog.dialog_footer.winfo_height(),
                             dialog.winfo_rooty() + dialog.winfo_height())
        dialog.ctx_entry.delete(0, 'end')
        dialog.ctx_entry.insert(0, '888')
        dialog.destroy()
        self.assertEqual('', row['ctx'].get())


if __name__ == '__main__':
    unittest.main()
