# Native BMC target lab

This local-development chart runs one native KVM guest with a `bmc-mock` Redfish
endpoint. A shared image contains QEMU, UEFI, libvirt and the BMC. It supports Linux
AMD64 and ARM64; the guest must match the runtime architecture. On macOS, run this
workflow inside a Linux VM with nested KVM access.

This stage of [the Tilt migration](https://github.com/dsx-ai-factory/infra-controller/issues/8124)
provides guest boot, BMC power control and retained target state. It does not
install NICo, DHCP, a boot server, Scout or an operating system. It creates an
isolated IPv4 guest bridge by default. Optional provisioning enables a DHCPv4
relay and forwarding to existing cluster services. Optional IPv6 bridge routing
supports explicitly addressed guests; DHCPv6 and router advertisements are outside
this chart’s implemented boundary. See the [provisioning and guest checks](../../dev/deployment/lab/README.md). Ordinary Tilt
machine-a-tron development remains independent of this chart.

## Prepare an isolated runtime

Run the [host preflight](../../dev/deployment/lab/README.md) before allocating
resources. The default guest has 2 vCPUs, 4 GiB RAM and a sparse 24 GiB disk. Budget
additional memory and disk for libvirt, images, saved guest memory and kind.
The chart requests 2 CPUs and derives memory requests from guest size: guest RAM
plus 1 GiB ordinary memory, or 1 GiB ordinary memory plus guest RAM in huge pages
when enabled. Explicit ordinary resources override these defaults; a memory
limit alone lets Kubernetes default the request to that limit. Kubernetes
requests do not guarantee free disk capacity. When upgrading a release created
with the fixed 5 GiB request, check available node capacity or set an explicit
request: larger guests and limit-only settings can increase the replacement
pod’s request and leave it Pending after the old pod stops.

Run from the repository root with Docker, kind, kubectl, Helm and Python 3.9 or
newer on PATH. The Docker daemon and kind node must expose native `/dev/kvm`.
The chart runs a privileged pod with host networking, so use an isolated local
kind cluster. In kind, host networking means the node container's network
namespace, not the outer host's namespace.

```bash
bash dev/deployment/setup-kind.sh --name nico-target
export KUBECONFIG="$HOME/.kube/kind-nico-target.yaml"
kubectl create namespace bmc-lab
kubectl -n bmc-lab apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: target-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 50Gi
YAML
```

The existing claim must support POSIX locks and remain on the selected node.
The chart never creates or deletes it. Kind's default local-path storage survives
release removal, but **deleting the cluster deletes that storage**.

Create a values file outside the repository. Choose an unused IPv4 Service
address in the cluster's Service CIDR and a guest subnet that overlaps no route
in the node. The example assumes the default kind Service CIDR.

```bash
cluster_uid=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')
architecture=$(kubectl get node nico-target-control-plane -o jsonpath='{.status.nodeInfo.architecture}')
cat > /tmp/nico-target.yaml <<YAML
architecture: $architecture
clusterUID: $cluster_uid
nodeName: nico-target-control-plane
storage:
  existingClaim: target-data
service:
  clusterIP: 10.96.40.10
network:
  bridgeAddress: 10.84.40.1/24
YAML
```

## Optional IPv6 guest routing

Set `network.bridgeAddressV6` to a ULA (`fc00::/7`) or global-unicast
(`2000::/3`) address with a `/64` prefix and a nonzero host part, for example
`fd42:8124:40::1/64`. The default empty string omits IPv6 from the runtime
configuration and preserves existing retained inventories. Each target needs a
separate non-overlapping prefix. Runtime validation rejects an invalid address
or a prefix overlapping another interface's route before claiming fresh storage.
IPv6 addresses are normalized before comparison with retained inventory.
Adding, removing or changing the IPv6 prefix of an existing target is rejected;
use its original configuration or explicitly release and destroy it before
creating a new target. Before reusing a release identity (namespace and name) with new storage,
[verify its network cleanup](#verify-target-network-cleanup). After an interrupted
shutdown, reinstall with the original values and retained storage, then stop
gracefully and verify cleanup as described there.
If that storage has already been lost, recreate the disposable cluster; deleting
only the PVC does not clean node networking and is not a supported reset.

The kind node must already have IPv6 enabled for `all`, `default` and an existing
owned bridge, with `net.ipv6.conf.all.forwarding=1`. For a new disposable node
created by the preceding example:

```bash
docker exec nico-target-control-plane sysctl -w net.ipv6.conf.all.forwarding=1
```

This changes only that node's network namespace; it does not configure the outer
host. Recheck the prerequisites after restarting or replacing the node, because
a recreated namespace loses runtime sysctl changes. Startup fails if these prerequisites are
missing. The target never changes shared forwarding policy. Keep IPv4 bridge,
BMC Service and provisioning settings: this option adds guest IPv6 routing,
not an IPv6 Kubernetes or provisioning deployment.

The target assigns the IPv6 gateway to its owned bridge, installs tagged IPv6
forwarding rules and a masquerade exemption for traffic entering that subnet
to preserve peer source addresses, and removes only its own rules on shutdown. It does not add
NAT66, a default route, router advertisements, DHCPv6, DNS or upstream routes.
Guests need explicit addresses and routes, or an independently configured
addressing service. Two target bridges on the same node can route directly;
external IPv6 requires separately managed upstream and return routes.
The same trust boundary as IPv4 applies: this is a privileged flat lab, not
network isolation between tenants. See the
[IPv6 guest procedure](../../dev/deployment/lab/README.md#two-instance-ipv6-routing).

## Build and deploy

The standalone Tilt entrypoint builds through the same Make target and installs
this chart. It requires a `kind-` context and matching Docker/guest architecture.
`--values` is required; `--namespace` defaults to `bmc-lab` and `--release` to
`target-one`. The namespace and PVC must already exist.

```bash
tilt ci -f dev/deployment/lab/Tiltfile -- \
  --values /tmp/nico-target.yaml --namespace bmc-lab --release target-one
```

Use `tilt up` with the same arguments for interactive rebuilds. Alternatively,
use Make and Helm directly:

```bash
make images-dev DEV_TARGET=bmc-target DEV_IMAGE=nico-bmc-target:local
kind load docker-image nico-bmc-target:local --name nico-target
helm upgrade --install target-one helm/nico-bmc-target \
  --namespace bmc-lab --values /tmp/nico-target.yaml \
  --set image.tag=local --wait --timeout 5m
```

A Ready pod contains a defined guest, initially powered off. Its HTTPS Redfish
Service listens on port 443. `service.targetPort` selects the listener in the
node namespace. TLS uses the repository's development fixture certificate.
The `generic_ami` fixture starts with user `root` and password `factory_password`;
rotate it through `/redfish/v1/AccountService/Accounts/2` before reading Systems.
The service is for isolated development only.

## Configuration and retention

| Value | Default | Contract |
| --- | --- | --- |
| `architecture` | `amd64` | `amd64` or `arm64`; must match node and native runtime. |
| `nodeName` | Empty, required | Exact existing Kubernetes node name; retained with the target. |
| `clusterUID` | Empty, required | UID of `kube-system`; checked on Helm install/upgrade and retained. Offline templating cannot query that UID. |
| `storage.existingClaim` | Empty, required | Existing PVC in the release namespace, with POSIX locks. |
| `image.repository`, `image.tag` | `nico-bmc-target`, `latest` | Locally built image. Tilt supplies its built repository and tag. |
| `image.pullPolicy` | `IfNotPresent` | Kubernetes pull policy; no registry upload is required. |
| `target.instanceIndex` | `1` | Integer 1–255, unique within the site; determines host/BMC MAC and serial. |
| `target.memoryMiB` | `4096` | Integer 1024–65536; guest RAM in MiB. Must be even with huge pages. |
| `target.hugepages` | `false` | Explicit 2 MiB HugeTLB backing with immediate allocation; requires a reserved host pool. Immutable with retained target data. |
| `target.cpus` | `2` | Integer 1–32; guest vCPUs. |
| `target.diskGiB` | `24` | Integer 8–1024; initial sparse guest disk size in GiB. |
| `network.bridgeAddress` | `10.84.40.1/24` | Usable IPv4 gateway with prefix /30 or shorter, without overlap with other node routes. |
| `network.bridgeAddressV6` | Empty, optional | ULA or global-unicast /64 gateway; see [IPv6 prerequisites and retention](#optional-ipv6-guest-routing). |
| `network.provisioning.enabled` | `false` | Enable IPv4 relay and forwarding in a single-node kind cluster using kube-proxy iptables mode. |
| `network.provisioning.dhcpServer` | Empty | Required when enabled: existing unicast IPv4 DHCP Service address outside the guest subnet. |
| `network.provisioning.podCIDR` | Empty | Required when enabled: selected node's IPv4 pod CIDR, separate from the guest subnet and not `/0`. Checked against live Nodes by Helm; offline templates cannot check it. |
| `network.provisioning.uplink` | `eth0` | Existing up node interface, separate from the guest bridge; 1–15 letters, digits, underscores, periods, colons or hyphens. |
| `service.clusterIP` | Empty, required | Unused IPv4 address in the cluster's Service CIDR. |
| `service.targetPort` | `2443` | Integer 1024–65535; must be unused on the selected node. Service port stays 443. |
| `resources` | Requests: 2 CPUs; ordinary RAM derived from guest sizing | Default ordinary RAM is guest RAM + 1024 MiB, or 1024 MiB with huge pages. Explicit ordinary requests/limits override defaults. Huge-page requests and limits are derived from target sizing; manual `hugepages-*` resource entries are rejected. |

Cluster UID, release namespace/name, node, architecture, guest identity/sizing,
bridge and BMC address/port are immutable for a retained target. A mismatch fails
startup rather than adopting or resizing the disk. Reinstall using the original
values and release identity. A new configuration requires a new empty PVC.

The PVC holds the disk, UEFI variables, libvirt definition/saved state, BMC
credentials and logs. A filesystem lock prevents concurrent target writers.
Recreate deployment strategy stops the old pod before starting its replacement.
During graceful shutdown the runtime stops BMC writes, saves a running guest,
then stops libvirt. Saving is best-effort and bounded to 150 seconds; a timeout
or storage failure can lose guest memory even during graceful shutdown. The
boot and retention checks use 4 GiB guests. Larger guests need enough storage
throughput to save within that deadline. The replacement leaves the guest off; Redfish `On` restores
saved state. Autostart is rejected. Forced termination can lose guest memory and
unflushed writes; retained disks are not a backup. A missing initialized disk,
firmware file, domain or BMC state fails startup and requires restoring the data.

## Huge-page lifecycle

Before enabling `target.hugepages`, follow the
[host pool setup and preflight](../../dev/deployment/lab/README.md#explicit-huge-page-backing).
No guest-address family or DPU setting is involved. The same values work on native
ARM64 and AMD64. Kubernetes schedules the separate `hugepages-2Mi` resource;
ordinary memory requests cover the remaining process allocations.

Omitted or false `target.hugepages` preserves the older inventory/configuration
shape. Enabling it changes the retained configuration and must use fresh target
storage. Do not toggle it on a retained disk or managed-save image. The runtime
checks the inactive libvirt domain and any managed-save XML against the retained
identity, guest RAM and backing policy before starting the BMC writer.

Insufficient unreserved pool capacity fails startup before fresh storage is
claimed. For a retained target, the disks, inventory and saved RAM remain intact:
restore the original backing configuration and sufficient pool capacity, then
restart the pod. The target stays off until BMC On restores its saved state.
The preflight is a snapshot, so another consumer can exhaust the pool later;
QEMU then fails allocation without falling back to small pages. Inspect the
BMC/libvirt logs for that power-operation failure. A ready target pod does not
promise that a later guest power-on will succeed.

## Verify target network cleanup

Release the target's instance through Core and wait for Ready with live Scout
before removing its runtime. Capture its nonsecret inventory while the pod is
available, then uninstall gracefully and check the node. This example removes
`target-one` from `bmc-lab`, retaining its PVC:

```bash
set -euo pipefail
inventory=$(kubectl -n bmc-lab exec deployment/target-one -- cat /data/inventory.json)
node=$(printf '%s' "$inventory" | jq -er .nodeName)
bridge=$(printf '%s' "$inventory" | jq -er .bridge)
owner="nico-target:$(printf '%s' "$inventory" | jq -er .uuid)"
helm uninstall target-one --namespace bmc-lab --wait --timeout 5m
kubectl -n bmc-lab wait --for=delete pod \
  -l app.kubernetes.io/name=nico-bmc-target,app.kubernetes.io/instance=target-one --timeout=5m
docker exec "$node" ip -json link show | \
  jq -e --arg name "$bridge" 'all(.[]; .ifname != $name)'
docker exec "$node" sh -ec 'iptables-save; ip6tables-save' | \
  awk -v tag="$owner" 'index($0, tag) { found=1 } END { exit found }'
```

The link check prints `true`; the rule check is silent. Either exits nonzero
if owned networking remains. Do not discard the PVC or reuse the identity after
a failed check. The runtime intentionally retains a bridge with attached TAPs
when guest save fails. Restore the original runtime and storage to recover,
then stop gracefully and repeat the checks. If storage was lost or recovery is
not possible, release any remaining instances and recreate the disposable
cluster to remove its node network state. Never delete a foreign bridge or flush
shared firewall tables to reset one target.

## Connect existing provisioning services

Provisioning is opt-in and does not install or configure a DHCP server. The
server must serve the guest subnet selected by the relay's gateway address and
provide the intended boot options. Enable it only in an isolated single-node
kind cluster with kube-proxy in iptables mode using the image's `nf_tables`
backend, and IPv4 forwarding already turned on. Nodes using the legacy
iptables backend are outside this chart's supported provisioning topology. The runtime checks those prerequisites without changing global
sysctls. It does not support a multi-node topology or an IPv6 provisioning path.

With an existing DHCP Service at `10.96.40.20`, enable the relay on an installed
target with:

```bash
pod_cidr=$(kubectl get node nico-target-control-plane -o jsonpath='{.spec.podCIDR}')
helm upgrade target-one helm/nico-bmc-target --namespace bmc-lab --reuse-values \
  --set network.provisioning.enabled=true \
  --set network.provisioning.dhcpServer=10.96.40.20 \
  --set network.provisioning.podCIDR="$pod_cidr" --wait --timeout 5m
```

For Tilt, put these values in the same target values file used at deployment.
The relay forwards guest broadcasts to that Service. Forwarding rules allow
outbound guest traffic and established replies. Traffic to the node's pod CIDR
keeps its source address, including after Service DNAT; this is needed by
services that identify the provisioning host from its source IP. Traffic leaving
through the uplink is masqueraded to the node address. This enables access
through existing upstream routing; it does not establish Internet reachability.
Packets destined for this target's subnet and exiting its bridge are exempt from
NAT before kindnet's catch-all masquerade rule, preserving peer guest sources.
This exemption does not add inbound forwarding permission or modify peer rules.
See [two-instance checks](../../dev/deployment/lab/README.md#two-instance-ipv4-connectivity)
for the supported same-node topology.
This isolated lab network is not a tenant security boundary.

The relay accepts requests only from its guest bridge and replies from the
uplink and kindnet pod-facing interfaces. It refreshes those upstream listeners
when pods change; in-flight DHCP exchanges may retry during that brief restart.
This uses the directional interfaces supported by
[ISC relay](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcrelay).

Rules carry the target UUID and are removed on graceful shutdown or reconciled
on the next start of the same target identity and retained guest subnet.
An owned rule with a different subnet or an unrecognized shape fails startup
instead of being deleted. Cleanup failures are logged;
bridge cleanup is still attempted. After uninstall or forceful termination with
no restart, leftover rules can remain. A different release identity does not
clean them up. Deleting the disposable kind cluster removes its node network
state when retiring such a lab. Even disabled mode needs working image-side
netfilter tools to remove any stale rules from an earlier enabled run. Other targets' rules and kube-proxy chains are not modified.
A stopped relay makes the target runtime fail and removes readiness. Provisioning
values are operational settings, separate from retained guest identity: changing
or disabling them restarts the pod but retains the disk and BMC credentials.
Each target needs a distinct guest subnet, target index and BMC port/address.

## Exercise boot and retained state

The smoke test below is **destructive and only for a fresh isolated test disk**:
it writes a disk marker, rotates the fixture password and boots the guest. It
compares Redfish serial and network-boot MAC with the libvirt guest identity,
then captures a real DHCPv4 request on the guest bridge with that MAC.
By default it checks a request only. With a configured DHCP server and relay,
add `--lease-address 10.84.40.100` to require a DHCP ACK for that IPv4 address
and guest MAC instead. The default is empty; an invalid IPv4 argument fails
validation. An ACK observed on the guest bridge does not establish Scout
readiness or operating-system installation.

```bash
python3 dev/deployment/lab/test-target.py prepare \
  --context kind-nico-target --credentials /tmp/nico-target-test.json
kubectl -n bmc-lab rollout restart deployment/target-one
kubectl -n bmc-lab rollout status deployment/target-one --timeout=5m
python3 dev/deployment/lab/test-target.py retained \
  --context kind-nico-target --credentials /tmp/nico-target-test.json
```

Both phases require the guest to start powered off and leave it running on
success. `prepare` creates the credentials/identity record exclusively with mode
0600; it refuses to overwrite an existing file. `retained` reuses it, verifies
credentials and disk/firmware identity plus the disk marker, restores the guest,
and cold-restarts it through Redfish to observe DHCP again. Failures leave the
record and runtime available for diagnosis; power may have changed. Keep the
record private and outside Git. Exit status is 0 on success, 1 on validation or
command failure, and 2 on invalid arguments. `--namespace` and `--release` have
the same defaults as Tilt; `--context` and `--credentials` are required.

Remove the release while retaining the claim with:

```bash
helm uninstall target-one --namespace bmc-lab --cascade foreground --wait --timeout 5m
```

Only when the isolated test data is no longer needed, delete its claim and the
private smoke-test record:

```bash
kubectl -n bmc-lab delete pvc target-data
rm /tmp/nico-target-test.json
```
