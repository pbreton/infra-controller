#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Own one native libvirt guest and its standalone BMC for a Kubernetes pod."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import preflight

ROOT = Path('/data')
READY = Path('/run/nico-target-ready')
DOMAIN = 'target'
STOP = False


def run(*args, timeout=30):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, timeout=timeout).stdout.strip()


def virsh(*args, timeout=30):
    return run('virsh', '-c', 'qemu:///system', *args, timeout=timeout)


def configuration(value):
    required = {'clusterUID', 'owner', 'nodeName', 'bmcAddress', 'architecture', 'instanceIndex', 'memoryMiB', 'cpus', 'diskGiB', 'bridgeAddress', 'bmcPort'}
    if set(value) != required:
        raise ValueError('target configuration must contain exactly: ' + ', '.join(sorted(required)))
    if value['architecture'] not in ('amd64', 'arm64'):
        raise ValueError('architecture must be amd64 or arm64')
    for name, lower, upper in [('instanceIndex', 1, 255), ('memoryMiB', 1024, 65536),
                               ('cpus', 1, 32), ('diskGiB', 8, 1024), ('bmcPort', 1024, 65535)]:
        if type(value[name]) is not int or not lower <= value[name] <= upper:
            raise ValueError(f'{name} must be an integer from {lower} to {upper}')
    for key in ('clusterUID', 'owner', 'nodeName'):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 253:
            raise ValueError(f'{key} must be a nonempty identity of at most 253 characters')
    ipaddress.IPv4Address(value['bmcAddress'])
    network = ipaddress.IPv4Interface(value['bridgeAddress'])
    if network.ip in (network.network.network_address, network.network.broadcast_address) or network.network.prefixlen > 30:
        raise ValueError('bridgeAddress must be a usable IPv4 gateway address with prefix /30 or shorter')
    identity = value['clusterUID'] + '/' + value['owner']
    return dict(value, version=1, uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                bridge='nvt' + hashlib.sha256(identity.encode()).hexdigest()[:10],
                mac=f'02:00:00:{value["instanceIndex"]:02x}:00:02',
                serial=f'020000{value["instanceIndex"]:02x}0001')


