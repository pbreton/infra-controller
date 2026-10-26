# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('lifecycle', ROOT / 'dev/deployment/lab/lifecycle.py')
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)
INSTANCE = '81240030-0000-4000-8000-000000000001'


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.target = dict(machine='machine-one', namespace='bmc-lab', release='one', intent='lab-test-1-one')
        self.current = dict(id=INSTANCE, machine_id='machine-one', metadata={'name': 'lab-test-1-one_0'})
        self.state = dict(context='kind-lab', core_namespace='nico-system', targets=[self.target], timeout=60,
                          generation=1, network='ipv6-only')

    def test_cleanup_recovers_lost_allocation_response_but_never_adopts_a_replacement(self):
        cases = [('matching', self.current, True),
                 ('foreign name', dict(self.current, metadata={'name': 'unrelated'}), False),
                 ('replaced ID', dict(self.current, id='81240030-0000-4000-8000-000000000002'), False)]
        for name, current, allowed in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                target = dict(self.target)
                if name == 'replaced ID':
                    target['instance'] = INSTANCE
                lab = LAB.Lifecycle(Path(directory), dict(self.state, targets=[target]))
                lab.admin = Mock(return_value={'instances': [current]})
                lab.mutation = Mock()
                lab.wait = Mock(return_value=True)
                if allowed:
                    def mutation(*args):
                        recorded = json.loads((Path(directory) / 'state.json').read_text())
                        self.assertEqual(recorded['targets'][0]['instance'], INSTANCE)
                    lab.mutation.side_effect = mutation
                    lab.release(target)
                    lab.mutation.assert_called_once_with('instance', 'release', '--instance', INSTANCE)
                    self.assertNotIn('intent', target)
                else:
                    with self.assertRaises(ValueError):
                        lab.release(target)
                    lab.mutation.assert_not_called()

    def test_inventory_rejects_cross_cluster_and_wrong_bmc_before_adoption(self):
        inventory = dict(clusterUID='cluster', owner='bmc-lab/one', bmcAddress='fd00::12', instanceIndex=3)
        machine = dict(id='machine-one', bmc_info={'ip': 'fd00::12', 'mac': '02:00:00:03:00:01'})
        LAB.validate_target(self.target, inventory, machine, 'cluster')
        for changes in ({'clusterUID': 'different'}, {'owner': 'bmc-lab/two'}, {'bmcAddress': 'fd00::13'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                LAB.validate_target(self.target, dict(inventory, **changes), machine, 'cluster')

    def test_disk_readiness_does_not_pin_a_previous_generation_or_the_imager(self):
        previous = 'disk_imaging.sh\nRebooting\nDatasource DataSourceNoCloud\n'
        with tempfile.TemporaryDirectory() as directory:
            lab = LAB.Lifecycle(Path(directory), self.state)
            target = dict(self.target, console_offset=len(previous))
            lab.ssh = Mock(return_value=INSTANCE + '\n')
            lab.checkpoint = Mock()
            for current in ('', 'disk_imaging.sh\nDatasource DataSourceNoCloud\n', 'disk_imaging.sh\nRebooting\n'):
                with self.subTest(current=current):
                    lab.kube = Mock(return_value=previous + current)
                    self.assertFalse(lab.installed(target))
                    lab.ssh.assert_not_called()
            lab.kube = Mock(return_value=previous + 'disk_imaging.sh\nRebooting\nDatasource DataSourceNoCloud\n')
            self.assertTrue(lab.installed(target))
            lab.ssh.assert_called_once()
            self.assertIn('test ! -e /etc/nico-peer-retained', lab.ssh.call_args.args[1])

    def test_allocation_intent_survives_lost_response(self):
        with tempfile.TemporaryDirectory() as directory:
            target = dict(self.target, inventory={'mac': '02:00:00:03:00:02'})
            state = dict(self.state, targets=[target], run='lab-test', os_image=INSTANCE, vpc=INSTANCE)
            lab = LAB.Lifecycle(Path(directory), state)
            lab.wait = Mock(return_value=True)
            lab.kube = Mock(return_value='previous console')
            lab.mutation = Mock(side_effect=RuntimeError('connection lost'))
            lab.instance = Mock()
            with patch.object(LAB, 'run', return_value='{}'), self.assertRaises(RuntimeError):
                lab.allocate(target)
            saved = json.loads((Path(directory) / 'state.json').read_text())['targets'][0]
            self.assertEqual(saved['intent'], 'lab-test-1-one')
            self.assertEqual(saved['console_offset'], len('previous console'))
            self.assertNotIn('instance', saved)
            lab.instance.assert_not_called()

    def test_addresses_must_belong_to_the_selected_target(self):
        inventory = {'bridgeAddressV6': 'fd42:1::1/64'}
        for addresses, valid in [(['fd42:1::a'], True), (['fd42:2::a'], False), (['fd42:1::1'], False),
                                 (['fd42:1::a', 'fd42:1::b'], False)]:
            with self.subTest(addresses=addresses):
                instance = {'status': {'network': {'interfaces': [{'addresses': addresses}]}}}
                if valid:
                    self.assertEqual(LAB.selected_addresses(instance, 'ipv6-only', inventory), {'6': 'fd42:1::a'})
                else:
                    with self.assertRaises(ValueError):
                        LAB.selected_addresses(instance, 'ipv6-only', inventory)


if __name__ == '__main__':
    unittest.main()
