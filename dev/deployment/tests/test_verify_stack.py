# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "verify_stack", Path(__file__).resolve().parents[1] / "verify-stack.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class InventoryTest(unittest.TestCase):
    def test_only_hosts_count_and_every_host_needs_a_unique_identity(self):
        host = {"device_kind": "machine", "machine_id": "host-a"}
        other = {"device_kind": "switch", "machine_id": "switch-a"}
        self.assertEqual(VERIFY.mat_host_ids({"machines": [host, other]}), {"host-a"})
        for devices in ([other], [host, host], [host, {"device_kind": "machine"}], [{}]):
            with self.subTest(devices=devices), self.assertRaises(VERIFY.NotReady):
                VERIFY.mat_host_ids({"machines": devices})

    def test_core_requires_exact_host_set_and_accepted_states(self):
        host = {"machine_type": 2, "id": "host-a", "state": "Ready"}
        dpu = {"machine_type": 1, "id": "dpu-a", "state": "Ready"}
        self.assertEqual(VERIFY.core_hosts({"machines": [host, dpu]}, {"host-a"}), [host])
        for machines in ([], [host, host], [{**host, "id": "other"}],
                         [{**host, "state": "Initializing"}]):
            with self.subTest(machines=machines), self.assertRaises(VERIFY.NotReady):
                VERIFY.core_hosts({"machines": machines}, {"host-a"})
        assigned = {**host, "state": "Assigned/Ready"}
        self.assertEqual(VERIFY.core_hosts({"machines": [assigned]}, {"host-a"}), [assigned])

    def test_fresh_inventory_needs_final_nonempty_page_and_success(self):
        import json
        def logs(messages, site="site-a"):
            return "\n".join(json.dumps({"Activity": "UpdateMachinesInDB", "Site ID": site,
                                         "msg": message}) for message in messages)
        page = "Received Machine inventory page: 2 of 2, page size: 100, total count: 101"
        complete = ["starting activity", page, "completed activity"]
        self.assertTrue(VERIFY.fresh_inventory(logs(complete), "site-a"))
        for messages in ([page], ["completed activity"],
                         [page, "starting activity", "completed activity"],
                         [page.replace("2 of 2", "1 of 2"), "completed activity"],
                         [page.replace("count: 101", "count: 0"), "completed activity"]):
            with self.subTest(messages=messages):
                self.assertFalse(VERIFY.fresh_inventory(logs(messages), "site-a"))
        self.assertFalse(VERIFY.fresh_inventory(logs(complete, "other-site"), "site-a"))
        self.assertFalse(VERIFY.fresh_inventory("not json\n{}", "site-a"))


