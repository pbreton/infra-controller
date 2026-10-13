# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify that the local lint target propagates analyzer failures."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LintGoTest(unittest.TestCase):
    def test_analyzer_exit_status(self):
        for failing_analyzer in (None, "vet", "golangci-lint", "revive"):
            with self.subTest(failing_analyzer=failing_analyzer):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    trace = root / "trace.jsonl"
                    fake_go = root / "go"
                    fake_go.write_text(
                        f"#!{sys.executable}\n"
                        "import json, os, sys\n"
                        "args = sys.argv[1:]\n"
                        "with open(os.environ['LINT_TRACE'], 'a') as f:\n"
                        "    f.write(json.dumps(args) + '\\n')\n"
                        "analyzer = args[1] if args[0] == 'tool' else args[0]\n"
                        "if analyzer == 'list':\n"
                        "    print('example/package')\n"
                        "if analyzer == 'golangci-lint':\n"
                        "    print('[]')\n"
                        "    if '--issues-exit-code' in args and args[args.index('--issues-exit-code') + 1] == '0':\n"
                        "        sys.exit(0)\n"
                        "sys.exit(1 if analyzer == os.environ.get('FAIL_ANALYZER') else 0)\n"
                    )
                    fake_go.chmod(0o755)
                    env = os.environ.copy()
                    env.update(PATH=f"{root}{os.pathsep}{env['PATH']}", LINT_TRACE=str(trace))
                    env["FAIL_ANALYZER"] = failing_analyzer or ""
                    result = subprocess.run(
                        ["make", "lint-go"],
                        cwd=Path(__file__).resolve().parents[1],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode == 0,
                        failing_analyzer is None,
                        result.stdout + result.stderr,
                    )
                    calls = [json.loads(line) for line in trace.read_text().splitlines()]
                    analyzers = [args[1] if args[0] == "tool" else args[0] for args in calls]
                    if failing_analyzer in ("vet", "golangci-lint"):
                        self.assertNotIn("revive", analyzers)
                    if failing_analyzer == "vet":
                        self.assertNotIn("golangci-lint", analyzers)


if __name__ == "__main__":
    unittest.main()
