# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check Tilt prerequisite inputs against the shared Helm chart contract."""

import ast
import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class LocalPrerequisitesTest(unittest.TestCase):
    def test_profile_preserves_core_contract_without_site_bootstrap_writers(self):
        ca = ROOT / "dev/certs/localhost/ca.crt"
        key = ROOT / "dev/certs/localhost/ca.key"
        ssh_key = ROOT / "crates/ssh-console/tests/fixtures/ssh_host_ed25519_key"
        # Evaluate the prerequisite configuration and Helm call without running
        # Tilt. Actual Helm rendering catches frontend/chart input drift.
        tree = ast.parse((ROOT / "dev/deployment/tilt/Tiltfile").read_text())
        calls = [node for node in tree.body if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call)
                 and getattr(node.value.func, "id", None) == "helm_resource"]
        call = next(node for node in calls if any(
            k.arg == "name" and isinstance(k.value, ast.Constant)
            and k.value.value == "nico-core-prereqs" for k in node.value.keywords
        ))
        configuration = [node for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id in (
                             "prereqs_values_file", "prereqs_values", "core_namespace",
                         ) for target in node.targets)]
        captured = []
        exec(compile(ast.Module(body=[*configuration, call], type_ignores=[]), "Tiltfile", "exec"), {
            "helm_resource": lambda **kwargs: captured.append(kwargs),
            "os": os, "repo_root": str(ROOT),
            "read_yaml": lambda path: yaml.safe_load(Path(path).read_text()),
        })
        resource = captured[0]
        # Remove only installation controls; preserve all value-affecting flags.
        render_flags = [flag for flag in resource["flags"]
                        if flag not in ("--create-namespace", "--wait")
                        and not flag.startswith("--timeout=")]
        result = subprocess.run(
            ["helm", "template", resource["release_name"], resource["chart"],
             "--namespace", resource["namespace"], *render_flags],
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
        self.assertEqual(by_name["ssh-host-key"]["stringData"], {
            "ssh_host_ed25519_key": ssh_key.read_text(),
            "ssh_host_ed25519_key_pub": Path(str(ssh_key) + ".pub").read_text(),
        })


if __name__ == "__main__":
    unittest.main()
