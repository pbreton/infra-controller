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
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "helm_json_flags"]
        captured = []
        exec(compile(ast.Module(body=[*helpers, *configuration, call], type_ignores=[]), "Tiltfile", "exec"), {
            "helm_resource": lambda **kwargs: captured.append(kwargs),
            "os": os, "repo_root": str(ROOT),
            "values_file": str(ROOT / "dev/deployment/tilt/values.yaml"),
            "enable_rest": False, "rest_namespace": "nico-rest", "temporal_namespace": "temporal",
            "rest_global": {"certificate": {"issuerRef": {"name": "nico-rest-ca-issuer"}}},
            "encode_json": json.dumps, "type": lambda value: type(value).__name__,
            "read_yaml": lambda path: yaml.safe_load(Path(path).read_text()),
        })
        resource = captured[0]
        self.assertNotIn("nico-local-namespaces", resource["resource_deps"])
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



    def test_rest_trust_preserves_certificate_contract_and_forwards_namespaces(self):
        tree = ast.parse((ROOT / "dev/deployment/tilt/Tiltfile").read_text())
        call = next(node for node in tree.body if isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "helm_resource"
                    and any(k.arg == "name" and isinstance(k.value, ast.Constant)
                            and k.value.value == "nico-core-prereqs" for k in node.value.keywords))
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "helm_json_flags"]
        captured = []
        exec(compile(ast.Module(body=[*helpers, call], type_ignores=[]), "Tiltfile", "exec"), {
            "helm_resource": lambda **kwargs: captured.append(kwargs),
            "os": os, "repo_root": str(ROOT), "core_namespace": "nico-system",
            "prereqs_values_file": str(ROOT / "helm-prereqs/values/local-development.yaml"),
            "values_file": str(ROOT / "dev/deployment/tilt/values.yaml"),
            "enable_rest": True, "rest_namespace": "custom-rest", "temporal_namespace": "custom-temporal",
            "rest_global": {"certificate": {"issuerRef": {"name": "custom-rest-ca"}}},
            "encode_json": json.dumps, "type": lambda value: type(value).__name__,
        })
        resource = captured[0]
        self.assertIn("nico-local-namespaces", resource["resource_deps"])
        self.assertIn("cert-manager", resource["resource_deps"])
        self.assertIn(str(ROOT / "dev/deployment/tilt/values.yaml"), resource["deps"])
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
        by_identity = {(r["kind"], r["metadata"].get("namespace"), r["metadata"]["name"]): r
                       for r in resources}
        self.assertEqual(len(resources), len(by_identity))
        self.assertEqual(len(resources), 14)
        for namespace in ("cert-manager", "custom-rest"):
            ca = by_identity[("Secret", namespace, "ca-signing-secret")]
            self.assertEqual(ca["type"], "kubernetes.io/tls")
            self.assertEqual(ca["stringData"], {
                "tls.crt": (ROOT / "dev/certs/localhost/ca.crt").read_text(),
                "tls.key": (ROOT / "dev/certs/localhost/ca.key").read_text(),
            })
        issuer = by_identity[("ClusterIssuer", None, "custom-rest-ca")]
        self.assertEqual(issuer["spec"], {"ca": {"secretName": "ca-signing-secret"}})
        # Preserve the entire existing TLS contract, adjusting only the explicit
        # namespace and issuer inputs. This covers every SAN and mounted Secret.
        old_certificates = yaml.safe_load_all((ROOT /
            "rest-api/deploy/kustomize/base/temporal-helm/certificates.yaml").read_text())
        for old in old_certificates:
            expected = old["spec"]
            expected["issuerRef"]["name"] = "custom-rest-ca"
            expected["dnsNames"] = [".".join("custom-temporal" if part == "temporal" else part
                                             for part in name.split("."))
                                    if ".server.temporal.local" not in name else name
                                    for name in expected["dnsNames"]]
            actual = by_identity[("Certificate", "custom-temporal", old["metadata"]["name"])]
            self.assertEqual(actual["spec"], expected)
        local_objects = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                             and any(isinstance(t, ast.Name) and t.id == "local_objects" for t in node.targets))
        self.assertNotIn("ca-signing-secret", [n.value for n in ast.walk(local_objects)
                                              if isinstance(n, ast.Constant)])
        self.assertNotIn("deploy/kustomize/base/temporal-helm/certificates.yaml",
                         [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)])
        ready = next(node.value for node in tree.body if isinstance(node, ast.Expr)
                     and isinstance(node.value, ast.Call)
                     and getattr(node.value.func, "id", None) == "local_resource"
                     and node.value.args and isinstance(node.value.args[0], ast.Constant)
                     and node.value.args[0].value == "temporal-certificates-ready")
        self.assertEqual(ast.literal_eval(next(k.value for k in ready.keywords
                                               if k.arg == "resource_deps")), ["nico-core-prereqs"])

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
