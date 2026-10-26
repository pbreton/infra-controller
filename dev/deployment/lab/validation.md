# Fresh native lab validation

Use a dedicated, single-node kind cluster for each run. This procedure connects
the existing [native lab helpers](README.md),
[stack frontend](../tilt/README.md) and
[target chart](../../../helm/nico-bmc-target/README.md). Run commands from the
repository root on native Linux ARM64 or AMD64. On macOS, execute them inside the
[managed Linux VM](../vfkit/README.md); Docker, KVM and the target architecture
must agree.

## Inputs and capacity

Record the source revision and any unmerged integration dependencies. Build the
native Scout, imager and iPXE artifacts using the lab README, then package the
publisher-verified OS with `build-os-artifact.py`. Retain its `build-info.json`.
For IPv6 provisioning, build both boot stages with `--ipv6-boot` and use the
matching stack profile below. Include the
[DHCPv6 lease-handoff prerequisite](../tilt/README.md#dhcpv6-lease-handoff-build-dependency)
in the build source; merely selecting the profile does not add that fix.

Complete [host setup and preflight](README.md#ubuntu-host-setup). Reserve the
[8 GiB huge-page pool](README.md#explicit-huge-page-backing) before kind creation
for two default 4 GiB targets. Check the outer VM's physical backing volume as
well as the guest filesystem. Sparse disks and image-export layers can exhaust
either independently. Keep evidence outside `/tmp` so a VM reboot retains it.

This procedure deliberately does not reset existing clusters, reuse old target
PVCs or reassign host reservations. Each run needs a new cluster name, state
directory and target disks. Retained-state recovery uses the original values and
journals instead of this fresh procedure.

## Fresh IPv6 stack

Set the image to the OS bundle produced above and the manifest to that build's
actual output. The example prefixes below assume kind's default dual-stack
Service CIDRs and no conflicting guest routes inside its node.

```bash
export LAB_NAME=nico-acceptance
export LAB_CONTEXT="kind-$LAB_NAME"
export LAB_STATE="$HOME/nico-acceptance"
export KUBECONFIG="$LAB_STATE/kubeconfig"
export LAB_BOOT_IMAGE=nico-boot-artifacts:validation
export LAB_OS_MANIFEST="$HOME/nico-os-artifact/build-info.json"
mkdir "$LAB_STATE"
bash dev/deployment/setup-kind.sh --name "$LAB_NAME" --ip-family dual \
  --kubeconfig "$KUBECONFIG"
docker exec "$LAB_NAME-control-plane" sysctl -w net.ipv6.conf.all.forwarding=1
kind load docker-image "$LAB_BOOT_IMAGE" --name "$LAB_NAME"
tilt ci -f dev/deployment/tilt/Tiltfile --port 10372 --timeout 90m -- \
  --bmc-lab --persistent-vault --dhcpv6 --ipv6-endpoints --ipv6-boot \
  --boot-artifact-image "$LAB_BOOT_IMAGE"
```

Stack readiness is a prerequisite, not proof of instance provisioning. The
profile creates the IPv6 resolver and PXE endpoint, exports their live addresses,
and applies the resulting DHCP values. Keep the generated endpoint values and
actual container image IDs with the run evidence. Use a fresh image tag after
changing an artifact; retained tags can select cached payloads.

## Two fresh targets

Create the namespace and two independent PVCs, then generate chart values from
this cluster's identity and live DHCP endpoint. The example uses target indices
3 and 4, BMC addresses `fd00:10:96::4012` and `::4013`, and guest prefixes
`fd42:8124:42::/64` and `fd42:8124:43::/64`. Choose different nonoverlapping
prefixes if necessary. IPv4 bridge addresses are structural only: provisioning
is disabled and no DHCPv4 response is permitted in this profile.

```bash
mkdir "$LAB_STATE/targets"
kubectl --context "$LAB_CONTEXT" create namespace bmc-lab
kubectl --context "$LAB_CONTEXT" get namespace kube-system -o json > "$LAB_STATE/targets/cluster.json"
kubectl --context "$LAB_CONTEXT" get nodes -o json > "$LAB_STATE/targets/nodes.json"
kubectl --context "$LAB_CONTEXT" -n nico-system get service nico-dhcp6 -o json > "$LAB_STATE/targets/dhcp6.json"
python3 - "$LAB_STATE/targets" <<'PYTHON'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
nodes = json.loads((root / 'nodes.json').read_text())['items']
assert len(nodes) == 1
node = nodes[0]
uid = json.loads((root / 'cluster.json').read_text())['metadata']['uid']
server = json.loads((root / 'dhcp6.json').read_text())['spec']['clusterIP']
assert ':' in server
cidr = next(c for c in node['spec']['podCIDRs'] if ':' in c)
claims, manifest = [], {}
for name, index, subnet, bmc, port in [
    ('target-one', 3, 42, '4012', 2445),
    ('target-two', 4, 43, '4013', 2446),
]:
    values = dict(
        clusterUID=uid, nodeName=node['metadata']['name'],
        architecture=node['status']['nodeInfo']['architecture'],
        storage={'existingClaim': name + '-data'},
        target={'hugepages': True, 'instanceIndex': index},
        service={'clusterIP': 'fd00:10:96::' + bmc, 'targetPort': port},
        network={
            'bridgeAddress': f'10.84.{subnet}.1/24',
            'bridgeAddressV6': f'fd42:8124:{subnet}::1/64',
            'provisioning': {'enabled': False},
            'dhcpv6': {'server': server, 'podCIDR': cidr, 'defaultRoute': True},
        },
    )
    (root / (name + '.json')).write_text(json.dumps(values, indent=2) + '\n')
    manifest[name] = name + '.json'
    claims.append(dict(
        apiVersion='v1', kind='PersistentVolumeClaim',
        metadata={'name': name + '-data', 'namespace': 'bmc-lab'},
        spec={'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '40Gi'}}},
    ))
(root / 'targets.json').write_text(json.dumps(manifest, indent=2) + '\n')
(root / 'claims.json').write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': claims}) + '\n')
PYTHON
kubectl --context "$LAB_CONTEXT" create -f "$LAB_STATE/targets/claims.json"
tilt ci -f dev/deployment/lab/Tiltfile --port 10373 --timeout 60m -- \
  --targets "$LAB_STATE/targets/targets.json" --namespace bmc-lab
```

For the separate direct-Helm target run, create a new lab with the same chosen
prefixes and sizing, regenerate its cluster identity and live endpoint values,
and replace only the final target Tilt command with:

```bash
make images-dev DEV_TARGET=bmc-target DEV_IMAGE=nico-bmc-target:validation
kind load docker-image nico-bmc-target:validation --name "$LAB_NAME"
for release in target-one target-two; do
  helm --kube-context "$LAB_CONTEXT" upgrade --install "$release" helm/nico-bmc-target \
    --namespace bmc-lab --values "$LAB_STATE/targets/$release.json" \
    --set image.tag=validation --wait --timeout 5m
done
```

This comparison exercises the two target deployment frontends. The preceding
stack command still uses Tilt; it does not establish an independent full-stack
Helm bootstrap. Each target starts powered off with its development BMC fixture.
Registering it through Core begins real discovery and Scout admission.

## Registration and instance lifecycle

Keep the initial fixture password in a private file. Core subsequently rotates
it; resuming registration must not reset the rotated value.

```bash
umask 077
printf 'factory_password\n' > "$LAB_STATE/initial-bmc-password"
python3 dev/deployment/lab/register.py --context "$LAB_CONTEXT" \
  --state-dir "$LAB_STATE/registration" --target target-one --target target-two \
  --network ipv6-only --bmc-prefix fd00:10:96::4010/124 \
  --bmc-password-file "$LAB_STATE/initial-bmc-password"
python3 dev/deployment/lab/register.py --state-dir "$LAB_STATE/registration" --resume
```

Create and verify the owned OS/VPC catalog using the bundle's recorded digest:

```bash
python3 dev/deployment/lab/catalog.py --context "$LAB_CONTEXT" \
  --state-dir "$LAB_STATE/catalog" --artifact-manifest "$LAB_OS_MANIFEST" \
  --pxe-origin http://carbide-pxe.forge:8080
python3 dev/deployment/lab/catalog.py --state-dir "$LAB_STATE/catalog" --resume
export LOCAL_DEV_KUBE_CONTEXT="$LAB_CONTEXT"
bash dev/deployment/core-admin.sh -f json machine show
```

Match each machine's `bmc_info.ip` to its target's BMC address. Wait for Scout to
replace the provisional machine identity with the admitted host identity. Set
`FIRST_MACHINE` and `SECOND_MACHINE` to those two observed IDs. The lifecycle
runner verifies the cluster, BMC and target inventory again before allocation;
it waits for Ready and a live Scout stream. It never guesses or adopts a target.
Read `os_id` and `vpc_id` from the completed catalog journal:

```bash
export LAB_OS_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["os_id"])' "$LAB_STATE/catalog/state.json")
export LAB_VPC_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["vpc_id"])' "$LAB_STATE/catalog/state.json")
python3 dev/deployment/lab/traffic-guard.py --context "$LAB_CONTEXT" \
  --target target-one --target target-two --output "$LAB_STATE/traffic" \
  --timeout 14400 -- \
  python3 dev/deployment/lab/lifecycle.py --context "$LAB_CONTEXT" \
    --state-dir "$LAB_STATE/lifecycle" \
    --target "target-one=$FIRST_MACHINE" --target "target-two=$SECOND_MACHINE" \
    --vpc "$LAB_VPC_ID" --os-image "$LAB_OS_ID" --network ipv6-only --cycles 2
```

On ARM64 with working external routing, append `--internet-ipv6` to the
**lifecycle command**, after `--cycles 2`. It requires certificate-verified HTTPS
from each guest before and after reboot, in both generations. On AMD64 without
that route, omit it and record Internet IPv6 as unavailable, retaining all peer
and lifecycle checks. A capable AMD64 environment can use the same flag.

The two cycles install and verify both disks, check finite DHCP leases and peer
TCP in both directions, reboot each while preserving its peer, release both to
Ready/live Scout, then repeat with a new disk generation. The guard observes the
whole command, including boot and release. See the [lifecycle runner](README.md#repeat-the-instance-lifecycle)
and [traffic guard](README.md#continuous-ipv6-traffic-guard) for error handling and
retained-journal cleanup. Rerunning into an existing state directory is rejected;
use `--release-only` with the original lifecycle journal to clean up its instances.

Require successful lifecycle completion and a passing guard report. Check both
capture results for zero prohibited IPv4, ARP, malformed frames and dropped
packets. Firmware DHCPv4 client probes are counted separately and allowed; DHCPv4
replies are failures. A report from an interrupted run is not acceptance evidence.
Keep source revisions, image IDs, OS digests, chart values, cluster UID, journals
and reports together. Generated private SSH keys and BMC credentials must not be
published with shareable evidence.

After both targets return to Ready with live Scout, use the
[documented stop/discard procedure](README.md#stop-restart-and-discard-the-bmc-lab).
Wait for both target pods to disappear before reusing the shared huge-page pool;
Helm release removal may precede completion of background pod deletion. Preserve
original stacks and disks that are outside the dedicated acceptance cluster.
