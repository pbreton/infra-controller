# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'lab'
SPEC = importlib.util.spec_from_file_location('lab_catalog', ROOT / 'catalog.py')
CAT = importlib.util.module_from_spec(SPEC)
with patch.object(sys, 'path', [str(ROOT), *sys.path]):
    SPEC.loader.exec_module(CAT)
OS_ID = '81240036-0000-4000-8000-000000000001'
VPC_ID = '81240036-0000-4000-8000-000000000002'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = dict(version=1, context='kind-test', namespace='nico-system', cluster='cluster', owner='lab-test',
                          os_id=OS_ID, artifact={'sha256': '0' * 64, 'architecture': 'arm64'}, url='http://pxe/image')
        self.image = {'attributes': dict(id={'value': OS_ID}, source_url='http://pxe/image', digest='0' * 64,
                                        tenant_organization_id='lab-test', name='lab-test-os', description='lab-test',
                                        boot_disk='/dev/vda', create_volume=False), 'status': CAT.IMAGE_READY}
        self.vpc = dict(id=VPC_ID, metadata=dict(name='lab-test-vpc', description='lab-test'), deleted=None,
                        config=dict(tenant_organization_id='lab-test', network_virtualization_type=CAT.FLAT, slaac_enabled=False))

    def test_new_catalog_then_resume_reuses_exact_resources_without_repeating_writes(self):
        lab = CAT.Catalog(self.directory, self.state)
        lab.verify_cluster = Mock()
        images, vpcs, writes = [], [], []
        def admin(*args, read=False, operation=None):
            if read:
                return images if args[0] == 'os-image' else {'vpcs': vpcs}
            saved = json.loads((self.directory / 'state.json').read_text())
            self.assertTrue(saved[('os' if args[0] == 'os-image' else 'vpc') + '_intent'])
            writes.append(args)
            (images if args[0] == 'os-image' else vpcs).append(copy.deepcopy(self.image if args[0] == 'os-image' else self.vpc))
        lab.admin = Mock(side_effect=admin)
        with contextlib.redirect_stdout(io.StringIO()):
            lab.apply()
            lab.apply()
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.state['vpc_id'], VPC_ID)
        self.assertTrue(self.state['complete'])
        self.assertIn('--boot-disk', writes[0])
        self.assertIn('flat', writes[1])

    def test_lost_vpc_response_is_recovered_only_when_unique_result_is_visible(self):
        for visible in (False, True):
            with self.subTest(visible=visible):
                lab = CAT.Catalog(self.directory, copy.deepcopy(self.state))
                resource = Mock(return_value=None)
                lab.admin = Mock(side_effect=RuntimeError('lost response'))
                with self.assertRaisesRegex(RuntimeError, 'lost response'):
                    lab.ensure('vpc', resource, ['vpc', 'create'])
                self.assertTrue(json.loads((self.directory / 'state.json').read_text())['vpc_intent'])
                lab.admin.reset_mock()
                resource.return_value = VPC_ID if visible else None
                if visible:
                    self.assertEqual(lab.ensure('vpc', resource, []), VPC_ID)
                else:
                    with self.assertRaisesRegex(ValueError, 'unknown outcome'):
                        lab.ensure('vpc', resource, [])
                lab.admin.assert_not_called()

    def test_unowned_duplicate_or_changed_vpc_cannot_be_adopted(self):
        lab = CAT.Catalog(self.directory, dict(self.state, vpc_id=VPC_ID))
        changed = copy.deepcopy(self.vpc)
        changed['config']['network_security_group_id'] = 'changed'
        for values in ([self.vpc, self.vpc], [dict(self.vpc, id=OS_ID)], [changed]):
            with self.subTest(values=values):
                lab.admin = Mock(return_value={'vpcs': values})
                with self.assertRaises(ValueError): lab.find_vpc()
        with self.assertRaisesRegex(ValueError, 'without this journal'):
            lab.ensure('vpc', Mock(return_value=VPC_ID), [])

    def test_changed_or_disabled_os_image_is_not_reused(self):
        lab = CAT.Catalog(self.directory, self.state)
        for changes in ({'digest': '1' * 64}, {'boot_disk': '/dev/vdb'}, {'auth_token': 'unexpected'}):
            image = copy.deepcopy(self.image)
            image['attributes'].update(changes)
            lab.admin = Mock(return_value=[image])
            with self.subTest(changes=changes), self.assertRaises(ValueError): lab.find_os()
        lab.admin = Mock(return_value=[dict(self.image, status=2)])
        with self.assertRaisesRegex(ValueError, 'not Ready'): lab.find_os()

    def test_changed_cluster_or_architecture_blocks_api_mutations(self):
        for uid, architecture in [('different', 'arm64'), ('cluster', 'amd64')]:
            lab = CAT.Catalog(self.directory, self.state)
            lab.admin = Mock()
            outputs = [json.dumps({'metadata': {'uid': uid}}), json.dumps({'items': [{'status': {'nodeInfo': {'architecture': architecture}}}]})]
            with self.subTest(uid=uid, architecture=architecture), patch.object(CAT, 'run', side_effect=outputs), self.assertRaises(ValueError):
                lab.apply()
            lab.admin.assert_not_called()

    def test_manifest_and_origin_keep_image_path_and_credentials_out_of_catalog_inputs(self):
        manifest = dict(architecture='arm64', artifact_directory='aarch64', pxe_path='/public/blobs/internal/aarch64/ubuntu.img',
                        sha256='0' * 64, image_id='output', boot_artifact_image_id='parent')
        self.assertEqual(CAT.artifact(manifest), manifest)
        for change in ({'pxe_path': '/other'}, {'architecture': 'amd64'}, {'sha256': 'not-a-digest'}):
            with self.subTest(change=change), self.assertRaises(ValueError): CAT.artifact(dict(manifest, **change))
        self.assertEqual(CAT.origin('http://[fd00::1]:8080/'), 'http://[fd00::1]:8080')
        for url in ('https://user:password@host', 'http://host/path', 'http://host?token=x'):
            with self.subTest(url=url), self.assertRaises(CAT.argparse.ArgumentTypeError): CAT.origin(url)


if __name__ == '__main__':
    unittest.main()
