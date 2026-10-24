# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check Tilt prerequisite inputs against the shared Helm chart contract."""

import ast
import json
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


    def test_rest_common_owns_secrets_and_certificate_with_forwarded_inputs(self):
        tree = ast.parse((ROOT / "dev/deployment/tilt/Tiltfile").read_text())
        call = next(node for node in tree.body if isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "helm_resource"
                    and any(k.arg == "name" and isinstance(k.value, ast.Constant)
                            and k.value.value == "nico-rest-common" for k in node.value.keywords))
        configuration = [node for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "rest_common_values"
                                 for target in node.targets)]
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in ("merged", "component_values", "helm_json_flags")]
        tilt = yaml.safe_load((ROOT / "dev/deployment/tilt/values.yaml").read_text())["tilt"]
        # Distinct inputs prove forwarding from the frontend, beyond chart defaults.
        captured = []
        exec(compile(ast.Module(body=[*helpers, *configuration, call], type_ignores=[]), "Tiltfile", "exec"), {
            "helm_resource": lambda **kwargs: captured.append(kwargs),
            "os": os, "rest_charts": str(ROOT / "helm/rest"),
            "values_file": str(ROOT / "dev/deployment/tilt/values.yaml"),
            "rest_namespace": "custom-rest", "rest_global": tilt["rest"]["global"],
            "rest_database": {"user": "custom-user", "password": "custom-db-value"},
            "local_config": {"keycloak": {"clientSecret": "custom-client-value"},
                             "temporal": {"encryptionKey": "custom-encryption-value"}},
            "encode_json": json.dumps, "type": lambda value: type(value).__name__,
        })
        resource = captured[0]
        self.assertEqual(resource["pod_readiness"], "ignore")
        self.assertIn("nico-local-config", resource["resource_deps"])
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
        names = {"db-creds", "keycloak-client-secret", "temporal-encryption-key",
                 "image-pull-secret", "temporal-client-cloud-cert"}
        self.assertEqual(set(by_name), names)
        self.assertEqual(len(resources), len(names))
        for obj in resources:
            self.assertEqual(obj["metadata"]["namespace"], "custom-rest")
            self.assertNotIn("helm.sh/hook", obj["metadata"].get("annotations", {}))
        self.assertEqual(by_name["db-creds"]["stringData"], {
            "username": "custom-user", "password": "custom-db-value"})
        self.assertEqual(by_name["keycloak-client-secret"]["stringData"], {
            "keycloak-client-secret": "custom-client-value"})
        self.assertEqual(by_name["temporal-encryption-key"]["stringData"], {
            "temporal-encryption-key": "custom-encryption-value"})
        self.assertEqual(by_name["temporal-client-cloud-cert"]["spec"]["issuerRef"],
                         tilt["rest"]["global"]["certificate"]["issuerRef"])
        # The raw frontend list must not remain a second writer for these Secrets.
        local_objects = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "local_objects" for t in node.targets))
        owned_literals = [n.value for n in ast.walk(local_objects) if isinstance(n, ast.Constant)]
        self.assertFalse((names - {"db-creds"}).intersection(owned_literals))
        rest_secrets = [n.args[0].value for n in ast.walk(local_objects)
                        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "secret"
                        and len(n.args) > 1 and isinstance(n.args[1], ast.Name)
                        and n.args[1].id == "rest_namespace"]
        self.assertNotIn("db-creds", rest_secrets)
        database = next(node.value for node in tree.body if isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Call)
                        and getattr(node.value.func, "id", None) == "k8s_resource"
                        and node.value.args and isinstance(node.value.args[0], ast.Constant)
                        and node.value.args[0].value == "nico-rest-db-migration-dev")
        dependencies = next(k.value for k in database.keywords if k.arg == "resource_deps")
        self.assertIn("nico-rest-common", ast.literal_eval(dependencies))
        certificate_ready = next(node.value for node in tree.body if isinstance(node, ast.Expr)
                                 and isinstance(node.value, ast.Call)
                                 and getattr(node.value.func, "id", None) == "local_resource"
                                 and node.value.args and isinstance(node.value.args[0], ast.Constant)
                                 and node.value.args[0].value == "rest-certificate-ready")
        dependencies = next(k.value for k in certificate_ready.keywords if k.arg == "resource_deps")
        self.assertEqual(ast.literal_eval(dependencies), ["nico-rest-common"])
        self.assertNotIn("deploy/kustomize/base/common/temporal-client-cloud-cert.yaml",
                         [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)])


if __name__ == "__main__":
    unittest.main()
