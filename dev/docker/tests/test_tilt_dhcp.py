# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check the Tilt DHCP image contract through the deployed Helm configuration."""

import json
from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]


class TiltDhcpTest(unittest.TestCase):
    def test_local_network_is_fully_rendered_by_helm(self):
        values = yaml.safe_load((ROOT / "dev/deployment/tilt/values.yaml").read_text())
        resources = []
        for component in ("nico-dhcp", "nico-machine-a-tron"):
            result = subprocess.run(
                ["helm", "template", component, str(ROOT / "helm/charts" / component),
                 "--namespace", "nico-system", "--values", "-"],
                input=yaml.safe_dump({"global": values["global"], **values[component]}),
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            resources.extend(r for r in yaml.safe_load_all(result.stdout) if r)
        services = {r["metadata"]["name"]: r["spec"] for r in resources if r["kind"] == "Service"}
        configs = {r["metadata"]["name"]: r["data"] for r in resources if r["kind"] == "ConfigMap"}
        server = services["nico-dhcp"]
        relay = services["nico-machine-a-tron-mat-0-dhcp-relay"]
        mat = configs["nico-machine-a-tron-mat-0-config-files"]["mat.toml"]
        kea = json.loads(configs["nico-dhcp-config"]["kea_config.json"])["Dhcp4"]
        identifier = next(o["data"] for o in kea["option-data"] if o["name"] == "dhcp-server-identifier")
        self.assertEqual(identifier, server["clusterIP"])
        self.assertIn('server_address = "' + server["clusterIP"] + ':' + str(server["ports"][0]["port"]) + '"', mat)
        self.assertIn('advertise_address = "' + relay["clusterIP"] + '"', mat)
        self.assertIn('listen_address = "0.0.0.0:' + str(relay["ports"][0]["port"]) + '"', mat)
        self.assertNotRegex(mat, r"__(NICO_DHCP|MAT_DHCP)_")
        ssh = services["nico-machine-a-tron-mat-0-ssh"]
        self.assertEqual(ssh["clusterIP"], "None")
        self.assertEqual(ssh["selector"], relay["selector"])
        console = values["nico-ssh-console-rs"]["configFiles"]["config"]
        self.assertIn('override_bmc_ssh_host = "nico-machine-a-tron-mat-0-ssh.nico-system.svc.cluster.local"', console)
        deployment = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "nico-machine-a-tron-mat-0")
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        self.assertTrue(all(labels.get(k) == v for k, v in relay["selector"].items()))
        ports = deployment["spec"]["template"]["spec"]["containers"][0]["ports"]
        target = next(p for p in ports if p["name"] == relay["ports"][0]["targetPort"])
        self.assertEqual(target["containerPort"], relay["ports"][0]["port"])

    def test_tilt_values_render_the_shared_hook_for_both_dhcp_families(self):
        tilt_values = yaml.safe_load((ROOT / "dev/deployment/tilt/values.yaml").read_text())
        values = {"global": tilt_values["global"], **tilt_values["nico-dhcp"]}
        # IPv6 is opt-in; use its required stable server identifier for this render.
        values.setdefault("dhcp", {}).update(
            v6Enabled=True, v6ServerIdentifier="020000000001",
        )
        result = subprocess.run(
            ["helm", "template", "nico-dhcp", str(ROOT / "helm/charts/nico-dhcp"),
             "--namespace", yaml.safe_load((ROOT / "helm-prereqs/values/local-development.yaml").read_text())["namespace"],
             "--values", "-", "--show-only", "templates/configmap.yaml",
             "--show-only", "templates/configmap-v6.yaml"],
            input=yaml.safe_dump(values), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        configs = {
            filename: json.loads(content)
            for resource in yaml.safe_load_all(result.stdout)
            for filename, content in resource["data"].items()
        }
        self.assertEqual(set(configs), {"kea_config.json", "kea_config6.json"})
        for filename, family in (("kea_config.json", "Dhcp4"), ("kea_config6.json", "Dhcp6")):
            with self.subTest(family=family):
                self.assertEqual(
                    configs[filename][family]["hooks-libraries"][0]["library"],
                    "/usr/lib/kea/hooks/libdhcp.so",
                )


if __name__ == "__main__":
    unittest.main()
