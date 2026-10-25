# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tilt/vault-bootstrap.py"
spec = importlib.util.spec_from_file_location("vault_bootstrap", SCRIPT)
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)


def result(output="", error="", code=0):
    return subprocess.CompletedProcess([], code, output, error)


class VaultBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.bootstrap = vault.Bootstrap("kind-test", "nico-system")

    def test_mode_guards_retained_storage_and_live_configuration(self):
        for installed, pvc, selected, fails in (
            (None, False, "ephemeral", False),
            (None, True, "ephemeral", True),
            ("ephemeral", False, "persistent", True),
            ("persistent", True, "ephemeral", True),
            ("persistent", True, "persistent", False),
        ):
            with self.subTest(installed=installed, pvc=pvc, selected=selected):
                env = [{"name": "VAULT_DEV_ROOT_TOKEN_ID"}] if installed == "ephemeral" else []
                sts = {"spec": {"template": {"spec": {"containers": [{"name": "vault", "env": env}]}}}}
                def get(kind, name):
                    return (sts if installed else None) if kind == "statefulset" else (pvc if kind == "pvc" else None)
                with patch.object(self.bootstrap, "get", side_effect=get):
                    if fails:
                        with self.assertRaises(vault.VaultError):
                            self.bootstrap.check_mode(selected)
                    else:
                        self.bootstrap.check_mode(selected)

    def test_ownership_checks_pvc_identity_before_adoption(self):
        self.bootstrap.pvc_uid = "original"
        for owner, pvc, fails in ((vault.OWNER, "original", False),
                                  ("helm", "original", True), (vault.OWNER, "replacement", True)):
            with self.subTest(owner=owner, pvc=pvc):
                secret = {"metadata": {"labels": {vault.LABEL: owner},
                                       "annotations": {vault.PVC_ANNOTATION: pvc}}}
                with patch.object(self.bootstrap, "get", return_value=secret):
                    if fails:
                        with self.assertRaises(vault.VaultError):
                            self.bootstrap.owned_secret(vault.KEYS)
                    else:
                        self.assertIs(self.bootstrap.owned_secret(vault.KEYS), secret)

    def test_missing_or_stale_keys_never_reinitialize(self):
        for initialized, keys in ((True, None), (False, {"data": {}})):
            with self.subTest(initialized=initialized), \
                 patch.object(self.bootstrap, "check_mode"), \
                 patch.object(self.bootstrap, "get", return_value={"metadata": {"uid": "pvc"}, "status": {"phase": "Bound"}}), \
                 patch.object(self.bootstrap, "owned_secret", side_effect=[keys, None]), \
                 patch.object(self.bootstrap, "status", return_value={"initialized": initialized, "sealed": True}), \
                 patch.object(self.bootstrap, "vault") as command:
                with self.assertRaises(vault.VaultError):
                    self.bootstrap.bootstrap()
                command.assert_not_called()

    def test_failed_secret_persistence_keeps_private_recovery_copy(self):
        keys = json.dumps({"root_token": "test-token", "unseal_keys_b64": ["test-key"]})
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(vault.tempfile, "tempdir", directory), \
             patch.object(self.bootstrap, "check_mode"), \
             patch.object(self.bootstrap, "get", return_value={"metadata": {"uid": "pvc"}, "status": {"phase": "Bound"}}), \
             patch.object(self.bootstrap, "owned_secret", return_value=None), \
             patch.object(self.bootstrap, "status", return_value={"initialized": False, "sealed": True}), \
             patch.object(self.bootstrap, "vault", return_value=result(keys)), \
             patch.object(self.bootstrap, "create_secret", side_effect=vault.VaultError("unavailable")):
            with self.assertRaisesRegex(vault.VaultError, "Initialization keys need recovery"):
                self.bootstrap.bootstrap()
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(files[0].read_text(), keys)

    def test_configure_preserves_ca_and_rotated_credentials_but_propagates_other_failures(self):
        for fixture_error, fails in (("check-and-set parameter did not match the current version", False),
                                     ("permission denied", True)):
            with self.subTest(error=fixture_error):
                calls = []
                def command(*args, **kwargs):
                    calls.append((args, kwargs))
                    if args[:2] == ("secrets", "list"):
                        return result(json.dumps({"secrets/": {"type": "kv", "options": {"version": "2"}},
                                                  "certs/": {"type": "pki"}}))
                    if args[:2] == ("kv", "put"):
                        self.assertEqual(args[2], "-cas=0")
                        return result(error=fixture_error, code=2)
                    return result("existing CA")
                with patch.object(self.bootstrap, "vault", side_effect=command):
                    if fails:
                        with self.assertRaises(vault.VaultError):
                            self.bootstrap.configure()
                    else:
                        self.bootstrap.configure()
                self.assertFalse(any("certs/root/generate/internal" in args for args, _ in calls))
                self.assertFalse(any(args[:2] == ("secrets", "enable") for args, _ in calls))

    def test_fresh_issuer_is_created_only_for_an_empty_list(self):
        for response, created, fails in ((result("{}\n", code=2), True, False),
                                         (result(error="permission denied", code=2), False, True)):
            with self.subTest(created=created), patch.object(self.bootstrap, "vault") as command:
                def run(*args, **kwargs):
                    if args[:2] == ("secrets", "list"):
                        return result("{}")
                    if args[0] == "list":
                        return response
                    return result()
                command.side_effect = run
                if fails:
                    with self.assertRaises(vault.VaultError):
                        self.bootstrap.configure()
                else:
                    self.bootstrap.configure()
                self.assertEqual(any("certs/root/generate/internal" in call.args
                                     for call in command.call_args_list), created)

    def test_transport_keeps_token_and_payload_out_of_argv(self):
        self.bootstrap.token = "test-token"
        with patch.object(vault.subprocess, "run", return_value=result()) as run:
            self.bootstrap.vault("write", "sys/unseal", "-", data='{"key":"test-key"}')
        args, kwargs = run.call_args
        self.assertNotIn("test-token", " ".join(args[0]))
        self.assertNotIn("test-key", " ".join(args[0]))
        self.assertEqual(kwargs["input"], 'test-token\n{"key":"test-key"}')
        self.assertTrue(kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
