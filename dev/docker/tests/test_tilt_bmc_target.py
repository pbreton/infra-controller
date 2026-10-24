# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[3]
TILTFILE = ROOT / 'dev/deployment/lab/Tiltfile'


class TargetWiringTest(unittest.TestCase):
    def evaluate(self, architecture='aarch64', values_architecture='arm64', context='kind-lab'):
        calls = []
        def fail(message):
            raise ValueError(message)
        namespace = {'__file__': str(TILTFILE), 'os': os, 'load': lambda *a: None,
                     'k8s_context': lambda: context, 'fail': fail,
                     'update_settings': lambda **kw: self.assertGreater(kw['k8s_upsert_timeout_secs'], 300),
                     'config': SimpleNamespace(define_string=lambda *a: None,
                         parse=lambda: {'values': '/tmp/target.yaml', 'release': 'custom-target', 'namespace': 'custom-lab'}),
                     'read_yaml': lambda _: {'architecture': values_architecture},
                     'local': lambda *a, **kw: architecture,
                     'custom_build': lambda *a, **kw: calls.append(('build', a, kw)),
                     'helm_resource': lambda *a, **kw: calls.append(('helm', a, kw))}
        exec(compile(TILTFILE.read_text(), str(TILTFILE), 'exec'), namespace)
        return calls

    def test_native_images_and_chart_share_one_build_and_values_contract(self):
        for platform, architecture in [('aarch64', 'arm64'), ('x86_64', 'amd64')]:
            with self.subTest(platform=platform):
                build, chart = self.evaluate(platform, architecture)
                self.assertIn('make images-dev DEV_TARGET=bmc-target', build[2]['command'])
                self.assertEqual(build[2]['dir'], str(ROOT))
                self.assertEqual(chart[2]['release_name'], 'custom-target')
                self.assertEqual(chart[2]['namespace'], 'custom-lab')
                self.assertEqual(chart[2]['image_deps'], [build[1][0]])
                self.assertEqual(chart[2]['flags'][:2], ['--values', '/tmp/target.yaml'])
                self.assertIn('--wait', chart[2]['flags'])

    def test_unsupported_context_or_emulated_target_fails_before_building(self):
        for args in [('x86_64', 'arm64', 'kind-lab'), ('aarch64', 'arm64', 'production')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.evaluate(*args)
