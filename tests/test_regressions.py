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

    def test_duplicate_provider_identity_rejected_without_writing(self):
        other = copy.deepcopy(self.data["config"]["providerConfigRules"]["providerRules"][0])
        other["providerName"] = "Duplicate identity"
        other["config"]["personalModelIds"] = ["b"]
        self.data["config"]["providerConfigRules"]["providerRules"].append(other)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(sync.SyncError):
            self.save(expected_revision=sync.config_revision(self.data))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob("*.bak-*")))

    def test_duplicate_and_long_ids_rejected(self):
        for models in ([{"id": "a"}, {"id": "a"}], [{"id": "x" * 201}]):
            with self.assertRaises(sync.SyncError):
                self.save(models=models)


class NetworkTests(unittest.TestCase):
    def test_refused_local_proxy_retries_get_directly(self):
        from unittest.mock import Mock
        import urllib.error
        import urllib.request
        import errno

        req = urllib.request.Request("https://example.invalid/models", headers={"Authorization": "Bearer fake"})
        proxy, direct = Mock(), Mock()

        def refused(request, **kwargs):
            request.set_proxy("127.0.0.1:7890", "http")
            raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

        proxy.open.side_effect = refused
        with patch.object(urllib.request, "getproxies", return_value={"https": "http://127.0.0.1:7890"}), \
             patch.object(urllib.request, "proxy_bypass", return_value=False), \
             patch.object(urllib.request, "build_opener", side_effect=[proxy, direct]) as build:
            self.assertIs(direct.open.return_value, sync._open_request(req, 8))
        retried = direct.open.call_args.args[0]
        self.assertEqual("example.invalid", retried.host)
        self.assertIsNone(retried._tunnel_host)
        self.assertEqual("Bearer fake", retried.get_header("Authorization"))
        self.assertLessEqual(direct.open.call_args.kwargs["timeout"], 8)
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, sync.SafeRedirectHandler) for h in handlers))
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers))

    def test_proxy_fallback_is_limited_to_local_refused_get(self):
        from unittest.mock import Mock
        import urllib.error
        import urllib.request
        import errno

        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        cases = [
            ("http://proxy.example.invalid:7890", "GET", refused, False),
            ("http://127.0.0.1:7890", "GET", TimeoutError(), False),
            ("http://127.0.0.1:7890", "POST", refused, False),
            ("http://127.0.0.1:7890", "GET", refused, True),
        ]
        for proxy_url, method, reason, bypass in cases:
            with self.subTest(proxy=proxy_url, method=method, reason=type(reason), bypass=bypass):
                opener = Mock()
                opener.open.side_effect = urllib.error.URLError(reason)
                req = urllib.request.Request("https://example.invalid/models", method=method)
                with patch.object(urllib.request, "getproxies", return_value={"https": proxy_url}), \
                     patch.object(urllib.request, "proxy_bypass", return_value=bypass), \
                     patch.object(urllib.request, "build_opener", return_value=opener) as build:
                    with self.assertRaises(urllib.error.URLError):
                        sync._open_request(req, 8)
                self.assertEqual(1, build.call_count)

    def test_backup_url_after_timeout(self):
        def get(url, headers, **kwargs):
            if url.endswith("/v1/models"):
                return 200, {"data": [{"id": "ok"}]}, ""
            raise sync.SyncError("连接超时")
        with patch.object(sync, "http_get_json", side_effect=get):
            url, records = sync.fetch_models("https://example.invalid", "test-only", "chat")
        self.assertTrue(url.endswith("/v1/models"))
        self.assertEqual("ok", records[0]["id"])

    def test_post_error_body_is_bounded_and_closed(self):
        import io
        import urllib.error

        class TrackedBody(io.BytesIO):
            def __init__(self):
                super().__init__(b"x" * 4096)
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return super().read(size)

        body = TrackedBody()
        error = urllib.error.HTTPError("https://example.invalid/chat/completions", 500, "error", {}, body)
        with patch.object(sync, "_open_request", side_effect=error):
            code, _ = sync._post_json("https://example.invalid/chat/completions", {}, {})
        self.assertEqual(500, code)
        self.assertEqual([400], body.read_sizes)
        self.assertTrue(body.closed)

    def test_model_test_reports_refused_redirect(self):
        with patch.object(sync, "_open_request", side_effect=sync.SyncError("已拒绝跨源重定向")):
            result = sync.test_connection("https://example.invalid/v1", "test-only", "chat", "a")
        self.assertFalse(result["ok"])
        self.assertIn("重定向", result["reason"])

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
    def lookup_fixture(self, database, model_id):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(json.dumps(database), encoding="utf-8")
            with patch.object(app, "BUILTIN_DB_PATH", path), \
                 patch.object(app, "ZCODE_BUILTIN_PATH", Path(tmp) / "missing.json"), \
                 patch.object(app, "_builtin_rules_cache", None):
                return app._builtin_model_info(model_id)

    def test_qualified_models_do_not_cross_vendor_boundaries(self):
        db = {"zcodeSnapshot": [], "supplemental": [
            {"sourceId": "openai/shared:batch", "vendor": "openai", "display": "shared:batch",
             "modelMatch": r"(?:[^/]+/)*shared:batch", "config": {"properties": {"contextWindow": 100000}}},
            {"sourceId": "google/shared:batch", "vendor": "google", "display": "shared:batch",
             "modelMatch": r"(?:[^/]+/)*shared:batch", "config": {"properties": {"contextWindow": 200000}}}
        ]}
        self.assertEqual(200000, self.lookup_fixture(db, "google/shared:batch")["contextWindow"])
        self.assertEqual(200000, self.lookup_fixture(db, "openrouter/google/shared:batch")["contextWindow"])
        self.assertEqual({}, self.lookup_fixture(db, "shared:batch"))

    def test_display_does_not_expand_normal_model_into_variant(self):
        db = {"zcodeSnapshot": [{"modelMatch": r"openai/shared:batch",
              "config": {"properties": {"contextWindow": 200000}}}],
              "supplemental": [{"sourceId": "openai/shared", "vendor": "openai", "display": "shared",
                  "modelMatch": r"openai/shared", "config": {"properties": {"contextWindow": 100000}}}]}
        self.assertEqual(200000, self.lookup_fixture(db, "openai/shared:batch")["contextWindow"])

    def test_reasoning_mapping_merges_independently_from_levels(self):
        import re
        rules = [(re.compile("a"), {"optionSpecs": {"reasoningLevel": {"map": "new-map"}}}),
                 (re.compile("a"), {"optionSpecs": {"reasoningLevel": {"values": ["low", "high"], "map": "old-map"}}})]
        with patch.object(app, "_load_builtin_rules", return_value=rules):
            found = app._builtin_model_info("a")
        self.assertEqual(["low", "high"], found["reasoningValues"])
        self.assertEqual("new-map", found["reasoningMap"])

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
