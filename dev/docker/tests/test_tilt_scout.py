# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import ast
import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
TILT = ROOT / 'dev/deployment/tilt/Tiltfile'


class ScoutWiringTest(unittest.TestCase):
    def profile(self, bmc, arch='aarch64', ipv6_endpoints=False):
        tree = ast.parse(TILT.read_text())
        calls = []
        values = yaml.safe_load((TILT.parent / 'values.yaml').read_text())
        selected = [n for n in tree.body if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Name) and n.test.id == 'bmc_lab'
                    and any(isinstance(x, ast.Call) and getattr(x.func, 'id', '') == 'helm_chart'
                            for x in ast.walk(n))]
        env = dict(bmc_lab=bmc, ipv6_endpoints=ipv6_endpoints, nico_api_values=values['nico-api'], all_values=values,
                   core_global=values['global'], core_namespace='nico-system',
                   core_charts=str(ROOT / 'helm/charts'), tilt_dir=str(TILT.parent), os=os,
                   read_file=lambda p: Path(p).read_text(), compiler_arch=arch,
                   boot_artifact_image='native-artifacts:test',
                   component_values=lambda a, b: dict(global_values=a, **b),
                   helm_chart=lambda **kw: calls.append(kw))
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'Tiltfile', 'exec'), env)
        return calls, values

    def test_real_scout_profile_renders_firmware_and_metadata_endpoints(self):
        for arch in ('aarch64', 'x86_64'):
            with self.subTest(arch=arch):
                calls, values = self.profile(True, arch)
                pxe = calls[0]
                self.assertEqual(pxe['resource_deps'], ['nico-api'])
                self.assertEqual(pxe['image_deps'], ['nico-pxe'])
                settings = pxe['values']
                settings['global'] = values['global']
                result = subprocess.run(['helm', 'template', 'nico-pxe', str(ROOT / 'helm/charts/nico-pxe'),
                                         '-n', 'nico-system', '-f', '-'], input=yaml.safe_dump(settings),
                                        capture_output=True, text=True, check=True, timeout=30)
                resources = list(yaml.safe_load_all(result.stdout))
                service = next(x for x in resources if x['kind'] == 'Service' and x['metadata']['name'] == 'nico-pxe')
                dhcp = values['nico-dhcp']['config']['kea']['hookParameters']
                self.assertEqual(service['spec']['clusterIP'], dhcp['provisioningServer'])
                self.assertEqual([p['port'] for p in service['spec']['ports']], [8080, 80])
                config = next(x for x in resources if x['kind'] == 'ConfigMap')['data']
                self.assertEqual(config['CARBIDE_API_URL'], 'https://nico-api.nico-system.svc.cluster.local:1079')
                deployment = next(x for x in resources if x['kind'] == 'Deployment')
                init = deployment['spec']['template']['spec']['initContainers'][0]
                self.assertEqual(init['image'], 'native-artifacts:test')
                self.assertIn('/' + arch, init['command'][-1])
                core = values['nico-api']
                self.assertEqual(core['extraEnv'], [])
                auth = tomllib.loads(core['siteConfig']['nicoApiSiteConfig'])['auth']
                self.assertFalse(auth['permissive_mode'])
                self.assertFalse(auth['allow_machineatron_scout_stream'])
                self.assertEqual(init['imagePullPolicy'], 'IfNotPresent')

    def test_default_mat_configuration_is_preserved(self):
        calls, values = self.profile(False)
        self.assertEqual(calls, [])
        self.assertEqual(values['nico-api']['extraEnv'][0]['value'], '1')
        self.assertEqual(values['nico-dhcp']['config']['kea']['hookParameters']['provisioningServer'], '127.0.0.1')

    def test_incomplete_bmc_mode_rejected_before_deployment(self):
        tree = ast.parse(TILT.read_text())
        guards = [n for n in tree.body if isinstance(n, ast.If)
                  and any(isinstance(x, ast.Constant) and isinstance(x.value, str)
                          and 'requires --' in x.value and ('bmc-lab' in x.value or 'boot-artifact' in x.value)
                          for x in ast.walk(n))]
        def fail(message):
            raise ValueError(message)
        for bmc, vault, artifact in [(True, False, 'image'), (True, True, ''), (False, True, 'image')]:
            with self.subTest(bmc=bmc, vault=vault, artifact=artifact), self.assertRaises(ValueError):
                exec(compile(ast.Module(body=guards, type_ignores=[]), 'Tiltfile', 'exec'),
                     dict(bmc_lab=bmc, dhcpv6=False, persistent_vault=vault, boot_artifact_image=artifact, fail=fail))

    def test_dhcpv6_chart_enablement_requires_bmc_and_a_dual_stack_node(self):
        tree = ast.parse(TILT.read_text())
        selected = [n for n in tree.body if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Name) and n.test.id == 'dhcpv6']
        def fail(message):
            raise ValueError(message)
        for bmc, cidrs, error, initial in [(False, ['10.244.0.0/24', 'fd00:10::/64'], '--bmc-lab', {}),
                                          (True, ['10.244.0.0/24'], 'dual-stack', {}),
                                          (True, ['10.244.0.0/24', 'fd00:10::/64'], '', {}),
                                          (True, ['fd00:10::/64'], '', {'dhcp': None})]:
            values = {'nico-dhcp': initial}
            env = dict(dhcpv6=True, bmc_lab=bmc, cluster_context='kind-lab', all_values=values,
                       fail=fail, decode_json=json.loads,
                       local=lambda *a, **kw: json.dumps({'items': [{'spec': {'podCIDRs': cidrs}}]}))
            with self.subTest(bmc=bmc, cidrs=cidrs):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        exec(compile(ast.Module(body=selected, type_ignores=[]), 'Tiltfile', 'exec'), env)
                    self.assertEqual(values, {'nico-dhcp': {}})
                else:
                    exec(compile(ast.Module(body=selected, type_ignores=[]), 'Tiltfile', 'exec'), env)
                    self.assertEqual(values['nico-dhcp']['dhcp'],
                                     {'v6Enabled': True, 'v6ServerIdentifier': '020000000006'})

    def test_dhcpv6_enablement_preserves_configured_resolver_and_lease_timing(self):
        tree = ast.parse(TILT.read_text())
        selected = [n for n in tree.body if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Name) and n.test.id == 'dhcpv6']
        values = yaml.safe_load((TILT.parent / 'values.yaml').read_text())
        values['nico-dhcp']['dhcp'] = {
            'v6Enabled': False, 'v6DnsServers': 'fd00:84::1',
            'v6ServerIdentifier': '020000001234',
            'v6LeaseTimers': {'preferredLifetime': 120, 'validLifetime': 180,
                              'renewTimer': 30, 'rebindTimer': 60},
        }
        env = dict(dhcpv6=True, bmc_lab=True, cluster_context='kind-lab', all_values=values,
                   decode_json=json.loads,
                   local=lambda *a, **kw: json.dumps({'items': [{'spec': {'podCIDRs': ['fd00:10::/64']}}]}))
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'Tiltfile', 'exec'), env)
        result = subprocess.run(
            ['helm', 'template', 'nico-dhcp', str(ROOT / 'helm/charts/nico-dhcp'),
             '--namespace', 'nico-system', '--values', '-', '--show-only', 'templates/configmap-v6.yaml'],
            input=yaml.safe_dump({'global': values['global'], **values['nico-dhcp']}),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        kea = json.loads(yaml.safe_load(result.stdout)['data']['kea_config6.json'])['Dhcp6']
        self.assertEqual(kea['hooks-libraries'][0]['parameters']['hook-dns-servers-ipv6'], 'fd00:84::1')
        self.assertEqual(kea['server-id']['identifier'], '020000001234')
        self.assertEqual([kea[k] for k in ('preferred-lifetime', 'valid-lifetime', 'renew-timer', 'rebind-timer')],
                         [120, 180, 30, 60])

    def test_ipv6_endpoints_render_existing_service_contracts_and_enable_dns(self):
        tree = ast.parse(TILT.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Name) and n.test.id == 'ipv6_endpoints']
        values = yaml.safe_load((TILT.parent / 'values.yaml').read_text())
        calls = []
        env = dict(ipv6_endpoints=True, nico_api_values=values['nico-api'],
                   core_namespace='nico-system', repo_root=str(ROOT), os=os,
                   helm_chart=lambda **kw: calls.append(kw), enabled_resources=[])
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'Tiltfile', 'exec'), env)
        self.assertEqual(env['enabled_resources'], ['nico-lab-dns'])
        self.assertEqual(calls[0]['resource_deps'], ['nico-api', 'nico-pxe'])
        pxe_calls, _ = self.profile(True, ipv6_endpoints=True)
        env_values = pxe_calls[0]['values']['config']['extraEnvData']
        self.assertEqual(env_values['CARBIDE_API_URL'], 'https://carbide-api.forge:1079')
        self.assertEqual(env_values['CARBIDE_PXE_URL'], 'http://carbide-pxe.forge:8080')
        for component, settings, port in [('nico-api', {'global': values['global'], **env['nico_api_values']}, 1079),
                                           ('nico-pxe', {**pxe_calls[0]['values'], 'global': values['global']}, 8080)]:
            with self.subTest(component=component):
                settings.pop('global_values', None)
                result = subprocess.run(
                    ['helm', 'template', component, str(ROOT / 'helm/charts' / component),
                     '--namespace', 'nico-system', '--values', '-', '--show-only', 'templates/external-service.yaml'],
                    input=yaml.safe_dump(settings), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                resources = list(yaml.safe_load_all(result.stdout))
                self.assertEqual(len(resources), 1)
                service = resources[0]['spec']
                self.assertEqual(service['type'], 'ClusterIP')
                self.assertEqual(service['ipFamilies'], ['IPv6'])
                self.assertEqual(service['ports'][0]['port'], port)
                self.assertNotIn('externalTrafficPolicy', service)

    def test_ipv6_endpoints_require_managed_dhcpv6(self):
        tree = ast.parse(TILT.read_text())
        guards = [n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.BoolOp)
                  and any(isinstance(x, ast.Name) and x.id == 'ipv6_endpoints' for x in ast.walk(n.test))]
        def fail(message):
            raise ValueError(message)
        with self.assertRaisesRegex(ValueError, 'requires --dhcpv6'):
            exec(compile(ast.Module(body=guards, type_ignores=[]), 'Tiltfile', 'exec'),
                 dict(ipv6_endpoints=True, dhcpv6=False, fail=fail))

    def test_bmc_mode_does_not_enable_mat_or_its_readiness_resource(self):
        tree = ast.parse(TILT.read_text())
        assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'enabled_resources' for t in n.targets)]
        filtering = [n for n in tree.body if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name) and n.test.id == 'bmc_lab'
                     and any(isinstance(x, ast.Name) and x.id == 'enabled_resources' for x in ast.walk(n))]
        for bmc in (False, True):
            with self.subTest(bmc=bmc):
                env = dict(bmc_lab=bmc)
                exec(compile(ast.Module(body=assignments + filtering, type_ignores=[]), 'Tiltfile', 'exec'), env)
                names = env['enabled_resources']
                self.assertEqual('nico-pxe' in names, bmc)
                self.assertIn('local-site-mode', names)
                for name in ('nico-machine-a-tron', 'nico-ssh-console-rs', 'stack-ready'):
                    self.assertEqual(name in names, not bmc)

    def test_mode_is_claimed_before_core_starts(self):
        tree = ast.parse(TILT.read_text())
        deps = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'nico_api_dependencies' for t in n.targets))
        self.assertIn('local-site-mode', ast.literal_eval(deps))
        start = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'site_mode' for t in node.targets))
        for bmc in (False, True):
            calls, resources = [], []
            with self.subTest(bmc=bmc):
                exec(compile(ast.Module(body=tree.body[start:start + 4], type_ignores=[]), 'Tiltfile', 'exec'), dict(
                    os=os, tilt_dir='/tilt', cluster_context='kind-test', core_namespace='nico-system',
                    bmc_lab=bmc, local=lambda command, **kw: calls.append(command),
                    local_resource=lambda name, command, **kw: resources.append((name, command))))
                expected = ['python3', '/tilt/site-mode.py', '--context', 'kind-test',
                            '--namespace', 'nico-system', '--mode', 'bmc' if bmc else 'mat']
                self.assertEqual(calls, [expected + ['check']])
                self.assertEqual(resources, [('local-site-mode', expected + ['claim'])])
