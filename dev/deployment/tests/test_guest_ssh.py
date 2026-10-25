# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import argparse
import contextlib
import importlib.util
import io
import ipaddress
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('guest_ssh', ROOT / 'dev/deployment/lab/ssh-guest.py')
SSH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SSH)


class GuestSshTest(unittest.TestCase):
    def test_relay_preserves_binary_response_after_stdin_eof(self):
        request = bytes(range(256)) * 512
        response = request[::-1]
        received = bytearray()
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(5)

            def serve():
                with server.accept()[0] as connection:
                    connection.settimeout(5)
                    while data := connection.recv(65536):
                        received.extend(data)
                    connection.sendall(response)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            result = subprocess.run([sys.executable, '-c', SSH.RELAY, '127.0.0.1', str(server.getsockname()[1])],
                                    input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, request)
        self.assertEqual(result.stdout, response)

    def test_proxy_keeps_key_on_client_and_preserves_argument_boundaries(self):
        args = argparse.Namespace(release='target-one', identity=Path('/tmp/key with spaces'),
                                  known_hosts=Path('/tmp/known hosts'), user='lab', command=['uname -m'])
        kube = ['kubectl', '--context', 'kind-lab', '-n', 'bmc-lab']
        command = SSH.ssh_command(args, kube, ipaddress.IPv4Address('10.84.40.10'))
        proxy = shlex.split(next(value.removeprefix('ProxyCommand=') for value in command if value.startswith('ProxyCommand=')))
        self.assertEqual(proxy, kube + ['exec', '-i', 'deployment/target-one', '--', 'python3', '-c', SSH.RELAY, '10.84.40.10', '22'])
        self.assertNotIn(str(args.identity), proxy)
        self.assertIn('StrictHostKeyChecking=accept-new', command)
        self.assertEqual(command[-2:], ['lab@10.84.40.10', 'uname -m'])

    def test_non_kind_context_rejected_before_cluster_access(self):
        with patch.object(sys, 'argv', ['ssh-guest', '--context', 'production', '--address', '10.84.40.10',
                                       '--identity', '/unused', '--known-hosts', '/unused']), \
                patch.object(SSH.subprocess, 'check_output') as command, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            SSH.main()
        self.assertEqual(error.exception.code, 2)
        command.assert_not_called()

    def test_wrong_target_or_address_rejected_before_ssh(self):
        cases = [('other-service', '10.84.40.10'), ('nico-bmc-target', '10.84.41.10'),
                 ('nico-bmc-target', '10.84.40.1')]
        for label, address in cases:
            with self.subTest(label=label, address=address), \
                    patch.object(sys, 'argv', ['ssh-guest', '--context', 'kind-lab', '--address', address,
                                               '--identity', '/unused', '--known-hosts', '/unused']), \
                    patch.object(Path, 'is_file', return_value=True), \
                    patch.object(SSH.subprocess, 'check_output', side_effect=[
                        ('{"metadata":{"labels":{"app.kubernetes.io/name":"' + label + '"}}}').encode(),
                        b'{"bridgeAddress":"10.84.40.1/24"}']), \
                    patch.object(SSH.subprocess, 'call') as connect, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                SSH.main()
            self.assertEqual(error.exception.code, 2)
            connect.assert_not_called()
