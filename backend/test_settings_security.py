import unittest
from types import SimpleNamespace

from app.api.routes import (
    _secret_configuration,
    _without_blank_secret_updates,
    _without_secret_values,
)


class SettingsSecurityTests(unittest.TestCase):
    def test_secret_values_are_never_returned(self) -> None:
        values = {"llm_model": "example", "llm_api_key": "secret", "imap_password": "secret"}

        self.assertEqual({"llm_model": "example"}, _without_secret_values(values))

    def test_blank_secret_updates_preserve_existing_values(self) -> None:
        values = {"llm_model": "example", "llm_api_key": "", "imap_password": " replacement "}

        self.assertEqual(
            {"llm_model": "example", "imap_password": " replacement "},
            _without_blank_secret_updates(values),
        )

    def test_secret_configuration_prefers_runtime_value(self) -> None:
        settings = SimpleNamespace(llm_api_key="env-key", smtp_password="", imap_password="env-password")
        runtime = {"llm_api_key": "", "smtp_password": "runtime-password"}

        configured = _secret_configuration(runtime, settings)

        self.assertFalse(configured["llm_api_key"])
        self.assertTrue(configured["smtp_password"])
        self.assertTrue(configured["imap_password"])


if __name__ == "__main__":
    unittest.main()
