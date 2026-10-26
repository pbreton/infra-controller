#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Observe two native target bridges throughout an explicitly supplied lab command."""

import argparse
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve() if '__file__' in globals() else None
SOL_PACKET = 263
PACKET_STATISTICS = 6
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1
SO_RCVBUFFORCE = 33

COUNTERS = ('ipv6', 'dhcp4_probe', 'ipv4', 'arp', 'other', 'malformed', 'dropped')


def classify(frame, mac):
    if len(frame) < 14:
        return None
    guest_frame = mac in (frame[:6], frame[6:12])
    offset, protocol = 14, int.from_bytes(frame[12:14], 'big')
    for _ in range(2):
        if protocol not in (0x8100, 0x88a8):
            break
        if len(frame) < offset + 4:
            return 'malformed'
        protocol = int.from_bytes(frame[offset + 2:offset + 4], 'big')
        offset += 4
    packet = frame[offset:]
    if protocol in (0x8100, 0x88a8):
        return 'malformed'
    if protocol == 0x0806:
        return 'arp'
    if protocol == 0x86dd:
        if not guest_frame:
            return None
        if len(packet) < 40 or packet[0] >> 4 != 6 or len(packet) < 40 + int.from_bytes(packet[4:6], 'big'):
            return 'malformed'
        return 'ipv6'
    if protocol != 0x0800:
        return 'other' if guest_frame else None
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return 'malformed'
    header = (packet[0] & 15) * 4
    length = int.from_bytes(packet[2:4], 'big')
    if header < 20 or length < header or len(packet) < length:
        return 'malformed'
    # Firmware may try DHCPv4 before IPv6. Replies, fragments and assigned-source
    # traffic are violations; these probes do not establish successful IPv4 use.
    if (packet[9] == 17 and int.from_bytes(packet[6:8], 'big') & 0x3fff == 0
            and packet[12:20] == b'\0\0\0\0\xff\xff\xff\xff' and length >= header + 8
            and packet[header:header + 4] == b'\0D\0C' and frame[6:12] == mac):
        return 'dhcp4_probe'
    return 'ipv4'


