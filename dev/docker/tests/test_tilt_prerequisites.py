# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render local Core prerequisites independently of the development frontend."""

from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class LocalPrerequisitesTest(unittest.TestCase):
    def test_profile_preserves_core_contract_without_site_bootstrap_writers(self):
        profile = ROOT / "helm-prereqs/values/local-development.yaml"
        ca = ROOT / "dev/certs/localhost/ca.crt"
        key = ROOT / "dev/certs/localhost/ca.key"
        ssh_key = ROOT / "crates/ssh-console/tests/fixtures/ssh_host_ed25519_key"
        result = subprocess.run(
            ["helm", "template", "nico-core-prereqs", str(ROOT / "helm-prereqs"),
             "--namespace", "nico-system", "--values", str(profile),
             "--set-file", f"certManager.localCA.certificate={ca}",
             "--set-file", f"certManager.localCA.privateKey={key}",
             "--set-file", f"sshHostKey.privateKey={ssh_key}"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = list(filter(None, yaml.safe_load_all(result.stdout)))
        by_name = {r["metadata"]["name"]: r for r in resources}
        self.assertEqual(set(by_name), {
            "nico-local-ca", "nico-roots", "local-ca-issuer",
            "ssh-host-key", "nico-vault-token", "nico-vault-approle-tokens",
            "vault-cluster-info", "nico-system-nico-database-config",
        })
        self.assertEqual(len(resources), len(by_name))
        for resource in resources:
            self.assertEqual(resource["metadata"]["namespace"], "nico-system")
            self.assertNotIn("helm.sh/hook", resource["metadata"].get("annotations", {}))
        self.assertEqual(by_name["nico-local-ca"]["stringData"], {
            "tls.crt": ca.read_text(), "tls.key": key.read_text(),
        })
        self.assertEqual(by_name["nico-roots"]["stringData"], {"ca.crt": ca.read_text()})
        issuer = by_name["local-ca-issuer"]
        self.assertEqual(issuer["kind"], "Issuer")
        self.assertEqual(issuer["spec"]["ca"]["secretName"], "nico-local-ca")
        tilt = yaml.safe_load((ROOT / "dev/deployment/tilt/values.yaml").read_text())
        self.assertEqual(tilt["global"]["certificate"]["issuerRef"]["name"], issuer["metadata"]["name"])
        self.assertEqual(by_name["vault-cluster-info"]["data"], {
            "VAULT_SERVICE": "http://nico-vault.nico-system.svc.cluster.local:8200",
            "NICO_VAULT_MOUNT": "secrets", "NICO_VAULT_PKI_MOUNT": "certs",
        })
        self.assertEqual(by_name["nico-system-nico-database-config"]["data"], {
            "DB_HOST": "nico-pg-cluster-rw.nico-system.svc.cluster.local",
            "DB_PORT": "5432", "DB_NAME": "nico_system_nico",
        })
        self.assertEqual(by_name["nico-vault-token"]["stringData"], {"token": "root"})
        self.assertEqual(by_name["nico-vault-approle-tokens"]["stringData"], {
            "VAULT_ROLE_ID": "", "VAULT_SECRET_ID": "",
        })
        self.assertEqual(by_name["ssh-host-key"]["stringData"]["ssh_host_ed25519_key"], ssh_key.read_text())


if __name__ == "__main__":
    unittest.main()
