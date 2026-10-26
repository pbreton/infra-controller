# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise target selection and deployment wiring; native Tilt validates Starlark."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

TILTFILE = Path(__file__).resolve().parents[1] / 'lab/Tiltfile'


def fail(message):
    raise ValueError(message)


class LabTiltTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            '/lab/one.json': dict(architecture='arm64', storage={'existingClaim': 'one-data'},
                                 target={'instanceIndex': 1}, service={'clusterIP': 'fd00::10', 'targetPort': 2443}),
            '/lab/two.json': dict(architecture='arm64', storage={'existingClaim': 'two-data'},
                                 target={'instanceIndex': 2}, service={'clusterIP': 'fd00::11', 'targetPort': 2444}),
        }
        self.build, self.deploy = Mock(), Mock()

    def evaluate(self, options, plan=None):
        environment = dict(__file__=str(TILTFILE), os=os, load=Mock(), update_settings=Mock(),
                           k8s_context=lambda: 'kind-lab', fail=fail, custom_build=self.build,
                           helm_resource=self.deploy, read_yaml=lambda path: self.values[path],
                           read_json=lambda path: plan or {'one': 'one.json', 'two': 'two.json'},
                           local=lambda *args, **kwargs: 'aarch64',
                           type=lambda value: 'dict' if isinstance(value, dict) else 'string' if isinstance(value, str) else 'other',
                           config=SimpleNamespace(define_string=Mock(), parse=lambda: options))
        exec(TILTFILE.read_text(), environment)

    def test_pair_shares_build_and_preserves_direct_helm_values(self):
        self.evaluate({'targets': '/lab/pair.json', 'namespace': 'chosen'})
        self.build.assert_called_once()
        self.assertEqual([call.args[0] for call in self.deploy.call_args_list], ['one', 'two'])
        for call, name in zip(self.deploy.call_args_list, ['one', 'two']):
            self.assertEqual(call.kwargs['namespace'], 'chosen')
            self.assertEqual(call.kwargs['flags'][:2], ['--values', '/lab/' + name + '.json'])
            self.assertEqual(call.kwargs['image_deps'], ['nico-bmc-target'])

    def test_existing_single_target_interface_is_preserved(self):
        self.evaluate({'values': '/lab/one.json', 'release': 'selected'})
        self.deploy.assert_called_once()
        self.assertEqual(self.deploy.call_args.kwargs['release_name'], 'selected')
        self.assertEqual(self.deploy.call_args.kwargs['namespace'], 'bmc-lab')

    def test_invalid_pair_stops_before_any_build_or_deploy(self):
        cases = [
            ('mixed', {'targets': '/lab/pair.json', 'values': '/lab/one.json'}, None, None),
            ('arity', {'targets': '/lab/pair.json'}, {'one': 'one.json'}, None),
            ('path', {'targets': '/lab/pair.json'}, {'one': 42, 'two': 'two.json'}, None),
            ('release', {'targets': '/lab/pair.json'}, {'../one': 'one.json', 'two': 'two.json'}, None),
            ('architecture', {'targets': '/lab/pair.json'}, None, ('architecture', 'amd64')),
            ('claim', {'targets': '/lab/pair.json'}, None, ('storage', {'existingClaim': 'one-data'})),
            ('index', {'targets': '/lab/pair.json'}, None, ('target', {'instanceIndex': 1})),
            ('bmc', {'targets': '/lab/pair.json'}, None, ('service', {'clusterIP': 'fd00::10', 'targetPort': 2444})),
            ('port', {'targets': '/lab/pair.json'}, None, ('service', {'clusterIP': 'fd00::11', 'targetPort': 2443})),
        ]
        original = copy.deepcopy(self.values)
        for name, options, plan, change in cases:
            with self.subTest(name=name):
                self.values = copy.deepcopy(original)
                if change:
                    self.values['/lab/two.json'][change[0]] = change[1]
                with self.assertRaises(ValueError):
                    self.evaluate(options, plan)
                self.build.assert_not_called()
                self.deploy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
