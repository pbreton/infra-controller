# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import errno
import json
from pathlib import Path
import sys
import subprocess
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

LAB = Path(__file__).resolve().parents[1] / 'lab'
sys.path.insert(0, str(LAB))
SPEC = importlib.util.spec_from_file_location('bmc_target', LAB / 'target.py')
TARGET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TARGET)
sys.path.pop(0)

CONFIG = {'nodeName': 'node', 'bmcAddress': '10.96.40.10', 'clusterUID': 'cluster', 'owner': 'namespace/release', 'architecture': 'arm64',
          'instanceIndex': 1, 'memoryMiB': 4096, 'cpus': 2, 'diskGiB': 24,
          'bridgeAddress': '10.84.40.1/24', 'bmcPort': 2443}


class TargetTest(unittest.TestCase):
    def test_native_domain_preserves_identity_and_boot_contract(self):
        for arch, machine, firmware, index, mac, serial in [
                ('arm64', 'virt', 'AAVMF', 1, '02:00:00:01:00:02', '020000010001'),
                ('amd64', 'q35', 'OVMF', 10, '02:00:00:0a:00:02', '0200000A0001')]:
            with self.subTest(arch=arch):
                config = TARGET.configuration({**CONFIG, 'architecture': arch, 'instanceIndex': index})
                xml = ET.fromstring(TARGET.domain_xml(config))
                self.assertEqual(xml.attrib['type'], 'kvm')
                self.assertEqual(xml.findtext('uuid'), config['uuid'])
                self.assertEqual(xml.find('os/type').attrib['machine'], machine)
                self.assertIn(firmware, xml.findtext('os/loader'))
                self.assertEqual([b.attrib['dev'] for b in xml.findall('os/boot')], ['network', 'hd'])
                self.assertEqual(xml.find('devices/interface/mac').attrib['address'], mac)
                self.assertEqual(xml.findtext("sysinfo/system/entry[@name='serial']"), serial)
                self.assertEqual(xml.find('devices/disk/source').attrib['file'], '/data/disks/root.qcow2')
                self.assertEqual(xml.findtext('os/nvram'), '/data/disks/nvram.fd')

    def test_config_rejects_incompatible_guest_inputs(self):
        for changes in [{'architecture': 'ppc64le'}, {'cpus': True}, {'instanceIndex': 0},
                        {'memoryMiB': 0}, {'owner': ''}, {'bridgeAddress': '10.84.40.0/24'},
                        {'bridgeAddress': 'fd42::1/64'}, {'extra': 'field'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TARGET.configuration({**CONFIG, **changes})

    def test_retained_storage_is_not_adopted_or_resized(self):
        config = TARGET.configuration(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            TARGET.claim(root, config)
            before = (root / 'inventory.json').read_bytes()
            TARGET.claim(root, config)
            for changed in [{**config, 'clusterUID': 'other'}, {**config, 'diskGiB': 48}]:
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'retained target'):
                    TARGET.claim(root, changed)
            self.assertEqual((root / 'inventory.json').read_bytes(), before)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bmc.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'unrecognized'):
                TARGET.claim(root, config)

    def test_lock_rejects_a_second_writer_and_releases_after_failure(self):
        config = TARGET.configuration(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'startup failure'):
                with TARGET.bound_storage(root, config):
                    with self.assertRaises(BlockingIOError):
                        with TARGET.bound_storage(root, config):
                            self.fail('second writer acquired the target')
                    raise RuntimeError('startup failure')
            with TARGET.bound_storage(root, config):
                self.assertTrue((root / 'inventory.json').is_file())

    def test_lost_bmc_state_is_not_reinitialized(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(TARGET, 'ROOT', Path(directory)), \
                patch.object(TARGET, 'run') as run:
            (Path(directory) / 'bmc-initialized').touch()
            with self.assertRaisesRegex(ValueError, 'missing its credentials'):
                TARGET.prepare_storage(TARGET.configuration(CONFIG))
            run.assert_not_called()

    def test_bridge_cleanup_preserves_foreign_and_attached_links(self):
        config = TARGET.configuration(CONFIG)
        for alias, attached, delete in [('other', [], False), (config['uuid'], [{'ifname': 'tap0'}], False), (config['uuid'], [], True)]:
            with self.subTest(alias=alias, attached=attached), \
                    patch.object(TARGET, 'run', side_effect=[json.dumps([{'ifname': config['bridge'], 'ifalias': alias}]), json.dumps(attached), '']) as run:
                TARGET.clear_bridge(config)
                self.assertEqual(any(call.args[:3] == ('ip', 'link', 'delete') for call in run.call_args_list), delete)

    def test_initialized_target_never_recreates_lost_disk(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(TARGET, 'ROOT', Path(directory)), \
                patch.object(TARGET, 'run') as run:
            (Path(directory) / 'initialized').touch()
            with self.assertRaisesRegex(ValueError, 'missing its disk'):
                TARGET.prepare_storage(TARGET.configuration(CONFIG))
            run.assert_not_called()

    def test_foreign_bridge_and_overlapping_routes_fail_before_mutation(self):
        config = TARGET.configuration(CONFIG)
        cases = [([{'ifname': config['bridge'], 'ifalias': 'another'}], [], 'different owner'),
                 ([], [{'dst': '10.84.0.0/16', 'dev': 'eth0'}], 'overlaps')]
        for links, routes, error in cases:
            with self.subTest(error=error), patch.object(TARGET, 'run', side_effect=[json.dumps(links), json.dumps(routes)]) as run:
                with self.assertRaisesRegex(ValueError, error):
                    TARGET.bridge(config)
                self.assertTrue(all(call.args[:3] in [('ip', '-json', 'link'), ('ip', '-4', '-json')]
                                    for call in run.call_args_list))

    def test_failed_alias_assignment_removes_only_the_new_bridge(self):
        config = TARGET.configuration(CONFIG)
        error = subprocess.CalledProcessError(1, 'ip')
        with patch.object(TARGET, 'run', side_effect=['[]', '[]', '', error, '']) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                TARGET.bridge(config)
        self.assertEqual(run.call_args_list[-1].args, ('ip', 'link', 'delete', config['bridge']))

    def test_port_probe_matches_bmc_fallback_without_hiding_conflicts(self):
        for failure in ('dual-stack', 'create-v6', 'setup-v6', 'bind'):
            with self.subTest(failure=failure):
                ipv6, ipv4 = Mock(), Mock()
                for listener in (ipv6, ipv4):
                    listener.__enter__ = Mock(return_value=listener)
                    listener.__exit__ = Mock(return_value=False)
                if failure == 'setup-v6':
                    ipv6.setsockopt.side_effect = OSError(errno.ENOPROTOOPT, 'no dual stack')
                if failure == 'bind':
                    ipv6.bind.side_effect = OSError(errno.EADDRINUSE, 'busy')
                creation = [OSError(errno.EAFNOSUPPORT, 'no IPv6') if failure == 'create-v6' else ipv6, ipv4]
                with patch.object(TARGET.socket, 'socket', side_effect=creation) as create:
                    if failure == 'bind':
                        with self.assertRaises(OSError) as error:
                            TARGET.check_bmc_port(CONFIG)
                        self.assertEqual(error.exception.errno, errno.EADDRINUSE)
                        self.assertEqual(create.call_count, 1)
                    elif failure == 'dual-stack':
                        TARGET.check_bmc_port(CONFIG)
                        self.assertEqual(create.call_count, 1)
                        ipv6.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        ipv6.bind.assert_called_once_with(('::', CONFIG['bmcPort']))
                        ipv6.listen.assert_called_once()
                    else:
                        TARGET.check_bmc_port(CONFIG)
                        ipv4.bind.assert_called_once_with(('0.0.0.0', CONFIG['bmcPort']))
                        ipv4.listen.assert_called_once()
                        self.assertEqual(create.call_args.args[0], socket.AF_INET)
                        if failure == 'setup-v6':
                            ipv6.close.assert_called_once()

    def test_bad_network_or_busy_port_does_not_claim_fresh_storage(self):
        create_socket = socket.socket

        def ipv4_only(family, kind):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, 'IPv4-only fixture')
            return create_socket(family, kind)

        for failure in ('overlap', 'busy-port'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, \
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(('0.0.0.0', 0))
                listener.listen()
                config = {**CONFIG, 'bmcPort': listener.getsockname()[1]}
                config_file = Path(directory) / 'config.json'
                config_file.write_text(json.dumps(config))
                root = Path(directory) / 'data'
                root.mkdir()
                routes = [{'dst': '10.84.0.0/16', 'dev': 'eth0'}] if failure == 'overlap' else []
                with patch.object(TARGET, 'ROOT', root), \
                        patch.object(sys, 'argv', ['target.py', '--config', str(config_file)]), \
                        patch.dict(TARGET.os.environ, NODE_NAME=config['nodeName']), \
                        patch.object(TARGET.platform, 'machine', return_value='aarch64'), \
                        patch.object(TARGET.preflight, 'check_kvm'), \
                        patch.object(TARGET.socket, 'socket', side_effect=ipv4_only), \
                        patch.object(TARGET, 'run', side_effect=['[]', json.dumps(routes)]), \
                        self.assertRaises((ValueError, OSError)):
                    TARGET.main()
                self.assertEqual(list(root.iterdir()), [])

    def test_bmc_stops_before_guest_save_and_libvirt_shutdown(self):
        events = []
        config = TARGET.configuration(CONFIG)

        class Child:
            def __init__(self, command):
                self.name = command[0]
            def poll(self):
                return None
            def terminate(self):
                events.append(('stop', self.name))
            def wait(self, timeout):
                return 0

        def virsh(*args, **kwargs):
            events.append(('virsh', args[0]))
            return {'list': 'target', 'domuuid': config['uuid'], 'domstate': 'running'}.get(args[0], '')

        with tempfile.TemporaryDirectory() as directory, patch.object(TARGET, 'ROOT', Path(directory)), \
                patch.object(TARGET, 'READY', Path(directory) / 'ready'), \
                patch.object(TARGET, 'prepare_storage'), patch.object(TARGET, 'bridge'), \
                patch.object(TARGET, 'clear_bridge'), \
                patch.object(TARGET.Path, 'mkdir'), \
                patch.object(TARGET, 'virsh', side_effect=virsh), \
                patch.object(TARGET.subprocess, 'Popen', side_effect=Child), \
                patch.object(TARGET.socket, 'create_connection'), \
                patch.object(TARGET.time, 'sleep', side_effect=lambda _: setattr(TARGET, 'STOP', True)), \
                patch.object(TARGET, 'STOP', False):
            (Path(directory) / 'bmc.json').write_text('{}')
            TARGET.serve(config)
        self.assertLess(events.index(('stop', 'bmc-mock')), events.index(('virsh', 'managedsave')))
        self.assertLess(events.index(('virsh', 'managedsave')), events.index(('stop', 'libvirtd')))
        self.assertNotIn(('virsh', 'start'), events)