class CommandTest(unittest.TestCase):
    def invoke(self, mode="healthy", rest=False, *extra):
        import os
        import subprocess
        import sys
        import tempfile
        import json
        with tempfile.TemporaryDirectory(prefix="stack check ") as temporary:
            root = Path(temporary)
            fake = root / "command"
            fake.write_text(f"#!{sys.executable}\n" + r'''
import base64, json, os, sys, time
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
mode = os.environ["TEST_MODE"]
with open(os.environ["TEST_CALLS"], "a") as log:
    log.write(json.dumps([name, args]) + "\n")
if name == "kubectl":
    if args == ["config", "current-context"]:
        print("kind-selected")
        sys.exit()
    assert args[:3] == ["--context", "kind-selected", "--request-timeout=30s"], args
    assert args[3:5] in (["-n", "core-test"], ["-n", "rest-test"]), args
    args = args[5:]
    if args[:2] == ["get", "service"]:
        print(json.dumps({"spec": {"clusterIP": "fd00::10"}}))
    elif args[0] == "exec":
        assert args[-1] == "https://[fd00::10]:1266/machines/status", args
        if mode == "stalled":
            time.sleep(30)
        print(json.dumps({"machines": [{"device_kind": "machine", "machine_id": "host-a"},
                                      {"device_kind": "switch", "machine_id": "switch-a"}]}))
    elif args[:2] == ["get", "configmap"]:
        print(json.dumps({"data": {"CLUSTER_ID": "site-a"}}))
    elif args[:2] == ["get", "secret"]:
        print(json.dumps({"data": {"site-uuid": base64.b64encode(
            ("other-site" if mode == "wrong-registration" else "site-a").encode()).decode()}}))
    elif args[0] == "logs":
        assert args[-1].startswith("--since-time="), args
        if mode != "stale":
            for msg in ["starting activity", "Received Machine inventory page: 1 of 1, page size: 100, total count: 1", "completed activity"]:
                print(json.dumps({"Activity": "UpdateMachinesInDB", "Site ID": "site-a", "msg": msg}))
    else:
        sys.exit("unexpected kubectl command")
elif name == "bash":
    assert args[0].endswith("/deployment/core-admin.sh"), args
    assert os.environ["LOCAL_DEV_KUBE_CONTEXT"] == "kind-selected"
    assert os.environ["LOCAL_DEV_NAMESPACE"] == "core-test"
    assert os.environ["FORGE_CLIENT_REQUEST_TIMEOUT_SECS"] == "15"
    if args[1:] == ["-f", "json", "machine", "show"]:
        marker = Path(os.environ["TEST_CALLS"] + ".retry")
        fail = mode == "retry" and not marker.exists()
        marker.touch()
        if fail:
            sys.exit(1)
        print(json.dumps({"machines": [{"machine_type": 2, "id": "host-a",
            "state": "Assigned/Ready" if mode == "assigned" else "Ready"}]}))
    elif args[1:] == ["scout-stream", "ping", "host-a"]:
        if mode == "scout-down":
            sys.exit(1)
    else:
        sys.exit("unexpected admin command")
elif name == "curl":
    if "--config" in args:
        assert sys.stdin.read() == 'header = "Authorization: Bearer fixture-token"\n'
    if any("openid-connect" in a for a in args):
        print(json.dumps({"access_token": "fixture-token"}))
    elif any("/site/" in a for a in args):
        print(json.dumps({"id": "site-a", "isOnline": mode != "offline", "status": "Registered"}))
    else:
        assert "--dump-header" in args
        print('HTTP/1.1 200 OK\r\nX-Pagination: {"total": ' + ("101" if mode == "truncated" else "1") + '}\r\n\r\n', end="")
        print(json.dumps([{"controllerMachineId": "wrong" if mode == "wrong-rest" else "host-a",
                           "siteId": "site-a", "isUsableByTenant": mode != "unusable"}]))
else:
    sys.exit("unexpected executable")
''')
            fake.chmod(0o755)
            for command in ("kubectl", "bash", "curl"):
                (root / command).symlink_to(fake)
            calls = root / "calls"
            result = subprocess.run(
                [sys.executable, "-B", str(SPEC.origin), "--core-namespace", "core-test",
                 "--rest-namespace", "rest-test", "--timeout", "15" if mode in ("retry", "healthy", "assigned") else "3",
                 *(["--rest"] if rest else []), *extra],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                     "TEST_MODE": mode, "TEST_CALLS": str(calls)},
                capture_output=True, text=True, timeout=25)
            records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            return result, records

    def test_core_only_and_rest_check_the_selected_context_and_scout(self):
        for rest in (False, True):
            with self.subTest(rest=rest):
                result, calls = self.invoke(rest=rest)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("1 matching MAT/Core hosts", result.stdout)
                self.assertTrue(any("scout-stream" in args for _, args in calls))
                self.assertEqual(any(name == "curl" for name, _ in calls), rest)

    def test_assigned_hosts_do_not_require_scout(self):
        result, calls = self.invoke("assigned", rest=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("scout-stream" in args for _, args in calls))

    def test_failures_do_not_become_readiness(self):
        for mode, reason in (("stale", "fresh completed"), ("offline", "Registered"),
                             ("wrong-rest", "exact usable"), ("unusable", "exact usable"),
                             ("truncated", "inventory total"),
                             ("wrong-registration", "disagree"), ("scout-down", "scout-stream"),
                             ("stalled", "timed out")):
            with self.subTest(mode=mode):
                result, _ = self.invoke(mode, rest=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(reason, result.stdout + result.stderr)
                self.assertNotIn("Stack ready:", result.stdout)

    def test_transient_core_failure_is_retried(self):
        result, calls = self.invoke("retry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sum("show" in args for _, args in calls), 2)

    def test_context_override_and_nonlocal_rejection(self):
        result, calls = self.invoke("healthy", False, "--context", "kind-selected")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(args == ["config", "current-context"] for _, args in calls))
        result, calls = self.invoke("healthy", False, "--context", "production")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(calls, [])


class FrontendTest(unittest.TestCase):
    def test_tilt_gates_selected_profile_and_context(self):
        import os
        source = (Path(SPEC.origin).parent / "tilt/Tiltfile").read_text()
        block = source[source.index("stack_verifier ="):source.index("\nenabled_resources =")]
        for rest in (False, True):
            with self.subTest(rest=rest):
                resources = []
                scope = {"os": os, "repo_root": "/repo with spaces", "enable_rest": rest,
                         "cluster_context": "kind-selected", "core_namespace": "core-test",
                         "rest_namespace": "rest-test", "machine_a_tron_dhcp_pod": "mat-test",
                         "local_resource": lambda *a, **kw: resources.append((a, kw))}
                exec(block, scope)
                (name, command), options = resources[0]
                self.assertEqual(name, "stack-ready")
                self.assertEqual(command[:4], ["python3", "/repo with spaces/dev/deployment/verify-stack.py",
                                               "--context", "kind-selected"])
                self.assertIn("nico-machine-a-tron-mat-test-bmc-mock", command)
                self.assertEqual("--rest" in command, rest)
                self.assertEqual("rest-integration" in options["resource_deps"], rest)
                self.assertIn("'stack-ready'", source[source.index("enabled_resources ="):])

    def test_legacy_setup_delegates_and_propagates_failure(self):
        import os
        import subprocess
        source = (Path(SPEC.origin).parent / "devspace/setup-rest-integration.sh").read_text()
        start = source.index('python3 "${SCRIPT_DIR}/../verify-stack.py"')
        end = source.index('\nif [[ "${DSX_GATEWAY_ENABLED}"', start)
        for status in (0, 42):
            with self.subTest(status=status):
                result = subprocess.run(["bash", "-c", """set -euo pipefail
python3() { printf '<%s>\\n' "$@"; return "$TEST_STATUS"; }
""" + source[start:end] + "\necho verified"],
                    env={**os.environ, "SCRIPT_DIR": "/repo with spaces/dev/deployment/devspace",
                         "CORE_NAMESPACE": "core-test", "REST_NAMESPACE": "rest-test",
                         "MACHINE_A_TRON_BMC_SERVICE": "mat-control", "API_FORWARD_PORT": "18389",
                         "KEYCLOAK_FORWARD_PORT": "18083", "TEST_STATUS": str(status)},
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("<--rest>", result.stdout)
                self.assertIn("<http://localhost:18389>", result.stdout)
                self.assertIn("<http://localhost:18083>", result.stdout)
                self.assertEqual("verified" in result.stdout, status == 0)


if __name__ == "__main__":
    unittest.main()