def capture():
    expected = json.loads(sys.stdin.buffer.readline())
    actual = json.loads(Path('/data/inventory.json').read_text())
    if actual != expected:
        raise ValueError('target inventory changed before capture')
    mac = bytes.fromhex(actual['mac'].replace(':', ''))
    counts = dict.fromkeys(COUNTERS, 0)
    start = time.monotonic()
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as packets:
        packets.bind((actual['bridge'], 0))
        packets.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, 4 * 1024 * 1024)
        membership = struct.pack('IHH8s', socket.if_nametoindex(actual['bridge']), PACKET_MR_PROMISC, 0, b'')
        packets.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, membership)
        packets.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)
        print(json.dumps({'event': 'ready', 'mac': actual['mac'], 'bridge': actual['bridge']}), flush=True)
        last = start
        while True:
            readable, _, _ = select.select([packets, sys.stdin.buffer], [], [], 1)
            if sys.stdin.buffer in readable:
                stop = os.read(sys.stdin.fileno(), 1)
                # Include frames already queued at the stop boundary. A busy
                # bridge that cannot drain is incomplete evidence, never a pass.
                packets.setblocking(False)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        category = classify(packets.recv(65535), mac)
                    except BlockingIOError:
                        break
                    if category:
                        counts[category] += 1
                else:
                    raise TimeoutError('capture queue did not drain')
                break
            if packets in readable:
                category = classify(packets.recv(65535), mac)
                if category:
                    counts[category] += 1
            if time.monotonic() - last >= 1:
                _, dropped = struct.unpack('II', packets.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
                counts['dropped'] += dropped
                print(json.dumps({'event': 'sample', 'seconds': time.monotonic() - start, 'counts': counts}), flush=True)
                last = time.monotonic()
        _, dropped = struct.unpack('II', packets.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
        counts['dropped'] += dropped
        print(json.dumps({'event': 'finished', 'stop_requested': stop == b'!',
                          'seconds': time.monotonic() - start, 'counts': counts}), flush=True)


def checked(command):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def assess(records):
    if not records or records[0].get('event') != 'ready' or records[-1].get('event') != 'finished':
        raise ValueError('capture did not cover the complete command')
    final = records[-1]
    if not final['stop_requested']:
        raise ValueError('capture input closed before the explicit stop')
    counts = final['counts']
    if not counts['ipv6'] or any(counts[key] for key in ('ipv4', 'arp', 'malformed', 'dropped')):
        raise ValueError('IPv6 guard failed: ' + json.dumps(counts, sort_keys=True))
    return final


class Guard:
    def __init__(self, args):
        self.args = args
        self.jobs = []
        self.command_succeeded = False
        self.kube = ['kubectl', '--context', args.context]
        self.cluster = checked([*self.kube, 'get', 'namespace', 'kube-system', '-o', 'json'])['metadata']['uid']

    def start(self):
        for release in self.args.target:
            base = [*self.kube, '-n', self.args.namespace]
            pods = checked([*base, 'get', 'pods', '-l', 'app.kubernetes.io/instance=' + release, '-o', 'json'])['items']
            pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
            if len(pods) != 1 or pods[0]['metadata']['labels'].get('app.kubernetes.io/name') != 'nico-bmc-target':
                raise ValueError('select exactly one native target pod per release')
            pod = pods[0]
            name = pod['metadata']['name']
            inventory = checked([*base, 'exec', name, '--', 'cat', '/data/inventory.json'])
            if inventory['clusterUID'] != self.cluster or inventory['owner'] != self.args.namespace + '/' + release:
                raise ValueError('target belongs to a different cluster or release')
            if any(job['inventory']['mac'] == inventory['mac'] or job['inventory']['bridge'] == inventory['bridge'] for job in self.jobs):
                raise ValueError('targets must have distinct guest MACs and bridges')
            log = (self.args.output / (release + '.jsonl')).open('w')
            errors = (self.args.output / (release + '.stderr')).open('w')
            process = subprocess.Popen([*base, 'exec', '-i', name, '--', 'python3', '-u', '-c', HERE.read_text(), '--capture'],
                                       stdin=subprocess.PIPE, stdout=log, stderr=errors)
            job = dict(release=release, pod=name, uid=pod['metadata']['uid'], inventory=inventory,
                       process=process, log=log, errors=errors)
            self.jobs.append(job)
            process.stdin.write((json.dumps(inventory) + '\n').encode())
            process.stdin.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError('capture exited before readiness: ' + release)
                text = (self.args.output / (release + '.jsonl')).read_text()
                lines = text.splitlines()
                if text.endswith('\n') and lines and json.loads(lines[0]).get('event') == 'ready':
                    break
                time.sleep(.1)
            else:
                raise TimeoutError('capture did not become ready: ' + release)

    def stop(self):
        failures, results = [], {}
        for job in self.jobs:
            process = job['process']
            try:
                if process.poll() is not None:
                    raise ValueError('capture exited before command finished')
                process.stdin.write(b'!')
                process.stdin.flush()
                process.stdin.close()
                if process.wait(timeout=30):
                    raise ValueError('capture returned nonzero')
                records = [json.loads(line) for line in (self.args.output / (job['release'] + '.jsonl')).read_text().splitlines()]
                results[job['release']] = assess(records)
                current = checked([*self.kube, '-n', self.args.namespace, 'get', 'pod', job['pod'], '-o', 'json'])
                if current['metadata']['uid'] != job['uid']:
                    raise ValueError('target pod was replaced during capture')
            except Exception as error:
                failures.append(job['release'] + ': ' + str(error))
            finally:
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                if process.poll() is None:
                    process.kill()
                    process.wait()
                job['log'].close()
                job['errors'].close()
        try:
            if checked([*self.kube, 'get', 'namespace', 'kube-system', '-o', 'json'])['metadata']['uid'] != self.cluster:
                failures.append('cluster identity changed')
        except Exception as error:
            failures.append('final cluster identity check failed: ' + str(error))
        if len(results) != 2:
            failures.append('two complete captures are required')
        report = {'cluster': self.cluster, 'command_succeeded': self.command_succeeded,
                  'passed': self.command_succeeded and not failures, 'captures': results, 'failures': failures}
        (self.args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        if failures:
            raise ValueError('; '.join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--namespace', default='bmc-lab')
    parser.add_argument('--target', action='append', required=True, help='exactly two distinct native target Helm releases')
    parser.add_argument('--output', type=Path, required=True, help='new private evidence directory under an existing parent')
    parser.add_argument('--timeout', type=int, default=14400, help='command timeout in seconds (60..86400, default 14400)')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='command after --; executed only after both captures are ready')
    args = parser.parse_args()
    if args.command[:1] == ['--']:
        args.command = args.command[1:]
    if not args.context.startswith('kind-') or len(set(args.target)) != 2 or len(args.target) != 2 or not args.command or not 60 <= args.timeout <= 86400:
        parser.error('require a kind context, two distinct targets, a command, and timeout 60..86400')
    if any(not name or '/' in name or name.startswith('-') for name in args.target):
        parser.error('targets must be Helm release names')
    os.umask(0o077)
    args.output = args.output.expanduser().resolve()
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    guard = Guard(args)
    process = None
    def interrupted(number, frame):
        raise InterruptedError(f'traffic guard received signal {number}')
    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGTERM, signal.SIGHUP)}
    try:
        guard.start()
        process = subprocess.Popen(args.command, stdin=subprocess.DEVNULL, start_new_session=True)
        code = process.wait(timeout=args.timeout)
        if code:
            raise subprocess.CalledProcessError(code, args.command)
        guard.command_succeeded = True
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            guard.stop()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


if __name__ == '__main__':
    if sys.argv[1:] == ['--capture']:
        capture()
    else:
        main()
