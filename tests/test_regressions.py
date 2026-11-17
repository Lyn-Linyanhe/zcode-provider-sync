"""Regression tests use synthetic providers only; never load the user's config."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sync
import app


def fixture():
    return {"schemaVersion": 1, "config": {
        "providerOrder": ["p"],
        "providerConfigRules": {"providerRules": [{
            "providerId": "p", "providerName": "Fixture", "enabled": False,
            "config": {"group": "standard-personal", "access": {"apiKey": "test-only"},
                       "api": {"type": "openai-chat-completions", "baseUrl": "https://example.invalid"},
                       "personalModelIds": ["a", "b"], "modelOrder": ["a", "b"]}}]},
        "modelConfigRules": {"providerModelRules": [
            {"providerId": "p", "modelId": "a", "config": {"enabled": False,
             "optionSpecs": {"reasoningLevel": {"values": ["custom", "high"], "map": "{}"}}}},
            {"providerId": "p", "modelId": "b", "config": {"properties": {"contextWindow": 500}}}
        ], "manualProviderModelRules": []}}}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "provider.json"
        self.data = fixture()
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **overrides):
        args = dict(name="Fixture", base_url="https://example.invalid", api_key="test-only",
                    api_type="chat", provider_id="p", models=[{"id": "a"}], path=self.path)
        args.update(overrides)
        return sync.save_provider(**args)

    def test_new_same_name_rejected_without_writing(self):
        before = self.path.read_bytes()
        with self.assertRaises(sync.SyncError):
            self.save(provider_id=None)
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_provider_id_is_not_name_fallback(self):
        with self.assertRaises(sync.SyncError):
            self.save(provider_id="gone")

    def test_external_change_rejected(self):
        revision = sync.config_revision(self.data)
        changed = copy.deepcopy(self.data)
        changed["config"]["providerOrder"].append("external")
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(sync.SyncError):
            self.save(expected_revision=revision)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob("*.bak-*")))

    def test_inherited_context_not_overridden(self):
        self.save()
        rules = sync.model_rules(sync.load_config(self.path))
        self.assertNotIn("contextWindow", rules[0]["config"].get("properties", {}))
        self.assertEqual("{}", rules[0]["config"]["optionSpecs"]["reasoningLevel"]["map"])
        self.assertFalse(rules[0]["config"]["enabled"])

    def test_deleted_target_then_rename_preserves_source(self):
        self.save(models=[{"id": "b", "origId": "a"}])
        rules = sync.model_rules(sync.load_config(self.path))
        self.assertEqual(["b"], [r["modelId"] for r in rules])
        self.assertFalse(rules[0]["config"]["enabled"])
        self.assertEqual("{}", rules[0]["config"]["optionSpecs"]["reasoningLevel"]["map"])

    def test_duplicate_and_long_ids_rejected(self):
        for models in ([{"id": "a"}, {"id": "a"}], [{"id": "x" * 201}]):
            with self.assertRaises(sync.SyncError):
                self.save(models=models)


class NetworkTests(unittest.TestCase):
    def test_backup_url_after_timeout(self):
        def get(url, headers, **kwargs):
            if url.endswith("/v1/models"):
                return 200, {"data": [{"id": "ok"}]}, ""
            raise sync.SyncError("连接超时")
        with patch.object(sync, "http_get_json", side_effect=get):
            url, records = sync.fetch_models("https://example.invalid", "test-only", "chat")
        self.assertTrue(url.endswith("/v1/models"))
        self.assertEqual("ok", records[0]["id"])

    def test_redirect_rejects_cross_origin_and_downgrade(self):
        import urllib.request
        handler = sync.SafeRedirectHandler()
        req = urllib.request.Request("https://example.invalid/models", headers={"Authorization": "Bearer fake"})
        for target in ("https://other.invalid/models", "http://example.invalid/models"):
            with self.assertRaises(sync.SyncError):
                handler.redirect_request(req, None, 302, "Found", {}, target)
        redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://example.invalid/v1/models")
        self.assertIsNotNone(redirected)


class CatalogTests(unittest.TestCase):
    def test_partial_rule_does_not_mask_base_fields(self):
        import re
        rules = [(re.compile(r"qwen3-max-2026"), {"properties": {"inputFormat": {"supportsImage": True}}}),
                 (re.compile(r"qwen3-max(?:-2026)?"), {"properties": {"contextWindow": 1000000},
                    "optionSpecs": {"maxOutputTokens": {"max": 100000}}})]
        with patch.object(app, "_load_builtin_rules", return_value=rules):
            found = app._builtin_model_info("qwen3-max-2026")
        self.assertEqual(1000000, found["contextWindow"])
        self.assertEqual(100000, found["maxOutputTokens"])

    def test_model_prefix_is_not_a_match(self):
        import re
        with patch.object(app, "_load_builtin_rules", return_value=[
            (re.compile(r".*gpt-4o(?:[.\-:/\[].*)?"), {"properties": {"contextWindow": 128000}})
        ]):
            self.assertEqual({}, app._builtin_model_info("gpt-4oops"))


if __name__ == "__main__":
    unittest.main()
