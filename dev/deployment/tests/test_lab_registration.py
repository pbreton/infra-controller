# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'lab'
SPEC = importlib.util.spec_from_file_location('lab_registration', ROOT / 'register.py')
REG = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(ROOT), *sys.path]):
    SPEC.loader.exec_module(REG)


def inventories():
    return {name: dict(clusterUID='cluster', owner='bmc-lab/' + name, architecture='arm64',
                       instanceIndex=index, bmcAddress=f'fd00:10:96::401{index}',
                       mac=f'02:00:00:0{index}:00:02', serial=f'0200000{index}0001',
                       bridgeAddress=f'10.84.{index}.1/24', bridgeAddressV6=f'fd42:8124:{index}::1/64')
            for name, index in [('one', 1), ('two', 2)]}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        secret = self.directory / 'password'
        secret.write_text('test-only-password\n')
        secret.chmod(0o600)
        segments, targets = REG.plan_targets(inventories(), 'ipv6-only', ['fd00:10:96::4010/124'], 'lab-test', 'domain')
        self.state = dict(version=1, context='kind-test', namespace='nico-system', target_namespace='bmc-lab',
                          cluster='cluster', owner='lab-test', architecture='arm64', segments=segments, targets=targets,
                          timeout=60, bmc_username='root', bmc_password_file=str(secret))
        self.segments, self.expected, self.writes = [], [], []
        self.lab = REG.Registration(self.directory, self.state)
        self.lab.verify_targets = Mock()
        self.lab.admin = Mock(side_effect=self.admin)

    def admin(self, *args, read=False, operation=None):
        if read:
            return {'network_segments': self.segments} if args[0] == 'network-segment' else {'expected_machines': self.expected}
        self.assertTrue(json.loads((self.directory / 'state.json').read_text())[operation.removesuffix('-create') + '_intent'])
        self.writes.append(args)
        resource_id = args[args.index('--id') + 1]
        if args[0] == 'network-segment':
            desired = next(s for s in self.state['segments'] if s['id'] == resource_id)
            self.segments.append(dict(id=resource_id, deleted=None, metadata={'name': desired['name']},
                                      config=dict(mtu=1500, vpc_id=None, **{k: copy.deepcopy(desired[k]) for k in ('segment_type', 'subdomain_id', 'prefixes')}),
                                      status={'lifecycle': {'state': '{"state":"ready"}'}}))
        else:
            target = next(t for t in self.state['targets'] if t['expected_id'] == resource_id)
            inv = target['inventory']
            self.expected.append(dict(id={'value': resource_id}, bmc_mac_address=target['bmc_mac'], bmc_ip_address=inv['bmcAddress'],
                                      chassis_serial_number=inv['serial'], dpu_mode=3, dpf_enabled=False,
                                      metadata={'name': target['name'], 'description': self.state['owner']},
                                      interfaces=[dict(mac_address=inv['mac'], primary=True, network_segment_type=3)]))

    def test_registration_then_resume_does_not_overwrite_rotated_credentials(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.lab.apply()
            for expected in self.expected:
                expected['bmc_password'] = 'rotated'
            Path(self.state['bmc_password_file']).unlink()
            self.lab.apply()
        self.assertEqual(len(self.writes), 5)
        self.assertEqual([w[0] for w in self.writes], ['network-segment'] * 3 + ['em'] * 2)
        self.assertEqual([e['bmc_password'] for e in self.expected], ['rotated', 'rotated'])
        self.assertNotIn('test-only-password', (self.directory / 'state.json').read_text())
        self.assertTrue(self.state['complete'])
        self.assertNotIn('--gateway', self.writes[1])

    def test_existing_overlapping_segment_stops_before_any_registration_write(self):
        self.segments = [dict(id='other', deleted=None, config={'prefixes': [{'prefix': 'fd42:8124::/32'}]})]
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            self.lab.apply()
        self.assertEqual(self.writes, [])

    def test_shared_identity_or_changed_policy_never_adopts_expected_machine(self):
        with contextlib.redirect_stdout(io.StringIO()): self.lab.apply()
        for change in ('owner', 'id', 'fixed_ip', 'dpu', 'duplicate'):
            original = copy.deepcopy(self.expected)
            if change == 'owner': self.expected[0]['metadata']['description'] = 'someone-else'
            elif change == 'id': self.expected[0]['id']['value'] = 'other'
            elif change == 'fixed_ip': self.expected[0]['interfaces'][0]['fixed_ip'] = 'fd42:8124:1::123'
            elif change == 'dpu': self.expected[0]['dpu_mode'] = 1
            else: self.expected.append(copy.deepcopy(self.expected[0]))
            before = len(self.writes)
            with self.subTest(change=change), self.assertRaises(ValueError): self.lab.apply()
            self.assertEqual(len(self.writes), before)
            self.expected = original

    def test_lost_expected_response_recovers_identity_without_repeating_create(self):
        original = self.admin
        def lost(*args, **kwargs):
            result = original(*args, **kwargs)
            if not kwargs.get('read') and args[0] == 'em': raise RuntimeError('lost response')
            return result
        self.lab.admin.side_effect = lost
        with self.assertRaisesRegex(RuntimeError, 'lost response'): self.lab.apply()
        self.lab.admin.side_effect = original
        with contextlib.redirect_stdout(io.StringIO()): self.lab.apply()
        self.assertEqual(len(self.writes), 5)
        self.assertEqual(len(self.expected), 2)

    def test_changed_target_inventory_blocks_all_core_writes(self):
        self.lab.verify_targets = REG.Registration.verify_targets.__get__(self.lab)
        self.lab.verify_cluster = Mock()
        with patch.object(REG, 'target_inventory', return_value={'owner': 'different'}), self.assertRaisesRegex(ValueError, 'identity'):
            self.lab.apply()
        self.lab.admin.assert_not_called()

    def test_prefix_plan_preserves_families_gateway_reservation_and_separation(self):
        for profile, families in [('ipv4', [4]), ('dual-stack', [4, 6]), ('ipv6-only', [6])]:
            segments, _ = REG.plan_targets(inventories(), profile, ['fd00:10:96::4010/124'], 'owner', 'domain')
            for s in segments[1:]:
                self.assertEqual([REG.ipaddress.ip_network(p['prefix']).version for p in s['prefixes']], families)
                self.assertTrue(all(p['reserve_first'] == 10 for p in s['prefixes']))
        for change in ('overlap', 'gateway', 'identity'):
            inv = inventories()
            if change == 'overlap': inv['two']['bridgeAddressV6'] = inv['one']['bridgeAddressV6']
            elif change == 'gateway': inv['one']['bridgeAddressV6'] = 'fd42:8124:1::20/64'
            else: inv['two']['mac'] = inv['one']['mac']
            with self.subTest(change=change), self.assertRaises(ValueError):
                REG.plan_targets(inv, 'ipv6-only', ['fd00:10:96::4010/124'], 'owner', 'domain')


if __name__ == '__main__':
    unittest.main()
