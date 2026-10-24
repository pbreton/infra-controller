# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify registration precedes the Helm workload without competing writers."""

import ast
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class RegistrationWiringTest(unittest.TestCase):
    def test_registration_runs_before_agent_and_does_not_render_placeholder(self):
        source = (ROOT / "dev/deployment/tilt/Tiltfile").read_text()
        tree = ast.parse(source)
        local = next(n for n in tree.body if isinstance(n, ast.Expr)
                     and isinstance(n.value, ast.Call)
                     and getattr(n.value.func, "id", None) == "local_resource"
                     and n.value.args and isinstance(n.value.args[0], ast.Constant)
                     and n.value.args[0].value == "rest-integration")
        agent = next(n for n in tree.body if isinstance(n, ast.Expr)
                     and isinstance(n.value, ast.Call)
                     and getattr(n.value.func, "id", None) == "helm_chart"
                     and any(k.arg == "name" and isinstance(k.value, ast.Constant)
                             and k.value.value == "nico-rest-site-agent" for k in n.value.keywords))
        captured = []
        import os
        values = yaml.safe_load((ROOT / "dev/deployment/tilt/values.yaml").read_text())["tilt"]["rest"]
        exec(compile(ast.Module(body=[local, agent], type_ignores=[]), "Tiltfile", "exec"), {
            "os": os, "rest_charts": "/charts", "rest_namespace": "custom-rest",
            "setup_local": "/repo/scripts/setup-local.sh", "rest_values": values,
            "rest_global": {}, "rest_image_keys": [],
            "component_values": lambda g, v: v,
            "local_resource": lambda *a, **k: captured.append((a, k)),
            "helm_chart": lambda **k: captured.append(((), k)),
        })
        (args, registration), (_, workload) = captured
        self.assertEqual(args[1], ["/repo/scripts/setup-local.sh", "register-site"])
        self.assertEqual(registration["env"]["NAMESPACE"], "custom-rest")
        self.assertNotIn("nico-rest-site-agent", registration["resource_deps"])
        self.assertIn("rest-integration", workload["resource_deps"])
        self.assertTrue(workload["values"]["siteIdentityFromRegistrationSecret"])
        self.assertTrue(workload.get("wait", True))
        self.assertNotIn("'site-registration'", source)
        self.assertNotIn("local_config['site']", source)


if __name__ == "__main__":
    unittest.main()
