# Independent Helm lab validation

Use this procedure for the second fresh lab in the [validation workflow](validation.md).
It deploys Core prerequisites and services directly with Helm. It uses the same
native images, charts, development values, BMC site configuration and ownership
helpers as Tilt; no Tilt process creates this cluster's resources. This recipe
covers IPv4 provisioning followed by dual-stack installed guests. It does not
exercise an IPv6-only control plane or a DPU.

Run on native Linux ARM64 or AMD64 with KVM, the [installed host tools](README.md#ubuntu-host-setup),
Python 3 with PyYAML, and the capacity checks from the validation workflow.
Two default targets need 8 GiB of reserved huge pages. If another pair is running,
stop it through its lifecycle first or explicitly reserve additional capacity;
independent kind nodes share the host pool. Do not reclaim pages from an occupied pool or reuse target disks from another
cluster.

The workflow requires two labs: this dual-stack Helm run and the guarded
IPv6-only Tilt run. Each completes two instance generations. Additional runs
that vary only the target frontend are optional.

## Inputs and fresh cluster

Build or select native API, BMC proxy, DHCP and PXE images through
[`make images-dev`](../tilt/README.md#image-builds).
Record their source revision, image IDs and any integration dependencies. Include
the documented DHCPv6 handoff dependency and verify its actual runtime package.
The image references below are examples: set them to the images you built.
Build Scout and imager **without** `--ipv6-boot`, then package the verified OS with
`build-os-artifact.py`. Retain that bundle's manifest; the PXE init container
rejects an IPv6 boot-profile marker in this default-profile run.

```bash
export LAB_NAME=nico-helm-validation
export LAB_CONTEXT="kind-$LAB_NAME"
export LAB_STATE="$HOME/nico-helm-validation"
export KUBECONFIG="$LAB_STATE/kubeconfig"
export LOCAL_DEV_KUBE_CONTEXT="$LAB_CONTEXT"
export LAB_API_IMAGE=nico-dev-api:validation
export LAB_BMC_PROXY_IMAGE=nico-dev-bmc-proxy:validation
export LAB_DHCP_IMAGE=nico-dev-dhcp:validation
export LAB_PXE_IMAGE=nico-dev-pxe:validation
export LAB_OS_MANIFEST="$HOME/nico-default-os/build-info.json"
export LAB_BOOT_IMAGE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$LAB_OS_MANIFEST")
umask 077
mkdir "$LAB_STATE"
python3 -c 'import yaml'
bash dev/deployment/setup-kind.sh --name "$LAB_NAME" --ip-family dual \
  --kubeconfig "$KUBECONFIG"
docker exec "$LAB_NAME-control-plane" sysctl -w net.ipv6.conf.all.forwarding=1
```

## Render development values

Extract the shared baseline and select the BMC lab's real Scout configuration.
The explicit overlays below match the default-boot BMC profile: persistent Vault,
no REST database roles, fixed DHCPv4/PXE Services, and an additional DHCPv6 relay
Service. Image references must have explicit tags. Generated values contain
local development configuration; keep them with the run evidence.

```bash
python3 - "$LAB_STATE" <<'PY'
import copy
import json
import os
import platform
import sys
from pathlib import Path

import yaml

out = Path(sys.argv[1])
source = yaml.safe_load(Path('dev/deployment/tilt/values.yaml').read_text())
images = {
    name: os.environ[key] for name, key in [
        ('nico-api', 'LAB_API_IMAGE'), ('nico-bmc-proxy', 'LAB_BMC_PROXY_IMAGE'),
        ('nico-dhcp', 'LAB_DHCP_IMAGE'), ('nico-pxe', 'LAB_PXE_IMAGE'),
    ]
}


def save(name, value):
    (out / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')


for name, key in [('cert-manager', 'certManager'), ('cnpg', 'cnpgOperator'), ('postgres', 'postgres')]:
    value = copy.deepcopy(source['tilt'][key])
    if name == 'postgres':
        value['cluster']['roles'] = []
    save(name, value)

for name in ['nico-api', 'nico-bmc-proxy', 'nico-dhcp']:
    value = {'global': copy.deepcopy(source['global']), **copy.deepcopy(source[name])}
    repository, tag = images[name].rsplit(':', 1)
    if name == 'nico-bmc-proxy':
        value['image'] = {'repository': repository, 'tag': tag}
    else:
        value['global']['image'].update(repository=repository, tag=tag)
    if name == 'nico-api':
        value['extraEnv'] = []
        value['siteConfig'] = {
            'enabled': True,
            'nicoApiSiteConfig': Path('dev/deployment/tilt/bmc-lab.toml').read_text(),
        }
    if name == 'nico-dhcp':
        value['config']['kea']['hookParameters'] = {
            'nameservers': '10.96.0.10', 'ntpServer': '127.0.0.1',
            'provisioningServer': '10.96.0.30',
        }
        value['dhcp'] = {'v6Enabled': True, 'v6ServerIdentifier': '020000000006'}
    save(name, value)

arch = {'aarch64': 'aarch64', 'x86_64': 'x86_64'}[platform.machine()]
repository, tag = images['nico-pxe'].rsplit(':', 1)
command = (
    'for name in scout qcow-imager; do actual=default; '
    f'if test -f /{arch}/$name.network-profile; then '
    f'actual=$(cat /{arch}/$name.network-profile); fi; '
    f'test "$actual" = default; done; cp -r /{arch} /forge-boot-artifacts/blobs/internal/'
)
save('nico-pxe', {
    'global': source['global'], 'image': {'repository': repository, 'tag': tag},
    'legacyAlias': {'enabled': False},
    'service': {'clusterIP': '10.96.0.30', 'port': 8080, 'bootPort': 80},
    'config': {'extraEnvData': {
        'CARBIDE_API_URL': 'https://nico-api.nico-system.svc.cluster.local:1079',
        'CARBIDE_PXE_URL': 'http://nico-pxe.nico-system.svc.cluster.local',
    }},
    'bootArtifactContainers': [{
        'name': 'scout-artifacts', 'image': os.environ['LAB_BOOT_IMAGE'],
        'imagePullPolicy': 'IfNotPresent', 'command': ['sh', '-ec', command],
    }],
})
(out / 'images.txt').write_text('\n'.join(images.values()) + '\n' + os.environ['LAB_BOOT_IMAGE'] + '\n')
PY
while IFS= read -r image; do
  docker image inspect "$image" --format '{{.Id}} {{json .RepoTags}}' >> "$LAB_STATE/image-ids.txt"
  kind load docker-image "$image" --name "$LAB_NAME"
done < "$LAB_STATE/images.txt"
```

## Install prerequisites and Core

The chart versions below are the tested dependency versions selected by the
[stack Tiltfile](../tilt/Tiltfile). Use its matching versions when changing the
shared baseline. Cert-manager creates the fixture-backed local trust; persistent
Vault supplies a separate public admin CA. Both fixture keys are public local
development inputs. Do not use this trust setup outside an isolated lab.

Wait for Vault's PVC and pod explicitly before initialization. Its StatefulSet
uses the OnDelete update strategy, so `kubectl rollout status` is not its
readiness check. The bootstrap helper retains generated Vault credentials in
owned Kubernetes Secrets and refuses incompatible retained state. The `certs`
PKI mount is selected by `helm-prereqs/values/local-development.yaml`.

```bash
(
set -euo pipefail
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo add cnpg https://cloudnative-pg.github.io/charts --force-update
helm repo add hashicorp https://helm.releases.hashicorp.com --force-update
helm repo update jetstack cnpg hashicorp
helm upgrade --install cert-manager jetstack/cert-manager --version v1.21.1 --namespace cert-manager --create-namespace -f "$LAB_STATE/cert-manager.json" --wait --timeout 5m
helm upgrade --install cnpg cnpg/cloudnative-pg --version 0.29.0 --namespace cnpg-system --create-namespace -f "$LAB_STATE/cnpg.json" --wait --timeout 5m
helm upgrade --install nico-vault hashicorp/vault --version 0.34.0 --namespace nico-system --create-namespace -f dev/deployment/tilt/vault-persistent.yaml --wait --timeout 5m
kubectl -n nico-system wait pvc/data-nico-vault-0 --for=create --timeout=300s
kubectl -n nico-system wait pvc/data-nico-vault-0 --for=jsonpath='{.status.phase}'=Bound --timeout=300s
kubectl -n nico-system wait pod/nico-vault-0 --for=create --timeout=300s
kubectl -n nico-system wait pod/nico-vault-0 --for=condition=Ready --timeout=300s
python3 dev/deployment/tilt/vault-bootstrap.py --context "$LAB_CONTEXT" --namespace nico-system bootstrap
helm upgrade --install nico-core-prereqs helm-prereqs --namespace nico-system --wait --timeout 5m -f helm-prereqs/values/local-development.yaml --set vault.tokenSecret.create=false \
 --set-file certManager.localCA.certificate=dev/certs/localhost/ca.crt --set-file certManager.localCA.privateKey=dev/certs/localhost/ca.key \
 --set-file sshHostKey.privateKey=crates/ssh-console/tests/fixtures/ssh_host_ed25519_key --set-file sshHostKey.publicKey=crates/ssh-console/tests/fixtures/ssh_host_ed25519_key.pub
python3 dev/deployment/tilt/site-mode.py --context "$LAB_CONTEXT" --namespace nico-system --mode bmc claim
python3 dev/deployment/tilt/vault-admin-ca.py --context "$LAB_CONTEXT" --namespace nico-system --pki-mount certs --output "$LAB_STATE/vault-admin-ca.json"
helm upgrade --install nico-postgres cnpg/cluster --version 0.8.1 --namespace nico-system -f "$LAB_STATE/postgres.json" --wait --timeout 5m
kubectl -n nico-system wait cluster/nico-pg-cluster --for=condition=Ready --timeout=300s
helm upgrade --install nico-api helm/charts/nico-api --namespace nico-system -f "$LAB_STATE/nico-api.json" -f "$LAB_STATE/vault-admin-ca.json" --wait --wait-for-jobs --timeout 5m
for component in nico-bmc-proxy nico-pxe nico-dhcp; do
 helm upgrade --install "$component" "helm/charts/$component" --namespace nico-system -f "$LAB_STATE/$component.json" --wait --wait-for-jobs --timeout 5m
done
export LOCAL_DEV_KUBE_CONTEXT="$LAB_CONTEXT"
bash dev/deployment/core-admin.sh -f json machine show
)
```

## Two targets and dual-stack lifecycle

Continue with [two fresh targets](validation.md#two-fresh-targets), using this
cluster's UID, node and live DHCPv6 Service, but stop before the target Tilt
command. In each generated target values file,
enable IPv4 provisioning with the shared DHCP endpoint and the node's IPv4 pod
CIDR. Keep the already generated IPv6 relay and bridge settings:

```bash
python3 - "$LAB_STATE/targets" <<'PYTHON'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
node = json.loads((root / 'nodes.json').read_text())['items'][0]
cidr = next(c for c in node['spec']['podCIDRs'] if ':' not in c)
for offset, name in enumerate(('target-one', 'target-two'), start=12):
    path = root / (name + '.json')
    values = json.loads(path.read_text())
    values['service']['clusterIP'] = f'10.96.40.{offset}'
    values['network']['provisioning'] = {
        'enabled': True, 'dhcpServer': '10.96.0.20', 'podCIDR': cidr,
    }
    path.write_text(json.dumps(values, indent=2) + '\n')
PYTHON
```

Make that edit **before** deploying either target. It selects unused IPv4 BMC
Services `10.96.40.12` and `10.96.40.13`, within kind's Service CIDR; verify they
are free in the new cluster. Build/load the native target image and deploy both
releases with their generated values:

```bash
(
set -euo pipefail
make images-dev DEV_TARGET=bmc-target DEV_IMAGE=nico-bmc-target:validation
kind load docker-image nico-bmc-target:validation --name "$LAB_NAME"
for release in target-one target-two; do
  helm --kube-context "$LAB_CONTEXT" upgrade --install "$release" helm/nico-bmc-target \
    --namespace bmc-lab --values "$LAB_STATE/targets/$release.json" \
    --set image.tag=validation --wait --timeout 5m
done
)
```

Follow the registration step with `--network dual-stack` and
`--bmc-prefix 10.96.40.0/24` in place of its IPv6-only profile and IPv6 BMC prefix.
Both address families are registered for the guests; the BMCs use IPv4. Never
change a registered target's BMC identity in place.

Create the catalog using this default-profile origin:

```bash
python3 dev/deployment/lab/catalog.py --context "$LAB_CONTEXT" \
  --state-dir "$LAB_STATE/catalog" --artifact-manifest "$LAB_OS_MANIFEST" \
  --pxe-origin http://nico-pxe.nico-system.svc.cluster.local
python3 dev/deployment/lab/catalog.py --state-dir "$LAB_STATE/catalog" --resume > "$LAB_STATE/catalog-result.json"
```

Select the observed machine IDs and catalog IDs as in the fresh validation
procedure. Invoke its lifecycle command directly with `--network dual-stack
--cycles 2`; omit the IPv6-only traffic guard because IPv4 is intentionally
required in this run. The runner verifies finite leases and peer TCP separately
for both address families, reboots both guests, releases them to Ready/live Scout,
and repeats. Add `--internet-ipv6` on a host with external IPv6 routing. A successful
IPv6-only run cannot substitute for these IPv4 checks.

Retain the completed journal, image/source provenance and generated chart values.
Use the same explicit stop/discard procedure after success. Removing Helm releases
does not automatically delete the separately created target PVCs or return the
host's huge-page pool to its original size.
