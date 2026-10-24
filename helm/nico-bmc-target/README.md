# Native BMC target lab

This local-development chart runs one native KVM guest with a `bmc-mock` Redfish
endpoint. A shared image contains QEMU, UEFI, libvirt and the BMC. It supports Linux
AMD64 and ARM64; the guest must match the runtime architecture. On macOS, run this
workflow inside a Linux VM with nested KVM access.

This stage of [the Tilt migration](https://github.com/dsx-ai-factory/infra-controller/issues/8124)
provides guest boot, BMC power control and retained target state. It does not
install NICo, DHCP, a boot server, Scout or an operating system. It creates an
isolated IPv4 guest bridge without forwarding or NAT. NICo discovery and tenant
IPv6 validation require the later provisioning integration. Ordinary Tilt
machine-a-tron development remains independent of this chart.

## Prepare an isolated runtime

Run the [host preflight](../../dev/deployment/lab/README.md) before allocating
resources. The default guest has 2 vCPUs, 4 GiB RAM and a sparse 24 GiB disk. Budget
additional memory and disk for libvirt, images, saved guest memory and kind.
The chart requests 2 CPUs and 5 GiB memory for its pod; adjust `resources` when
changing guest size. Kubernetes requests do not guarantee free disk capacity.

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
| `target.memoryMiB` | `4096` | Integer 1024–65536; guest RAM in MiB. |
| `target.cpus` | `2` | Integer 1–32; guest vCPUs. |
| `target.diskGiB` | `24` | Integer 8–1024; initial sparse guest disk size in GiB. |
| `network.bridgeAddress` | `10.84.40.1/24` | Usable IPv4 gateway with prefix /30 or shorter, without overlap with other node routes. |
| `service.clusterIP` | Empty, required | Unused IPv4 address in the cluster's Service CIDR. |
| `service.targetPort` | `2443` | Integer 1024–65535; must be unused on the selected node. Service port stays 443. |
| `resources` | Requests: 2 CPUs, 5 GiB | Kubernetes container resources, independent of guest sizing. No limits by default. |

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

## Exercise boot and retained state

The smoke test below is **destructive and only for a fresh isolated test disk**:
it writes a disk marker, rotates the fixture password and boots the guest. It
compares Redfish serial and network-boot MAC with the libvirt guest identity,
then captures a real DHCPv4 request on the guest bridge with that MAC.
It does not provide a DHCP lease, install an OS or establish Scout readiness.

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
