# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise registration through the public setup command without a cluster."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class RegistrationTest(unittest.TestCase):
    def test_registration_creates_or_preserves_binding_without_patching_helm(self):
        for mode in ("new", "existing", "mismatch", "lookup-error", "site-list-error", "legacy-placeholder", "incomplete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                command = root / "command"
                command.write_text(f"#!{sys.executable}\n" + r'''
import base64, json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
mode = os.environ["MODE"]
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps([name, args]) + "\n")
if name == "sleep":
    sys.exit()
if name == "curl":
    url = next(a for a in args if a.startswith("http"))
    if "openid-connect/token" in url:
        print(json.dumps({"access_token": "fixture-token"}))
    elif url.endswith("/realms/nico-dev"):
        print("{}")
    elif "infrastructure-provider/current" in url:
        print(json.dumps({"id": "provider-a"}))
    elif "tenant/current" in url:
        print("{}")
    elif "site?" in url:
        if mode == "site-list-error": sys.exit(22)
        print(json.dumps([{"id": "site-a", "name": "local-dev-site", "infrastructureProviderId": "provider-a",
                           "registrationToken": None if mode == "existing" else "fixture-otp"}]))
    elif "/site/site-a" in url:
        print(json.dumps({"id": "site-a"}))
    else:
        raise AssertionError(args)
elif name == "kubectl":
    if "rollout" in args or "wait" in args:
        assert "restart" not in args or mode == "legacy-placeholder", args
    elif "exec" in args:
        assert "namespace" in args and "describe" in args, args
    elif "get" in args and "site-registration" in args:
        if mode == "lookup-error": sys.exit(1)
        if mode != "new":
            print(json.dumps({"metadata": {"name": "site-registration"}, "data": {
                "site-uuid": base64.b64encode(("other-site" if mode == "mismatch" else "00000000-0000-4000-8000-000000000001" if mode == "legacy-placeholder" else "site-a").encode()).decode(),
                "otp": "" if mode == "incomplete" else "cm90YXRlZC1vdHA=",
                "creds-url": "aHR0cHM6Ly9leGFtcGxlLmNvbQ==", "cacert": "Y2VydA=="}}))
    elif "get" in args and "site-manager-tls" in args:
        print(base64.b64encode(b"fixture-ca").decode())
    elif ("create" in args or "apply" in args) and "-f" in args:
        content = sys.stdin.read()
        if content.startswith("{"):
            Path(os.environ["CREATED"]).write_text(content)
        else:
            assert mode == "legacy-placeholder", args
            assert 'CLUSTER_ID: "site-a"' in content, content
    elif "get" in args and "configmap" in args and mode == "legacy-placeholder":
        print('data:\n  CLUSTER_ID: placeholder\n  TEMPORAL_SUBSCRIBE_NAMESPACE: placeholder\n  TEMPORAL_SUBSCRIBE_QUEUE: placeholder')
    elif ("delete" in args or "get" in args) and "pod" in args and mode == "legacy-placeholder":
        pass
    elif "get" in args and "pods" in args and mode == "legacy-placeholder":
        pass
    else:
        raise AssertionError(args)
''')
                command.chmod(0o700)
                for name in ("kubectl", "curl", "sleep"):
                    (root / name).symlink_to(command)
                result = subprocess.run(
                    ["bash", str(ROOT / "rest-api/scripts/setup-local.sh"), "site-agent" if mode == "legacy-placeholder" else "register-site"],
                    env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "MODE": mode,
                         "CALLS": str(root / "calls"), "CREATED": str(root / "created"),
                         "RESTART_API": "0"}, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode == 0, mode in ("new", "existing", "legacy-placeholder"), result.stdout + result.stderr)
                if mode in ("new", "legacy-placeholder"):
                    obj = json.loads((root / "created").read_text())
                    self.assertEqual(obj["stringData"]["site-uuid"], "site-a")
                    self.assertEqual(obj["stringData"]["otp"], "fixture-otp")
                else:
                    self.assertFalse((root / "created").exists())
                if (root / "calls").exists():
                    calls = (root / "calls").read_text()
                    if mode != "legacy-placeholder":
                        self.assertNotIn("configmap", calls)
                    self.assertNotIn("fixture-otp", calls)
                self.assertNotIn("fixture-otp", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
