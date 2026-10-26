# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('os_artifact', ROOT / 'lab/build-os-artifact.py')
OS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OS)
PAYLOAD = b'QFI\xfb' + b'fixture qcow2'


class OsArtifactTest(unittest.TestCase):
    def test_build_records_verified_source_and_native_parent_without_changing_parent_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(output=Path(directory) / 'output', boot_artifact_image='boot:fixture',
                                      image='os:fixture', source_url='https://images.example/ubuntu.img',
                                      sha256=hashlib.sha256(PAYLOAD).hexdigest())
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                result = ''
                if command[1] == 'info': result = json.dumps({'OSType': 'linux', 'Architecture': 'aarch64'})
                elif command[1:3] == ['image', 'inspect']:
                    result = json.dumps([{'Id': 'sha256:parent' if command[3] == 'boot:fixture' else 'sha256:built', 'Os': 'linux', 'Architecture': 'arm64'}])
                elif command[0] == 'curl': Path(command[command.index('--output') + 1]).write_bytes(PAYLOAD)
                elif command[1] == 'build':
                    self.assertEqual(sorted(p.name for p in Path(command[-1]).iterdir()), ['ubuntu.img'])
                    self.assertIn('ARTIFACT_ARCH=aarch64', command)
                return subprocess.CompletedProcess(command, 0, stdout=result)
            with patch.object(OS.platform, 'system', return_value='Linux'), patch.object(OS.platform, 'machine', return_value='aarch64'), patch.object(OS, 'run', side_effect=run):
                OS.build(args)
            manifest = json.loads((args.output / 'build-info.json').read_text())
            self.assertEqual(manifest['sha256'], args.sha256)
            self.assertEqual(manifest['boot_artifact_image_id'], 'sha256:parent')
            self.assertEqual(manifest['image_id'], 'sha256:built')
            self.assertEqual(manifest['pxe_path'], '/public/blobs/internal/aarch64/ubuntu.img')
            self.assertEqual((args.output / 'ubuntu.img').read_bytes(), PAYLOAD)
            tags = [c for c in calls if c[1] == 'tag']
            self.assertEqual(len(tags), 1)
            self.assertEqual(tags[0][2], 'sha256:parent')
            self.assertEqual(calls[-1], ['docker', 'image', 'rm', tags[0][3]])

    def test_untrusted_or_wrong_format_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image'
            for content, digest, reason in [(PAYLOAD, '0' * 64, 'SHA256 mismatch'),
                                             (b'raw image', hashlib.sha256(b'raw image').hexdigest(), 'qcow2')]:
                with self.subTest(reason=reason):
                    image.write_bytes(content)
                    with self.assertRaisesRegex(ValueError, reason): OS.verify_image(image, digest)

    def test_native_mismatch_and_existing_tag_fail_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(output=Path(directory) / 'output', boot_artifact_image='boot:fixture', image='os:fixture')
            for arch, tags, message in [('amd64', '', 'architecture'), ('arm64', 'os:fixture\n', 'already exists')]:
                with self.subTest(message=message):
                    def run(command, **kwargs):
                        if command[1] == 'info': result = {'OSType': 'linux', 'Architecture': 'aarch64'}
                        elif command[1:3] == ['image', 'inspect']: result = [{'Id': 'parent', 'Os': 'linux', 'Architecture': arch}]
                        elif command[1:3] == ['image', 'ls']: return subprocess.CompletedProcess(command, 0, stdout=tags)
                        else: self.fail('unexpected mutation: ' + str(command))
                        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result))
                    with patch.object(OS.platform, 'system', return_value='Linux'), patch.object(OS.platform, 'machine', return_value='aarch64'), patch.object(OS, 'run', side_effect=run), self.assertRaisesRegex(ValueError, message): OS.build(args)
                    self.assertFalse(args.output.exists())

    def test_source_urls_do_not_embed_credentials_or_query_tokens(self):
        for value in ('https://user:secret@example/image', 'https://example/image?token=secret', 'file:///image', 'https://example/image#fragment'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError): OS.source_url(value)
        self.assertEqual(OS.source_url('http://[fd00::1]:8080/image'), 'http://[fd00::1]:8080/image')


if __name__ == '__main__': unittest.main()