def claim(root, config):
    inventory = root / 'inventory.json'
    if inventory.exists():
        if json.loads(inventory.read_text()) != config:
            raise ValueError('retained target identity or sizing differs; use its original configuration')
    else:
        if any(path.name not in ('.lock', 'lost+found') for path in root.iterdir()):
            raise ValueError('refusing to adopt unrecognized nonempty target storage')
        with inventory.open('x') as stream:
            json.dump(config, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


@contextmanager
def bound_storage(root, config):
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        claim(root, config)
        yield


def domain_xml(config, root=ROOT):
    arm = config['architecture'] == 'arm64'
    architecture = 'aarch64' if arm else 'x86_64'
    firmware = '/usr/share/AAVMF/AAVMF_CODE.fd' if arm else '/usr/share/OVMF/OVMF_CODE_4M.fd'
    domain = ET.Element('domain', type='kvm')

    def node(parent, tag, text=None, **attributes):
        element = ET.SubElement(parent, tag, attributes)
        element.text = str(text) if text is not None else None
        return element

    node(domain, 'name', DOMAIN)
    node(domain, 'uuid', config['uuid'])
    node(domain, 'memory', config['memoryMiB'], unit='MiB')
    node(domain, 'vcpu', config['cpus'])
    operating_system = node(domain, 'os')
    node(operating_system, 'type', 'hvm', arch=architecture, machine='virt' if arm else 'q35')
    node(operating_system, 'loader', firmware, readonly='yes', type='pflash')
    node(operating_system, 'nvram', root / 'disks/nvram.fd')
    node(operating_system, 'boot', dev='network')
    node(operating_system, 'boot', dev='hd')
    node(operating_system, 'smbios', mode='sysinfo')
    system = node(node(domain, 'sysinfo', type='smbios'), 'system')
    node(system, 'entry', config['serial'], name='serial')
    node(system, 'entry', config['uuid'], name='uuid')
    node(node(domain, 'features'), 'acpi')
    node(domain, 'cpu', mode='host-passthrough')
    node(domain, 'on_poweroff', 'destroy')
    node(domain, 'on_reboot', 'restart')
    devices = node(domain, 'devices')
    node(devices, 'emulator', f'/usr/bin/qemu-system-{architecture}')
    disk = node(devices, 'disk', type='file', device='disk')
    node(disk, 'driver', name='qemu', type='qcow2')
    node(disk, 'source', file=str(root / 'disks/root.qcow2'))
    node(disk, 'target', dev='vda', bus='virtio')
    nic = node(devices, 'interface', type='bridge')
    node(nic, 'mac', address=config['mac'])
    node(nic, 'source', bridge=config['bridge'])
    node(nic, 'model', type='virtio')
    if arm:
        node(nic, 'rom', enabled='no')
    else:
        node(nic, 'rom', enabled='yes', file='/usr/lib/ipxe/qemu/efi-virtio.rom')
    console = node(devices, 'console', type='pty')
    node(console, 'log', file=str(root / 'logs/console.log'), append='on')
    node(console, 'target', type='serial', port='0')
    return ET.tostring(domain, encoding='unicode')


def prepare_storage(config):
    for directory in ('disks', 'domains', 'libvirt', 'logs'):
        (ROOT / directory).mkdir(exist_ok=True)
    disk, nvram = ROOT / 'disks/root.qcow2', ROOT / 'disks/nvram.fd'
    if (ROOT / 'initialized').exists() and (not disk.is_file() or not nvram.is_file()):
        raise ValueError('initialized target is missing its disk or firmware state; restore retained data')
    if (ROOT / 'bmc-initialized').exists() and not (ROOT / 'bmc.json').is_file():
        raise ValueError('initialized BMC is missing its credentials; restore retained data')
    autostart = ROOT / 'domains/autostart'
    if autostart.exists() and any(autostart.iterdir()):
        raise ValueError('target domain autostart is unsupported; power must remain under BMC control')
    if not disk.exists():
        run('qemu-img', 'create', '-f', 'qcow2', str(disk), f'{config["diskGiB"]}G')
    if not nvram.exists():
        template = '/usr/share/AAVMF/AAVMF_VARS.fd' if config['architecture'] == 'arm64' else '/usr/share/OVMF/OVMF_VARS_4M.fd'
        shutil.copyfile(template, nvram)


def check_network(config):
    links = json.loads(run('ip', '-json', 'link', 'show'))
    existing = next((link for link in links if link['ifname'] == config['bridge']), None)
    if existing and existing.get('ifalias') != config['uuid']:
        raise ValueError('target bridge exists with a different owner')
    subnet = ipaddress.ip_interface(config['bridgeAddress']).network
    for route in json.loads(run('ip', '-4', '-json', 'route', 'show', 'table', 'all')):
        if route.get('dev') == config['bridge'] or route['dst'] == 'default':
            continue
        if subnet.overlaps(ipaddress.ip_network(route['dst'], strict=False)):
            raise ValueError('target subnet overlaps an existing route: ' + route['dst'])
    return existing


def check_bmc_port(config):
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(("::", config["bmcPort"]))


def bridge(config):
    existing = check_network(config)
    if not existing:
        run('ip', 'link', 'add', config['bridge'], 'type', 'bridge')
        try:
            run('ip', 'link', 'set', config['bridge'], 'alias', config['uuid'])
        except (subprocess.SubprocessError, OSError):
            run('ip', 'link', 'delete', config['bridge'])
            raise
    run('ip', 'address', 'replace', config['bridgeAddress'], 'dev', config['bridge'])
    run('ip', 'link', 'set', config['bridge'], 'up')


def clear_bridge(config):
    links = json.loads(run('ip', '-json', 'link', 'show'))
    owned = next((link for link in links if link['ifname'] == config['bridge']), None)
    if owned and owned.get('ifalias') == config['uuid']:
        attached = json.loads(run('ip', '-json', 'link', 'show', 'master', config['bridge']))
        if not attached:
            run('ip', 'link', 'delete', config['bridge'])


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def serve(config):
    children = []
    domain_ready = False
    try:
        prepare_storage(config)
        Path('/run/libvirt').mkdir(exist_ok=True)
        bridge(config)
        children.append(subprocess.Popen(['virtlogd']))
        children.append(subprocess.Popen(['libvirtd']))
        for attempt in range(60):
            if any(child.poll() is not None for child in children):
                raise RuntimeError('libvirt runtime exited during startup')
            try:
                virsh('list', '--all')
                break
            except subprocess.CalledProcessError:
                if STOP or attempt == 59:
                    raise
                time.sleep(1)
        domains = virsh('list', '--all', '--name').splitlines()
        if domains and domains != [DOMAIN]:
            raise ValueError('unexpected domains in retained target storage')
        if not domains:
            if (ROOT / 'initialized').exists():
                raise ValueError('initialized target is missing its domain; restore retained data')
            xml = ROOT / 'domain.xml'
            xml.write_text(domain_xml(config))
            virsh('define', str(xml))
        if virsh('domuuid', DOMAIN) != config['uuid']:
            raise ValueError('retained libvirt domain identity differs')
        domain_ready = True
        (ROOT / 'initialized').touch()
        # A new target stays off. Only Redfish controls power-on or saved-state restoration.
        children.append(subprocess.Popen(['bmc-mock', '--machine-role', 'host', '--hardware-profile', 'generic_ami',
            '--dpu-count', '0', '--instance-index', str(config['instanceIndex']), '--port', str(config['bmcPort']),
            '--redfish-auth', '--state-backend', 'libvirt', '--libvirt-domain', DOMAIN,
            '--state-file', str(ROOT / 'bmc.json')]))
        for attempt in range(60):
            if STOP or any(child.poll() is not None for child in children):
                raise RuntimeError('target runtime stopped during BMC startup')
            try:
                with socket.create_connection(('127.0.0.1', config['bmcPort']), timeout=1):
                    break
            except OSError:
                if attempt == 59:
                    raise
                time.sleep(1)
        if not (ROOT / 'bmc.json').is_file():
            raise ValueError('BMC did not persist initial state')
        (ROOT / 'bmc-initialized').touch()
        READY.touch()
        while not STOP:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('target child process exited')
            time.sleep(1)
    finally:
        READY.unlink(missing_ok=True)
        # Stop the BMC writer before saving the guest or stopping libvirt.
        if len(children) == 3:
            stop_child(children.pop())
        if domain_ready:
            try:
                if virsh('domstate', DOMAIN) in ('running', 'paused'):
                    virsh('managedsave', DOMAIN, timeout=150)
            except (subprocess.SubprocessError, OSError) as error:
                print(f'guest save failed; memory may be lost: {error}', file=sys.stderr)
        for child in reversed(children):
            stop_child(child)
        # Retain a bridge with live TAPs after an unsuccessful guest save.
        clear_bridge(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = configuration(json.loads(args.config.read_text()))
    if preflight.ARCHITECTURES.get(platform.machine()) != config['architecture']:
        raise ValueError('target architecture must match the native runtime')
    if os.environ.get('NODE_NAME') != config['nodeName']:
        raise ValueError('target must run on its configured Kubernetes node')
    preflight.check_kvm(config['architecture'])
    # Reject fixable host preconditions before binding fresh storage to these values.
    check_network(config)
    check_bmc_port(config)
    os.umask(0o077)
    with bound_storage(ROOT, config):
        serve(config)


def stopping(_signal, _frame):
    global STOP
    STOP = True


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'target runtime failed: {error}', file=sys.stderr)
        sys.exit(1)
