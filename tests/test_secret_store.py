from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import secret_store


class _Backend:
    """占位后端对象，仅用于验证标签与安全检查。"""


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.deleted = 0

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.deleted += 1
        self.values.pop((service, account), None)


def _patch(keyring_obj: _FakeKeyring):
    return mock.patch.object(secret_store, "_load_keyring", return_value=(keyring_obj, _Backend()))


class BackendSecurityTests(unittest.TestCase):
    def test_secure_backends_are_accepted(self) -> None:
        for label in (
            "keyring.backends.Windows.WinVaultKeyring",
            "keyring.backends.macOS.Keyring",
            "keyring.backends.SecretService.Keyring",
            "keyring.backends.kwallet.DBusKeyring",
        ):
            with self.subTest(label=label), \
                 mock.patch.object(secret_store, "_backend_label", return_value=label):
                secret_store._ensure_secure_backend(_Backend())  # 不抛错即为通过

    def test_plaintext_and_unknown_backends_are_rejected(self) -> None:
        for label in (
            "keyrings.alt.file.PlaintextKeyring",
            "keyring.backends.null.Keyring",
            "keyring.backends.fail.Keyring",
            "some.unknown.Backend",
        ):
            with self.subTest(label=label), \
                 mock.patch.object(secret_store, "_backend_label", return_value=label):
                with self.assertRaises(secret_store.SecretStoreError):
                    secret_store._ensure_secure_backend(_Backend())


class ApiKeyCrudTests(unittest.TestCase):
    def test_set_get_delete_roundtrip(self) -> None:
        keyring_obj = _FakeKeyring()
        with _patch(keyring_obj):
            self.assertIsNone(secret_store.get_api_key())
            secret_store.set_api_key("sk-roundtrip")
            self.assertEqual(secret_store.get_api_key(), "sk-roundtrip")
            self.assertTrue(secret_store.delete_api_key())
            self.assertIsNone(secret_store.get_api_key())
            self.assertFalse(secret_store.delete_api_key())

    def test_trailing_newlines_are_stripped(self) -> None:
        keyring_obj = _FakeKeyring()
        with _patch(keyring_obj):
            secret_store.set_api_key("sk-value\r\n")

        self.assertEqual(keyring_obj.values[(secret_store.SERVICE_NAME, secret_store.ACCOUNT_NAME)], "sk-value")

    def test_invalid_values_are_rejected(self) -> None:
        keyring_obj = _FakeKeyring()
        with _patch(keyring_obj):
            for value in ("", "   \n", "bad\x00key", "x" * 10001):
                with self.subTest(value=repr(value)[:40]):
                    with self.assertRaises(secret_store.SecretStoreError):
                        secret_store.set_api_key(value)

    def test_status_reports_configuration(self) -> None:
        keyring_obj = _FakeKeyring()
        with _patch(keyring_obj), \
             mock.patch.object(secret_store, "backend_name", return_value="keyring.backends.Windows.WinVaultKeyring"):
            self.assertFalse(secret_store.status()["configured"])
            secret_store.set_api_key("sk-status")
            payload = secret_store.status()
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["backend"], "keyring.backends.Windows.WinVaultKeyring")


class CliTests(unittest.TestCase):
    def test_status_prints_json_without_secret(self) -> None:
        keyring_obj = _FakeKeyring()
        out = io.StringIO()
        with _patch(keyring_obj), \
             mock.patch.object(secret_store, "backend_name", return_value="fake-backend"), \
             redirect_stdout(out):
            code = secret_store._main(["status"])

        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["backend"], "fake-backend")

    def test_get_returns_failure_when_unset(self) -> None:
        keyring_obj = _FakeKeyring()
        with _patch(keyring_obj), redirect_stdout(io.StringIO()):
            self.assertEqual(secret_store._main(["get"]), 1)

    def test_errors_go_to_stderr_with_nonzero_exit(self) -> None:
        err = io.StringIO()
        with mock.patch.object(secret_store, "_load_keyring",
                               side_effect=secret_store.SecretStoreError("密钥库不可用")), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = secret_store._main(["status"])

        self.assertEqual(code, 2)
        self.assertIn("密钥库不可用", err.getvalue())


if __name__ == "__main__":
    unittest.main()
