# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify shared Helm ownership and parity with the retained Kustomize workflow."""

import ast
import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
KEYCLOAK = ROOT / "rest-api/deploy/kustomize/base/keycloak"


class KeycloakTest(unittest.TestCase):
    def test_tilt_forwards_inputs_and_preserves_workload_and_realm_contracts(self):
        tree = ast.parse((ROOT / "dev/deployment/tilt/Tiltfile").read_text())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "helm_json_flags"]
        configuration = [node for node in tree.body if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id in (
                             "keycloak_realm_file", "keycloak_chart_values",
                         ) for target in node.targets)]
        overrides = [node for node in tree.body if isinstance(node, ast.If)
                     and any(isinstance(n, ast.Name) and n.id == "keycloak_values"
                             for n in ast.walk(node.test))]
        call = next(node for node in tree.body if isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", None) == "helm_resource"
                    and any(k.arg == "name" and isinstance(k.value, ast.Constant)
                            and k.value.value == "keycloak" for k in node.value.keywords))
        captured = []
        inputs = {"image": "example.invalid/keycloak@sha256:" + "a" * 64,
                  "databaseUrl": "jdbc:postgresql://custom-db:5432/auth"}
        exec(compile(ast.Module(body=[*helpers, *configuration, *overrides, call], type_ignores=[]), "Tiltfile", "exec"), {
            "os": os, "repo_root": str(ROOT), "rest_api_dir": str(ROOT / "rest-api"),
            "keycloak_values": inputs,
            "keycloak_database": {"user": "auth-user", "password": "auth-password"},
            "values_file": str(ROOT / "dev/deployment/tilt/values.yaml"),
            "rest_namespace": "custom-rest", "read_file": lambda p: Path(p).read_text(),
            "helm_resource": lambda **kwargs: captured.append(kwargs),
            "port_forward": lambda local, remote: (local, remote),
            "encode_json": json.dumps, "type": lambda value: type(value).__name__,
        })
        resource = captured[0]
        self.assertEqual(resource["resource_deps"], ["rest-databases-ready", "nico-local-config"])
        self.assertEqual(resource["port_forwards"], [(18082, 8080)])
        self.assertIn(str(KEYCLOAK / "nico-dev-realm.json"), resource["deps"])
        flags = [flag for flag in resource["flags"]
                 if flag != "--wait" and not flag.startswith("--timeout=")]
        output = subprocess.check_output([
            "helm", "template", resource["release_name"], resource["chart"],
            "--namespace", resource["namespace"], *flags], text=True)
        objects = list(filter(None, yaml.safe_load_all(output)))
        by_identity = {(o["kind"], o["metadata"]["name"]): o for o in objects}
        self.assertEqual(len(objects), 5)
        self.assertEqual(len(by_identity), 5)
        for obj in objects:
            self.assertEqual(obj["metadata"]["namespace"], "custom-rest")
            self.assertNotIn("helm.sh/hook", obj["metadata"].get("annotations", {}))
        secret = by_identity[("Secret", "keycloak-db-credentials")]
        self.assertEqual(secret["stringData"], {"username": "auth-user", "password": "auth-password"})
        # The two frontends retain the same public Service/Deployment contract.
        legacy = list(yaml.safe_load_all(subprocess.check_output([
            "kubectl", "kustomize", str(KEYCLOAK)], text=True)))
        for obj in legacy:
            obj["metadata"]["namespace"] = "custom-rest"
            if obj["kind"] == "Deployment":
                # Credential-triggered rollouts are protected separately below.
                obj["spec"]["template"]["metadata"]["annotations"] = by_identity[("Deployment", "keycloak")]["spec"]["template"]["metadata"]["annotations"]
                pod = obj["spec"]["template"]["spec"]
                pod.pop("imagePullSecrets")
                container = pod["containers"][0]
                container["image"] = inputs["image"]
                for env in container["env"]:
                    if env["name"] == "KC_DB_URL":
                        env["value"] = inputs["databaseUrl"]
                    if env["name"] in ("KC_DB_USERNAME", "KC_DB_PASSWORD"):
                        key = "username" if env["name"] == "KC_DB_USERNAME" else "password"
                        env.pop("value")
                        env["valueFrom"] = {"secretKeyRef": {"name": "keycloak-db-credentials", "key": key}}
            self.assertEqual(obj, by_identity[(obj["kind"], obj["metadata"]["name"])])
        imported = by_identity[("ConfigMap", "keycloak-realm")]["data"]["nico-dev-realm.json"]
        self.assertEqual(json.loads(imported), json.loads((KEYCLOAK / "nico-dev-realm.json").read_text()))
        self.assertNotIn("keycloak_objects", [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)])


    def test_database_password_change_rolls_workload_without_inline_credentials(self):
        def deployment(password):
            output = subprocess.check_output([
                "helm", "template", "keycloak", str(ROOT / "helm/nico-dev-keycloak"),
                "--set-json", "database.password=" + json.dumps(password)], text=True)
            return next(o for o in yaml.safe_load_all(output) if o["kind"] == "Deployment")
        before = deployment("original-password")
        after = deployment("replacement-password")
        before_hash = before["spec"]["template"]["metadata"]["annotations"].pop("checksum/database-credentials")
        after_hash = after["spec"]["template"]["metadata"]["annotations"].pop("checksum/database-credentials")
        self.assertNotEqual(before_hash, after_hash)
        self.assertEqual(before, after)
        self.assertNotIn("replacement-password", json.dumps(after))


if __name__ == "__main__":
    unittest.main()
