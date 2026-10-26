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
SPEC = importlib.util.spec_from_file_location('build_imager', ROOT / 'dev/deployment/lab/build-imager.py')
IMAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGER)


class ImagerArtifactsTest(unittest.TestCase):
    def test_native_profiles_preserve_installer_and_hardware_sources(self):
        for architecture, profile in [('aarch64', 'qcow-imager-aarch64'), ('x86_64', 'qcow-imager')]:
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                original = (ROOT / 'pxe/mkosi.profiles' / profile / 'mkosi.conf').read_bytes()
                self.assertEqual(IMAGER.stage_profile(ROOT, work, architecture), profile)
                staged = work / 'mkosi.profiles' / profile
                self.assertEqual((staged / 'mkosi.conf').read_bytes(), original)
                self.assertEqual((ROOT / 'pxe/mkosi.profiles' / profile / 'mkosi.conf').read_bytes(), original)
                self.assertEqual((staged / 'mkosi.extra/opt/forge/disk_imaging.sh').read_bytes(),
                                 (ROOT / 'pxe/common_files/disk_imaging.sh').read_bytes())
                self.assertTrue((staged / 'mkosi.extra/opt/forge/disk_imaging.sh').stat().st_mode & 0o111)
                self.assertEqual((staged / 'mkosi.extra/etc/systemd/resolved.conf.d/90-nico-lab.conf').read_text(),
                                 '[Resolve]\nDNS=10.96.0.10\nDomains=~cluster.local\n')

    def test_ipv6_profile_does_not_pin_an_ipv4_cluster_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            profile = IMAGER.stage_profile(ROOT, work, 'x86_64', ipv6_boot=True)
            self.assertFalse((work / 'mkosi.profiles' / profile /
                'mkosi.extra/etc/systemd/resolved.conf.d/90-nico-lab.conf').exists())

    def test_existing_output_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mkosi = root / 'pxe/mkosi/bin/mkosi'
            mkosi.parent.mkdir(parents=True)
            mkosi.touch()
            output = root / 'output'
            output.mkdir()
            (output / 'keep').write_text('prior artifact')
            with patch.object(IMAGER, 'REPO', root), patch.object(IMAGER.platform, 'system', return_value='Linux'), \
                    patch.object(IMAGER.platform, 'machine', return_value='aarch64'), \
                    patch.object(IMAGER.os, 'geteuid', return_value=0), patch.object(IMAGER, 'stage_profile') as stage, \
                    patch.object(sys, 'argv', ['build-imager', '--output', str(output)]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                IMAGER.main()
            stage.assert_not_called()
            self.assertEqual((output / 'keep').read_text(), 'prior artifact')

    def test_build_failure_publishes_no_payload_or_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mkosi = root / 'pxe/mkosi/bin/mkosi'
            mkosi.parent.mkdir(parents=True)
            mkosi.touch()
            output = root / 'output'
            with patch.object(IMAGER, 'REPO', root), patch.object(IMAGER.platform, 'system', return_value='Linux'), \
                    patch.object(IMAGER.platform, 'machine', return_value='x86_64'), \
                    patch.object(IMAGER.os, 'geteuid', return_value=0), patch.object(IMAGER, 'stage_profile', return_value='qcow-imager'), \
                    patch.object(IMAGER.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'mkosi')), \
                    patch.object(sys, 'argv', ['build-imager', '--output', str(output)]), \
                    self.assertRaises(subprocess.CalledProcessError):
                IMAGER.main()
            self.assertTrue((output / 'build').is_dir())
            self.assertFalse((output / 'x86_64').exists())
            self.assertFalse((output / 'build-info.json').exists())
