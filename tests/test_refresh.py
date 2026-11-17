import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'tools/refresh_builtin_models.py'
spec = importlib.util.spec_from_file_location('refresh', MODULE)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def payload():
    return {'data': [
        {'id': 'openai/model-a', 'top_provider': {'context_length': 10000, 'max_completion_tokens': 1000},
         'architecture': {'input_modalities': ['text', 'file']}},
        {'id': 'openai/model-a:batch', 'top_provider': {'context_length': 20000, 'max_completion_tokens': 500},
         'architecture': {'input_modalities': ['text']}}]}


class Response:
    status = 200
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self, size): return self.data[:size]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class RefreshTests(unittest.TestCase):
    def test_online_fetch_ignores_old_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'cache.json'
            cache.write_text('{"error":"old"}', encoding='utf-8')
            with patch.object(refresh.urllib.request, 'urlopen', return_value=Response(payload())) as fetch:
                data, stamp = refresh.fetch(cache=cache)
            self.assertEqual(1, fetch.call_count)
            self.assertTrue(stamp)
            self.assertEqual(2, len(data['data']))

    def test_error_payload_does_not_replace_output_or_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, output = Path(tmp)/'cache.json', Path(tmp)/'db.json'
            cache.write_text('old-cache', encoding='utf-8')
            output.write_text('{"zcodeSnapshot":[]}', encoding='utf-8')
            before = (cache.read_bytes(), output.read_bytes())
            with patch.object(refresh.urllib.request, 'urlopen', return_value=Response({'error': 'rate limit'})):
                with self.assertRaises(ValueError):
                    refresh.refresh(cache=cache, output=output, zcode=Path(tmp)/'missing')
            self.assertEqual(before, (cache.read_bytes(), output.read_bytes()))

    def test_variants_separate_no_pdf_inference(self):
        rules = refresh.build_rules(payload())
        self.assertEqual(['model-a', 'model-a:batch'], [r['display'] for r in rules])
        self.assertEqual([10000, 20000], [r['config']['properties']['contextWindow'] for r in rules])
        self.assertNotIn('supportsPdf', rules[0]['config']['properties']['inputFormat'])

    def test_offline_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)/'cache.json'
            cache.write_text(json.dumps({'payload': payload(), 'fetchedAt': '2026-09-01T00:00:00Z'}))
            with patch.object(refresh.urllib.request, 'urlopen') as network:
                _, stamp = refresh.fetch(offline=True, cache=cache)
            self.assertFalse(network.called)
            self.assertEqual('2026-09-01T00:00:00Z', stamp)

if __name__ == '__main__':
    unittest.main()
