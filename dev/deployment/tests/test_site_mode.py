# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('site_mode', ROOT / 'dev/deployment/tilt/site-mode.py')
MODE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODE)


class FakeSite(MODE.SiteMode):
    def __init__(self, mode='bmc', marker=None, releases='', core=False):
        super().__init__('kind-test', 'nico-system', mode)
        self.marker, self.releases, self.core = marker, releases, core
        self.writes = []

    def run(self, command, data=None):
        if command[0] == 'helm':
            return self.releases
        if 'create' in command:
            self.marker = json.loads(data)
            self.writes.append(self.marker)
            return ''
        if 'configmap' in command:
            return json.dumps(self.marker) if self.marker else ''
        if 'deployment' in command:
            return '{"metadata": {"name": "nico-api"}}' if self.core else ''
        raise AssertionError(command)


class SiteModeTest(unittest.TestCase):
    def test_read_only_check_then_idempotent_claim_survives_release_removal(self):
        site = FakeSite()
        self.assertFalse(site.check())
        self.assertEqual(site.writes, [])
        site.claim()
        self.assertEqual(len(site.writes), 1)
        self.assertTrue(site.marker['immutable'])
        self.assertEqual(site.marker['metadata']['namespace'], 'kube-system')
        site.claim()
        self.assertEqual(len(site.writes), 1)
        site.mode = 'mat'
        with self.assertRaisesRegex(ValueError, 'Cannot change'):
            site.claim()
        self.assertEqual(len(site.writes), 1)

    def test_unknown_marker_is_not_adopted(self):
        site = FakeSite(marker={'metadata': {'labels': {}}, 'data': {'mode': 'bmc'}})
        with self.assertRaisesRegex(ValueError, 'different owner'):
            site.claim()
        self.assertEqual(site.writes, [])

    def test_legacy_release_adoption_and_interrupted_core_boundary(self):
        for releases, core, accepted in [('', False, True), ('nico-pxe\n', True, True),
                                          ('', True, False), ('nico-machine-a-tron\n', True, False)]:
            with self.subTest(releases=releases, core=core):
                site = FakeSite(releases=releases, core=core)
                if accepted:
                    site.claim()
                    self.assertEqual(len(site.writes), 1)
                else:
                    with self.assertRaises(ValueError):
                        site.claim()
                    self.assertEqual(site.writes, [])

    def test_concurrent_conflicting_claim_is_rejected(self):
        site = FakeSite()
        original = site.kube
        def kube(namespace, *args, data=None):
            if args[0] == 'create':
                site.marker = json.loads(data)
                site.marker['data']['mode'] = 'mat'
                raise ValueError('already exists')
            return original(namespace, *args, data=data)
        site.kube = kube
        with self.assertRaisesRegex(ValueError, 'Cannot change'):
            site.claim()
        self.assertEqual(site.writes, [])
