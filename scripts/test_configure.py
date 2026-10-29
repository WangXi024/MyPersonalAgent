"""Deployment preflight regression checks; no network, credentials, or containers."""
import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from configure import configure


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.origin = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.text = (
            "APP_PUBLIC_URL=http://localhost:8080\nAPP_BIND_HOST=127.0.0.1\n"
            "BOOTSTRAP_USERNAME=test\nBOOTSTRAP_PASSWORD=long-unit-test-only-password\n"
            "MODEL_PROVIDER=bailian\nBAILIAN_API_BASE=https://example.test/v1\n"
            "BAILIAN_MODEL=unit-test-model\nBAILIAN_API_KEY=<REDACTED>\n"
            "APP_MODEL_MONTHLY_BUDGET=20\nMEMORY_MODEL_MONTHLY_BUDGET=10"
        )
        Path(".env").write_text(self.text)

    def tearDown(self):
        os.chdir(self.origin)
        self.temp.cleanup()

    def run_configure(self, infrastructure_only=True):
        with contextlib.redirect_stdout(io.StringIO()):
            configure(".env", infrastructure_only=infrastructure_only)

    def test_production_rejects_placeholders_without_creating_credentials(self):
        with self.assertRaisesRegex(ValueError, "BAILIAN_API_KEY"):
            self.run_configure(False)
        self.assertFalse(Path(".secrets").exists())

    def test_repeat_preserves_internal_credentials_and_private_directory(self):
        self.run_configure()
        first = Path(".secrets/app_db_password").read_bytes()
        voice = Path(".secrets/voice_credentials_key").read_bytes()
        self.assertEqual(len(base64.urlsafe_b64decode(voice + b"=" * (-len(voice) % 4))), 32)
        self.run_configure()
        self.assertEqual(first, Path(".secrets/app_db_password").read_bytes())
        self.assertEqual(voice, Path(".secrets/voice_credentials_key").read_bytes())
        # Windows uses ACLs; st_mode does not express POSIX owner-only permissions.
        if os.name != "nt":
            self.assertEqual(Path(".secrets").stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(".env").stat().st_mode & 0o777, 0o600)

    def test_invalid_voice_key_cannot_be_silently_rotated(self):
        self.run_configure()
        key = Path(".secrets/voice_credentials_key")
        key.chmod(0o600)
        key.write_text("invalid-existing-key")
        with self.assertRaisesRegex(ValueError, "voice_credentials_key"):
            self.run_configure()
        self.assertEqual(key.read_text(), "invalid-existing-key")

    def test_voice_limits_are_validated_before_generating_credentials(self):
        for field, value in (("VOICE_ENABLED", "yes"), ("VOICE_MAX_SESSION_SECONDS", "901"),
                             ("VOICE_DAILY_SECONDS", "100"), ("VOICE_IDLE_SECONDS", "181"),
                             ("VOICE_MAX_CONCURRENT", "0"), ("VOICE_LEASE_SECONDS", "0")):
            with self.subTest(field=field):
                Path(".env").write_text(self.text + f"\n{field}={value}")
                with self.assertRaisesRegex(ValueError, field):
                    self.run_configure()
                self.assertFalse(Path(".secrets").exists())

    def test_explicit_sandbox_feature_flags_are_strict_booleans(self):
        for name in ("VERIFICATION_ENABLED", "MATERIAL_ENABLED"):
            Path(".env").write_text(self.text + f"\n{name}=yes\n")
            with self.assertRaisesRegex(ValueError, name):
                self.run_configure()
            self.assertFalse(Path(".secrets").exists())

    def test_http_cannot_bind_public_interfaces(self):
        Path(".env").write_text(self.text.replace("APP_BIND_HOST=127.0.0.1", "APP_BIND_HOST=0.0.0.0"))
        with self.assertRaisesRegex(ValueError, "loopback"):
            self.run_configure()

    def test_unverified_fallback_is_rejected(self):
        Path(".env").write_text(self.text + "\nMODEL_FALLBACK_PROVIDER=zhipu")
        with self.assertRaisesRegex(ValueError, "MODEL_FALLBACK_VERIFIED"):
            self.run_configure()

    def test_upstream_tls_does_not_expose_plaintext_backend(self):
        Path(".env").write_text(self.text.replace("http://localhost:8080", "https://learning.example.test").replace("APP_BIND_HOST=127.0.0.1", "APP_BIND_HOST=0.0.0.0") + "\nTLS_MODE=upstream")
        with self.assertRaisesRegex(ValueError, "plaintext backend"):
            self.run_configure()

    def test_explicit_prices_are_converted_without_embedding_secrets(self):
        Path(".env").write_text(self.text.replace("BAILIAN_API_KEY=<REDACTED>", "BAILIAN_API_KEY=unit-test-non-secret") + "\nBAILIAN_INPUT_USD_PER_MILLION=2\nBAILIAN_OUTPUT_USD_PER_MILLION=4")
        self.run_configure(False)
        config = Path(".runtime/litellm.json").read_text()
        self.assertNotIn("unit-test-non-secret", config)
        self.assertEqual(json.loads(config)["model_list"][0]["model_info"]["input_cost_per_token"], 0.000002)
        catalog = Path(".runtime/chat-models.json")
        self.assertEqual(json.loads(catalog.read_text()), [
            {"id": "learning-agent", "name": "unit-test-model", "provider": "bailian",
             "api_base": "https://example.test/v1", "usd_cny_rate": "7",
             "input_cny_per_million": "14.000000", "output_cny_per_million": "28.000000"},
        ])
        self.assertTrue(json.loads(config)["general_settings"]["store_model_in_db"])
        self.assertNotIn("unit-test-non-secret", catalog.read_text())
        self.assertEqual(catalog.stat().st_mode & 0o777, 0o444)

    def test_catalog_only_lists_enabled_primary_and_verified_fallback(self):
        Path(".env").write_text(self.text + "\nMODEL_FALLBACK_PROVIDER=zhipu\nMODEL_FALLBACK_VERIFIED=true"
                               "\nZHIPU_API_BASE=https://example.test/v1\nZHIPU_MODEL=test-fallback")
        self.run_configure()
        models = json.loads(Path(".runtime/chat-models.json").read_text())
        self.assertEqual([item["id"] for item in models], ["learning-agent", "learning-fallback"])
        self.assertEqual(models[1], {"id": "learning-fallback", "name": "test-fallback", "provider": "zhipu",
                                    "api_base": "https://example.test/v1", "usd_cny_rate": "7"})

    def test_gateway_allows_long_chat_and_planning_calls_even_with_legacy_timeout(self):
        for configured, expected in ((90, 630), (330, 630), (630, 630), (700, 700)):
            with self.subTest(timeout=configured):
                Path(".env").write_text(self.text + f"\nMODEL_REQUEST_TIMEOUT={configured}")
                self.run_configure()
                config = json.loads(Path(".runtime/litellm.json").read_text())
                self.assertEqual(config["router_settings"]["timeout"], expected)
                self.assertEqual(config["model_list"][0]["litellm_params"]["timeout"], expected)
                self.assertEqual(config["router_settings"]["num_retries"], 0)
                self.assertEqual(config["litellm_settings"]["num_retries"], 0)


if __name__ == "__main__":
    unittest.main()
