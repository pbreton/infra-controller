# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('build_scout', ROOT / 'dev/deployment/lab/build-scout.py')
SCOUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCOUT)


class ScoutArtifactsTest(unittest.TestCase):
    def test_staging_preserves_real_scout_and_hardware_sources_on_both_architectures(self):
        for arch in ('aarch64', 'x86_64'):
            with self.subTest(architecture=arch), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                package = work / 'scout.deb'
                package.write_bytes(b'scout package fixture')
                originals = {profile: (ROOT / 'pxe/mkosi.profiles' / profile / 'mkosi.conf').read_bytes()
                             for profile in SCOUT.profiles(arch)}
                SCOUT.stage_profiles(ROOT, work, package, arch)
                for profile, original in originals.items():
                    self.assertEqual((ROOT / 'pxe/mkosi.profiles' / profile / 'mkosi.conf').read_bytes(), original)
                    staged = work / 'mkosi.profiles' / profile
                    lines = [line.strip() for line in (staged / 'mkosi.conf').read_text().splitlines()]
                    self.assertEqual([line for line in lines if line.startswith('linux-image-')], ['linux-image-virtual'])
                    self.assertFalse(any(line.startswith(('linux-headers-', 'linux-modules-', 'linux-firmware')) for line in lines))
                    self.assertEqual((staged / 'mkosi.extra/opt/forge/forge-scout-network.sh').read_bytes(),
                                     (ROOT / 'pxe/common_files/forge-scout-network.sh').read_bytes())
                rootfs = work / 'mkosi.profiles' / f'scout-oss-{arch}'
                self.assertEqual((rootfs / 'mkosi.extra/build-output/forge-scout.deb').read_bytes(), package.read_bytes())
                self.assertEqual((rootfs / 'mkosi.extra/etc/systemd/system/forge-scout.service').read_bytes(),
                                 (ROOT / 'crates/scout/misc/forge-scout.service').read_bytes())
                self.assertFalse((rootfs / 'mkosi.extra/etc/apt').exists())
                self.assertIn('mlx-firmware-supervisor', (rootfs / 'mkosi.postinst.chroot').read_text())
                self.assertTrue((rootfs / 'mkosi.postinst.chroot').stat().st_mode & 0o111)

    def test_changed_kernel_profile_contract_is_rejected(self):
        for config in ('[Content]\nPackages=\n curl\n', '[Content]\n linux-image-something\n'):
            with self.subTest(config=config), self.assertRaises(ValueError):
                SCOUT.vm_config(config)

    def test_invalid_package_or_existing_output_does_not_start_a_build(self):
        for package_arch, existing in [('amd64', False), ('arm64', True)]:
            with self.subTest(package_arch=package_arch, existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / 'scout.deb'
                package.touch()
                output = root / 'output'
                if existing:
                    output.mkdir()
                    (output / 'keep').write_text('retained artifact')
                mkosi = root / 'pxe/mkosi/bin/mkosi'
                mkosi.parent.mkdir(parents=True)
                mkosi.touch()
                with patch.object(SCOUT, 'REPO', root), patch.object(SCOUT.platform, 'machine', return_value='aarch64'), \
                        patch.object(SCOUT.platform, 'system', return_value='Linux'), patch.object(SCOUT.os, 'geteuid', return_value=0), \
                        patch.object(SCOUT.subprocess, 'check_output', side_effect=[package_arch, '0.0.0-test']), \
                        patch.object(SCOUT.subprocess, 'run') as run, \
                        patch.object(sys, 'argv', ['build-scout', '--scout-package', str(package), '--output', str(output)]), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        SCOUT.main()
                    self.assertEqual(error.exception.code, 2)
                    run.assert_not_called()
                if existing:
                    self.assertEqual((output / 'keep').read_text(), 'retained artifact')
                else:
                    self.assertFalse(output.exists())

    def test_failed_mkosi_build_does_not_publish_a_manifest_or_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'scout.deb'
            package.touch()
            output = root / 'output'
            mkosi = root / 'pxe/mkosi/bin/mkosi'
            mkosi.parent.mkdir(parents=True)
            mkosi.touch()
            with patch.object(SCOUT, 'REPO', root), patch.object(SCOUT, 'stage_profiles'), \
                    patch.object(SCOUT.platform, 'machine', return_value='x86_64'), \
                    patch.object(SCOUT.platform, 'system', return_value='Linux'), patch.object(SCOUT.os, 'geteuid', return_value=0), \
                    patch.object(SCOUT.subprocess, 'check_output', side_effect=['amd64', '0.0.0-test']), \
                    patch.object(SCOUT.subprocess, 'run', side_effect=[None, subprocess.CalledProcessError(1, 'mkosi')]), \
                    patch.object(sys, 'argv', ['build-scout', '--scout-package', str(package), '--output', str(output)]):
                with self.assertRaises(subprocess.CalledProcessError):
                    SCOUT.main()
            self.assertFalse((output / 'build-info.json').exists())
            self.assertFalse((output / 'x86_64').exists())
