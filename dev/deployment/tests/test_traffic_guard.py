# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'lab/traffic-guard.py'
SPEC = importlib.util.spec_from_file_location('traffic_guard', PATH)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
MAC = bytes.fromhex('020000030002')


def frame(protocol, payload):
    return b'\xff' * 6 + MAC + protocol.to_bytes(2, 'big') + payload


def ipv4(source=b'\0' * 4, destination=b'\xff' * 4, ports=b'\0D\0C', fragment=0):
    packet = bytearray(28)
    packet[0] = 0x45
    packet[2:4] = len(packet).to_bytes(2, 'big')
    packet[6:8] = fragment.to_bytes(2, 'big')
    packet[9] = 17
    packet[12:20] = source + destination
    packet[20:24] = ports
    packet[24:26] = b'\0\x08'
    return bytes(packet)


class TrafficGuardTests(unittest.TestCase):
    def test_packet_policy_distinguishes_firmware_probes_from_ipv4_use(self):
        v6 = b'\x60' + b'\0' * 39
        cases = [
            (frame(0x0806, b'\0' * 28), 'arp'),
            (b'\xff' * 6 + b'\x01' * 6 + b'\x08\x00' + ipv4(ports=b'\0C\0D'), 'ipv4'),
            (b'\xff' * 6 + b'\x01' * 6 + b'\x08\x06' + b'\0' * 28, 'arp'),
            (frame(0x86dd, v6), 'ipv6'),
            (frame(0x8100, b'\0\1\x86\xdd' + v6), 'ipv6'),
            (frame(0x0800, ipv4()), 'dhcp4_probe'),
            (frame(0x0800, ipv4(source=b'\x0a\0\0\1')), 'ipv4'),
            (frame(0x0800, ipv4(ports=b'\0C\0D')), 'ipv4'),
            (frame(0x0800, ipv4(fragment=0x2000)), 'ipv4'),
            (frame(0x0800, ipv4(ports=b'\0\x35\0\x35')), 'ipv4'),
            (frame(0x0800, b'\x45'), 'malformed'),
            (frame(0x86dd, b'\x60'), 'malformed'),
            (frame(0x88cc, b'lldp'), 'other'),
            (b'\0' * 14, None),
        ]
        for packet, expected in cases:
            with self.subTest(expected=expected, packet=packet):
                self.assertEqual(GUARD.classify(packet, MAC), expected)

    def test_incomplete_lossy_or_ipv4_observation_cannot_pass(self):
        good = {'event': 'finished', 'stop_requested': True, 'counts': dict.fromkeys(GUARD.COUNTERS, 0)}
        good['counts'].update(ipv6=10, dhcp4_probe=4)
        records = [{'event': 'ready'}, good]
        self.assertEqual(GUARD.assess(records), good)
        cases = [[], records[:1], [dict(good)], [records[0], dict(good, stop_requested=False)]]
        for counter in ('arp', 'ipv4', 'malformed', 'dropped'):
            cases.append([records[0], dict(good, counts=dict(good['counts'], **{counter: 1}))])
        cases.append([records[0], dict(good, counts=dict(good['counts'], ipv6=0))])
        for invalid in cases:
            with self.subTest(records=invalid), self.assertRaises(ValueError):
                GUARD.assess(invalid)

    def test_stop_attempts_both_targets_and_records_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = object.__new__(GUARD.Guard)
            guard.args = Mock(output=Path(directory), namespace='bmc-lab')
            guard.cluster, guard.kube, guard.command_succeeded = 'cluster', ['kubectl'], True
            jobs = []
            for name in ('one', 'two'):
                process = Mock()
                process.poll.return_value = 1
                jobs.append(dict(release=name, process=process, log=Mock(), errors=Mock()))
            guard.jobs = jobs
            with patch.object(GUARD, 'checked', side_effect=RuntimeError('cluster unavailable')), self.assertRaises(ValueError):
                guard.stop()
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(len(report['failures']), 4)
            self.assertIn('cluster unavailable', report['failures'][-2])
            for job in jobs:
                job['log'].close.assert_called_once()
                job['errors'].close.assert_called_once()

    def test_failed_capture_start_never_launches_wrapped_command_and_stops_started_observers(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = Mock()
            observer.start.side_effect = ValueError('second capture failed')
            args = ['traffic-guard.py', '--context', 'kind-test', '--target', 'one', '--target', 'two',
                    '--output', str(Path(directory) / 'output'), '--', 'unexpected-command']
            with patch.object(GUARD.sys, 'argv', args), patch.object(GUARD, 'Guard', return_value=observer), \
                    patch.object(GUARD.subprocess, 'Popen') as launch, \
                    self.assertRaisesRegex(ValueError, 'second capture failed'):
                GUARD.main()
            launch.assert_not_called()
            observer.stop.assert_called_once()

    def test_termination_signals_stop_the_wrapped_process_group_and_observers(self):
        for number in (GUARD.signal.SIGTERM, GUARD.signal.SIGHUP):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as directory:
                observer, process = Mock(), Mock()
                handlers = {}
                def install(number, handler):
                    previous = handlers.get(number, GUARD.signal.SIG_DFL)
                    handlers[number] = handler
                    return previous
                def wait(**kwargs):
                    if kwargs:
                        handlers[number](number, None)
                    return 0
                process.wait.side_effect = wait
                args = ['traffic-guard.py', '--context', 'kind-test', '--target', 'one', '--target', 'two',
                        '--output', str(Path(directory) / 'output'), '--', 'command']
                with patch.object(GUARD.sys, 'argv', args), patch.object(GUARD, 'Guard', return_value=observer), \
                        patch.object(GUARD.signal, 'signal', side_effect=install), \
                        patch.object(GUARD.subprocess, 'Popen', return_value=process), \
                        patch.object(GUARD.os, 'killpg') as kill, self.assertRaises(InterruptedError):
                    GUARD.main()
                kill.assert_called_once_with(process.pid, GUARD.signal.SIGKILL)
                observer.stop.assert_called_once()
                self.assertEqual(handlers[number], GUARD.signal.SIG_DFL)

    def test_same_source_executes_through_python_c_without_file_context(self):
        result = subprocess.run(['python3', '-c', PATH.read_text(), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--context', result.stdout)

    def test_invalid_cli_never_launches_command_or_creates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            result = subprocess.run(['python3', str(PATH), '--context', 'production', '--target', 'one',
                                     '--target', 'two', '--output', str(output), '--', 'false'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
