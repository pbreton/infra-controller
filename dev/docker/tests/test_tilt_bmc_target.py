# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import copy
import importlib.util
import json
import subprocess
import sys

import yaml
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
                     'read_yaml': lambda _: {'architecture': values_architecture, 'storage': {'existingClaim': 'data'},
                                             'service': {'clusterIP': '10.96.40.10'}},
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

    def test_helm_configuration_is_accepted_by_the_native_runtime(self):
        lab = ROOT / 'dev/deployment/lab'
        sys.path.insert(0, str(lab))
        try:
            spec = importlib.util.spec_from_file_location('native_target', lab / 'target.py')
            runtime = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runtime)
        finally:
            sys.path.pop(0)
        for hugepages, ipv6 in [(False, ''), (True, ''), (True, 'fd42:8124:40::1/64')]:
            with self.subTest(hugepages=hugepages, ipv6=ipv6):
                result = subprocess.run(
                    ['helm', 'template', 'target-two', str(ROOT / 'helm/nico-bmc-target'),
                     '--namespace', 'test-lab', '--show-only', 'templates/configmap.yaml',
                     '--set', 'nodeName=node,clusterUID=cluster,storage.existingClaim=data,service.clusterIP=10.96.40.10',
                     '--set', f'architecture=arm64,target.instanceIndex=2,target.memoryMiB=8192,target.hugepages={str(hugepages).lower()}',
                     '--set-string', 'network.bridgeAddressV6=' + ipv6],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = runtime.configuration(json.loads(yaml.safe_load(result.stdout)['data']['config.json']))
                self.assertEqual(config['owner'], 'test-lab/target-two')
                self.assertEqual(config['memoryMiB'], 8192)
                self.assertEqual(config['mac'], '02:00:00:02:00:02')
                self.assertEqual(config['architecture'], 'arm64')
                self.assertEqual(config.get('hugepages', False), hugepages)
                self.assertEqual(config.get('bridgeAddressV6', ''), ipv6)


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
        # Tilt joins path components without Python's absolute-path reset.
        tilt_os = SimpleNamespace(path=SimpleNamespace(
            realpath=os.path.realpath, dirname=os.path.dirname, abspath=os.path.abspath,
            join=lambda *parts: os.path.normpath('/'.join(parts))))
        environment = dict(__file__=str(TILTFILE), os=tilt_os, load=Mock(), update_settings=Mock(),
                           k8s_context=lambda: 'kind-lab', fail=fail, custom_build=self.build,
                           helm_resource=self.deploy, read_yaml=lambda path: self.values[path],
                           read_json=lambda path: plan or {'one': 'one.json', 'two': '/lab/two.json'},
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

    def test_single_target_defaults(self):
        self.evaluate({'values': '/lab/one.json'})
        self.deploy.assert_called_once()
        self.assertEqual(self.deploy.call_args.kwargs['release_name'], 'target-one')
        self.assertEqual(self.deploy.call_args.kwargs['namespace'], 'bmc-lab')

    def test_invalid_pair_stops_before_any_build_or_deploy(self):
        cases = [
            ('mixed', {'targets': '/lab/pair.json', 'values': '/lab/one.json'}, None, None),
            ('arity', {'targets': '/lab/pair.json'}, {'one': 'one.json'}, None),
            ('path', {'targets': '/lab/pair.json'}, {'one': 42, 'two': 'two.json'}, None),
            ('release', {'targets': '/lab/pair.json'}, {'1one': 'one.json', 'two': 'two.json'}, None),
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
