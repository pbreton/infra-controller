# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('guest_connectivity', Path(__file__).resolve().parents[1] / 'lab/check-connectivity.py')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class GuestConnectivityTest(unittest.TestCase):
    def test_real_exchange_checks_source_and_payload_and_closes_listener(self):
        def local_command(args, guest, code, *values):
            return [sys.executable, '-u', '-c', code, *map(str, values)]
        guest = {'address': '127.0.0.1'}
        with patch.object(CHECK, 'ssh_command', side_effect=local_command):
            result = CHECK.probe(None, guest, guest)
        self.assertEqual(result, {'source': '127.0.0.1', 'destination': '127.0.0.1', 'bytes': 32})

    def test_failed_sender_reaps_listener_process(self):
        processes = []
        original = CHECK.guest_process

        @contextlib.contextmanager
        def record(command):
            with original(command) as process:
                processes.append(process)
                yield process

        def local_command(args, guest, code, *values):
            if code == CHECK.CLIENT:
                return [sys.executable, '-c', 'raise SystemExit(7)']
            return [sys.executable, '-u', '-c', code, *map(str, values)]

        with patch.object(CHECK, 'ssh_command', side_effect=local_command), \
                patch.object(CHECK, 'guest_process', side_effect=record), \
                self.assertRaisesRegex(RuntimeError, 'sender failed'):
            CHECK.probe(None, {'address': '127.0.0.1'}, {'address': '127.0.0.1'})
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertTrue(all(process.stdout.closed for process in processes))

    def test_receiver_rejects_translated_source_or_wrong_payload(self):
        for expected_source, payload in [('127.0.0.2', 'expected'), ('127.0.0.1', 'wrong')]:
            with self.subTest(source=expected_source, payload=payload), subprocess.Popen(
                    [sys.executable, '-u', '-c', CHECK.SERVER, '127.0.0.1', expected_source, 'expected'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as server:
                port = json.loads(server.stdout.readline())['port']
                client = subprocess.run([sys.executable, '-c', CHECK.CLIENT, '127.0.0.1', '127.0.0.1', str(port), payload],
                                        capture_output=True, text=True, timeout=10)
                output, errors = server.communicate(timeout=10)
                self.assertNotEqual(server.returncode, 0, errors)
                self.assertNotEqual(client.returncode, 0, client.stderr)
                self.assertNotIn('destination', output)

    def test_cli_probes_both_directions_and_does_not_report_success_after_failure(self):
        argv = ['check-connectivity', '--context', 'kind-test', '--first-address', '10.84.40.10',
                '--second-address', '10.84.41.10', '--identity', '/tmp/key',
                '--first-known-hosts', '/tmp/one', '--second-known-hosts', '/tmp/two']
        for failure in (False, True):
            output = io.StringIO()
            effects = [{'source': 'first'}, RuntimeError('peer unreachable') if failure else {'source': 'second'}]
            with self.subTest(failure=failure), patch.object(sys, 'argv', argv), \
                    patch.object(CHECK, 'probe', side_effect=effects) as probe, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                if failure:
                    with self.assertRaises(SystemExit) as error:
                        CHECK.main()
                    self.assertEqual(error.exception.code, 1)
                    self.assertEqual(output.getvalue(), '')
                else:
                    CHECK.main()
                    self.assertEqual(len(json.loads(output.getvalue())['directions']), 2)
                first, second = probe.call_args_list
                self.assertEqual(first.args[1]['address'], second.args[2]['address'])
                self.assertEqual(first.args[2]['address'], second.args[1]['address'])

    def test_same_guest_rejected_before_starting_probe(self):
        argv = ['check-connectivity', '--context', 'kind-test', '--first-address', '10.84.40.10',
                '--second-address', '10.84.40.10', '--identity', '/tmp/key',
                '--first-known-hosts', '/tmp/one', '--second-known-hosts', '/tmp/two']
        with patch.object(sys, 'argv', argv), patch.object(CHECK, 'probe') as probe, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            CHECK.main()
        self.assertEqual(error.exception.code, 2)
        probe.assert_not_called()
