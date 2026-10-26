# Native guest lab prerequisites

The optional BMC-backed guest lab requires a Linux runtime with native KVM.
Run the preflight on that runtime before building images or allocating targets.
Ordinary Tilt development with machine-a-tron does not require KVM or this check.
On macOS, run it inside the Linux VM that will host the guests; running it on the
macOS client reports an unsupported platform.

## Host budget

Python 3.9 or newer and a Linux AMD64 or ARM64 host are required. Run as the identity that
will access KVM. The tool does not change device permissions or invoke `sudo`.
The ordinary memory, CPU and disk budget flags below are required positive
integers with no defaults:

```bash
python3 dev/deployment/lab/preflight.py host \
  --storage-path /var/lib/docker \
  --memory-mib 6144 --cpus 2 --disk-gib 32
```

This example checks an **additional** 6 GiB memory and 32 GiB disk budget, and an
affinity mask containing at least two CPUs. Choose the budget for the intended
number and size of guests, runtime overhead and any services still to be started.
These example numbers are not a sizing guarantee for a complete NICo deployment.
For a nonstandard Docker data root or separate guest disk, replace
`--storage-path` with an existing directory on the actual data filesystem.
The command never creates that directory.

Memory is Linux `MemAvailable`, excluding swap. CPU capacity is the number of
CPUs in the invoking process's affinity mask, **not** the number of idle CPUs.
Disk capacity uses filesystem blocks available to unprivileged users, rounded
down to GiB; memory is rounded down to MiB. Equality with a requested budget
passes. These are host snapshots, not resource reservations. They do not account
for container memory/CPU quotas, filesystem quotas, future concurrent allocation
or thin-provisioned backing storage. Run `host` on the Linux host; use `kvm`
inside a container or pod and size its resource limits separately. A disk-space
pass does not establish that the eventual runtime can write to that path.

## Explicit huge-page backing

Set `target.hugepages: true` in the native BMC target values to back guest RAM
with immediately allocated 2 MiB HugeTLB pages. This is opt-in on both native
architectures. It avoids the opportunistic small-page fallback that can make
nested ARM64 KVM mapping and reset operations expensive. It does not change the
host's transparent-huge-page or compaction policy.

Reserve the pool on the Linux runtime **before creating the kind cluster** so
kubelet discovers the capacity. Reserve early in the lab's lifetime: fragmented
memory can prevent a full reservation even when ordinary available RAM is large.
The pool is shared across the host, including other clusters. Coordinate its
budget with their owners; do not overwrite an existing reservation.

For a previously empty pool, this reserves 8 GiB for two 4 GiB guests. Run the
write as root on the Linux runtime:

```bash
pool=/sys/kernel/mm/hugepages/hugepages-2048kB
test "$(cat "$pool/nr_hugepages")" = 0 &&
  printf '4096\n' | sudo tee "$pool/nr_hugepages" &&
  test "$(cat "$pool/nr_hugepages")" = 4096
```

The last check is required: the kernel can allocate fewer pages than requested.
An incomplete reservation remains visible in the pool and requires an explicit
operator decision to retry or release it; target startup does not compact memory
or silently use ordinary pages. This runtime reservation does not survive a host
reboot. An existing kind node may have stale capacity; use a fresh lab cluster
rather than assuming a late reservation is schedulable.

Check unreserved huge-page capacity separately from ordinary RAM:

```bash
python3 dev/deployment/lab/preflight.py host \
  --storage-path /var/lib/docker --memory-mib 6144 --hugepages-mib 8192 \
  --cpus 4 --disk-gib 60
```

`--hugepages-mib` is optional, a positive integer with no default. When supplied,
`requested.hugepages_mib` and `available.hugepages_mib` are included in the JSON
capacity report. Availability is twice the nonnegative difference between the
2 MiB pool's free and reserved page counts. Ordinary `--memory-mib` covers only
non-HugeTLB allocations: QEMU/BMC overhead and any additional stack services.
The check does not reserve pages or validate cgroup limits. Pool reads fail
closed; when omitted, the existing host-budget contract is unchanged.

The chart derives equal `hugepages-2Mi` requests and limits from guest RAM and
requests 1 GiB of ordinary memory by default for process overhead. Explicit
ordinary `resources` settings still apply; size them for the actual workload.
See the [chart contract](../../../helm/nico-bmc-target/README.md) for retained
state and insufficient-pool recovery.

After deleting this lab's targets and cluster, a pool created from zero above
can be released as follows. Stop if any other workload has claimed the pool:

```bash
pool=/sys/kernel/mm/hugepages/hugepages-2048kB
test "$(cat "$pool/free_hugepages")" = "$(cat "$pool/nr_hugepages")" &&
  test "$(cat "$pool/resv_hugepages")" = 0 &&
  printf '0\n' | sudo tee "$pool/nr_hugepages" &&
  test "$(cat "$pool/nr_hugepages")" = 0
```

Target or Helm teardown never shrinks the shared host pool. For kernel allocation
details, see [Linux HugeTLB administration](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html).

## KVM execution boundary

Probe again in the container or pod that will execute QEMU, with its intended
identity and device access. The host's result does not establish pod access:

```bash
python3 dev/deployment/lab/preflight.py kvm
```

Both commands open `/dev/kvm` read-write, verify KVM API version 12, create an
empty VM and close its descriptor and the device. No guest CPU, memory, disk or
network is created. ARM64 probes select an IPA width supported by the host when
the kernel exposes that capability. This follows the
[Linux KVM API](https://docs.kernel.org/virt/kvm/api.html).

The `kvm` command checks only KVM access; it does not check the host budget.
For kind, a `hostNetwork` pod runs in the kind node's network namespace, so its
result still does not prove guest networking to the outer host or NICo services.
This prerequisite does not establish QEMU/UEFI boot, Scout discovery, DHCP
identity, BMC control, retained disks or credential recovery. Use the
[native BMC target chart](../../../helm/nico-bmc-target/README.md) for
guest boot, BMC control and retained-state validation. Scout discovery and tenant
networking remain provisioning work tracked in
[the Tilt migration issue](https://github.com/dsx-ai-factory/infra-controller/issues/8124).

Both commands print one JSON object with `scope`, `architecture`, `ready` and
`errors`. A successful KVM probe also includes `kvm_api_version`. Host checks
include `storage_path`, `requested` and `available` together only after path
resolution and capacity reads succeed; none of those fields appears on a
probe/read failure. The first probe/read error stops further checks. A budget failure reports all
insufficient resources. Exit status is 0 for a pass and 1 for a probe, platform,
path or capacity failure. Invalid or missing arguments exit 2 with argparse usage
on stderr; `--help` exits 0 with help text instead of JSON. Neither command
reserves resources, changes Kubernetes state nor modifies persistent lab data.

## Scout boot artifacts

The native no-DPU lab uses real Scout userspace with Ubuntu's virtual kernel in
both the loader and root filesystem. This avoids GPU/DPU driver installation and
the ARM64 hardware profile's 64 KiB kernel requirement. The builder stages copies
of the hardware profiles; it does not modify them. These artifacts are for
isolated development: the inherited profiles include a known root password.
They are not production hardware images.

Run the commands below from the repository root on the native Linux runtime
with Docker, Git and Make. The builder container supplies Python and the mkosi
prerequisites. The Docker daemon must run on that same host, because the mkosi
build uses bind mounts. On macOS, run this step inside
the Linux VM. Building does not require KVM; boot validation does.

```bash
git submodule update --init pxe/mkosi
make images-dev DEV_TARGET=scout-package DEV_IMAGE=nico-scout-package:local
make images-dev DEV_TARGET=scout-builder DEV_IMAGE=nico-scout-builder:local
mkdir -p /tmp/nico-scout-artifacts
package_container=$(docker create nico-scout-package:local /not-started)
docker cp "$package_container:/scout.deb" /tmp/nico-scout-artifacts/scout.deb
docker rm "$package_container"
docker run --rm --privileged \
  --mount "type=bind,src=$PWD,dst=/src,readonly" \
  --mount type=bind,src=/tmp/nico-scout-artifacts,dst=/state \
  --workdir /src nico-scout-builder:local \
  python3 dev/deployment/lab/build-scout.py \
  --scout-package /state/scout.deb --output /state/boot
```

`scout-package` builds native debug binaries for Scout and its firmware
supervisor, then invokes the repository's existing package task. Its package
version is `0.0.0-dev`. The image contains `/scout.deb` and has no runnable
entrypoint. `scout-builder` contains build tools; it does not contain Scout or
fetch a package implicitly. Both use the shared `make images-dev` architecture
selection and local image naming controls.

`build-scout.py` requires `--scout-package` and `--output`, with no defaults.
It runs as root on native ARM64 or AMD64 Linux with the pinned mkosi submodule
initialized. The `.deb` must match the runtime architecture. Output must be new
or empty; an existing artifact directory is rejected. A failed build leaves its
workspace for diagnosis and requires a different empty output directory to retry.
No cleanup or cluster operation is performed by this helper.

Successful output contains `aarch64/scout.efi` and `aarch64/scout.squashfs`, or
`x86_64/scout.efi` and `x86_64/scout.squashfs`. `build/` retains the staged profiles,
mkosi output and downloaded package cache. `build-info.json` records the native
architecture, package version, requested virtual-kernel package and SHA-256
hashes of the input package and both payloads. The manifest is written only after
both build stages succeed. Distribution packages are resolved during each build;
these development builds are not promised to be byte-for-byte reproducible.
Output is root-owned when built in the container. `.dockerignore` excludes the
retained build workspace if the output directory is later used as an image context.

The root filesystem retains the real Scout service, network handoff helper,
module loader and current pre-start script. Its build checks both executables
and their dynamic-library dependencies. GPU package sources and hardware-driver
post-installation are omitted. No iPXE, OS imager, DHCP service, artifact web
server, site credential or expected-machine registration is produced here.
Those are separate boot/admission integration inputs tracked by
[the Tilt migration](https://github.com/dsx-ai-factory/infra-controller/issues/8124).
A successful build alone does not establish NICo discovery or instance readiness.

## Connect Scout to the Tilt stack

This path uses a fresh, dedicated single-node kind cluster on the native Linux
runtime. It supports ARM64 and AMD64 with the same IPv4 provisioning workflow.
It uses the default kind Service CIDR and the `nico-system` Core namespace.
It does not convert an existing MAT deployment or adopt its database/credentials.
Keep the target PVC, Core database and persistent Vault together for recovery.

Build iPXE after initializing its pinned submodule. The helper stages a copy,
uses NICo's embedded boot script and HTTP IPv6 source patch, and builds native
`snponly.efi`. Vendor-specific BlueField patches are unnecessary for virtio guests.
The builder image from the previous section supplies the build tools.

```bash
git submodule update --init pxe/ipxe/upstream
docker run --rm \
  --mount "type=bind,src=$PWD,dst=/src,readonly" \
  --mount type=bind,src=/tmp/nico-scout-artifacts,dst=/state \
  --workdir /src nico-scout-builder:local \
  python3 dev/deployment/lab/build-ipxe.py --output /state/ipxe
mkdir -p "/tmp/nico-boot-image/$(uname -m)"
cp "/tmp/nico-scout-artifacts/ipxe/$(uname -m)/ipxe.efi" \
  "/tmp/nico-boot-image/$(uname -m)/"
cp "/tmp/nico-scout-artifacts/boot/$(uname -m)/scout.efi" \
  "/tmp/nico-scout-artifacts/boot/$(uname -m)/scout.squashfs" \
  "/tmp/nico-boot-image/$(uname -m)/"
docker build -f dev/deployment/lab/Dockerfile.boot-artifacts \
  -t nico-boot-artifacts:local /tmp/nico-boot-image
```

`build-ipxe.py` requires `--output`, a new or empty directory, with no default.
It rejects unsupported host architectures and preserves failed builds for
inspection. Successful output contains `ARCH/ipxe.efi` and a `build/` workspace.
The artifact image requires the native architecture's nonempty `ipxe.efi`,
`scout.efi` and `scout.squashfs`; missing payloads fail the image build. It also
accepts the optional [disk imager](#native-disk-imager). It does
not publish the image to a registry. Build and load it on the cluster's native
Docker runtime.

The VM Scout loader's default rootfs URL points to
`http://nico-pxe.nico-system.svc.cluster.local/public/blobs/internal/ARCH/scout.squashfs`.
An explicit `newrootfs=` kernel argument still takes precedence. Hardware source
profiles retain their original fallback. Both VM stages route `cluster.local`
lookups to kind DNS at `10.96.0.10`, rather than multicast DNS. Rebuild Scout artifacts using the
preceding instructions to get this VM-specific default.

After creating the cluster using the [Tilt setup instructions](../tilt/README.md):

```bash
kind load docker-image nico-boot-artifacts:local --name nico-tilt
tilt up -f dev/deployment/tilt/Tiltfile -- \
  --bmc-lab --persistent-vault --boot-artifact-image nico-boot-artifacts:local
```

`--bmc-lab` defaults to false and requires both `--persistent-vault` and a
nonempty `--boot-artifact-image`. Supplying the image without BMC mode is an
error. The image uses `IfNotPresent`; load it on the cluster or use a pullable image.
Use a new tag for changed payloads to avoid reusing a cached image. BMC mode deploys
Core, BMC proxy, DHCP and PXE, with real Scout authentication and hardware polling.
It disables MAT, MAT's readiness check and the MAT-specific SSH console resource.
Tilt service readiness therefore does **not** establish guest admission or
instance readiness. The checks below establish Scout connectivity separately.
A mode marker in `kube-system` is claimed before Core starts and survives release
removal. It rejects mode changes even after a partial deployment. See the
[existing-cluster rules](../tilt/README.md#existing-local-clusters) for adoption
and rejection of unmarked sites. Removing the marker is not a supported migration
procedure.

PXE uses Service address `10.96.0.30`: firmware downloads iPXE on port 8080;
iPXE requests boot metadata on port 80. Both ports reach the same PXE server.
Kea at `10.96.0.20` allocates addresses through Core and advertises cluster DNS
at `10.96.0.10`. No NTP server is deployed by this profile.

Deploy a target using the [BMC target chart](../../../helm/nico-bmc-target/README.md),
with provisioning enabled, `network.provisioning.dhcpServer=10.96.0.20` and the
selected node's pod CIDR. The example below assumes target index 1, BMC Service
`10.96.40.10`, and guest bridge `10.84.40.1/24`. These must match the target values.
It reserves a separate underlay prefix containing that fixed BMC Service address;
Core does not allocate the BMC Service itself.

From the repository root, register the fresh target through the admin API:

```bash
export LOCAL_DEV_KUBE_CONTEXT=kind-nico-tilt
export FORGE_CLIENT_REQUEST_TIMEOUT_SECS=30
admin() { bash dev/deployment/core-admin.sh "$@"; }
domain=$(admin -f json domain show | jq -er '.domains[] | select(.name=="local.nico") | .id')
admin --cloud-unsafe-op 'Initialize local host provisioning' network-segment create \
  --name lab-host --segment-type host-inband --prefix 10.84.40.0/24 \
  --gateway 10.84.40.1 --reserve-first 10 --subdomain-id "$domain"
admin --cloud-unsafe-op 'Initialize local BMC Service addresses' network-segment create \
  --name lab-bmc --segment-type underlay --prefix 10.96.40.8/29
admin em add --bmc-mac-address 02:00:00:01:00:01 \
  --bmc-username root --bmc-password factory_password \
  --chassis-serial-number 020000010001 --bmc-ip-address 10.96.40.10 \
  --dpu-policy ignore --dpf-enabled false --meta-name native-target-one \
  --interfaces '[{"mac_address":"02:00:00:01:00:02","role":"host","primary":true,"network_segment_type":3}]'
admin -f json site-explorer get-report all
admin -f json machine show
```

These create commands are for a new site and target, not a reset procedure.
Do not repeat registration over an existing expected machine or overwrite its
rotated credentials. Core rotates the initial BMC password, performs normal
preingestion and powers on the guest. Firmware may try PXEv4 and PXEv6 before
HTTPv4; those initial failures do not by themselves establish a failed HTTP boot.

Once `machine show` reports the target's Core machine ID, verify the live stream:

```bash
admin scout-stream ping MACHINE_ID
```

Use the ID from the target whose metadata name is `native-target-one`.
A successful ping proves a live real-Scout response through Core. It does not
prove OS installation, instance assignment, release, or tenant IPv6 connectivity.
Continue with the [instance lifecycle checks](#allocate-and-recycle-an-instance);
tenant IPv6 remains a separate milestone in
[issue 8124](https://github.com/dsx-ai-factory/infra-controller/issues/8124).

### Stop, restart and discard the BMC lab

Stop the Tilt process with Ctrl-C to stop watching while retaining the running
stack, Core database, Vault and target state. Restart using the same context,
BMC flags, image reference and target configuration. Do **not** use `tilt down`
for a resumable BMC lab: uninstalling PostgreSQL can delete its PVC while Vault
and target PVCs survive, leaving an inconsistent site. The generic Tilt teardown
instructions do not apply to retained BMC labs.

For a full discard, first stop both Tilt processes (stack and target), then
remove the dedicated cluster. With the default kind/local-path storage used by
this workflow, this removes its database, Vault, target PVC and mode marker
together. This destroys the disposable guest and all site data; it is not a
backup or restore operation.

```bash
kind delete cluster --name nico-tilt
```

Never reuse target disks or Vault credentials from a discarded site with a new
empty database. This slice provides stop/restart and full discard, not partial
site recovery or migration between clusters.

## Native disk imager

Disk-installed instances need an additional `qcow-imager.efi` in the boot artifact
image. Scout admission alone does not need it. Build on the same native Linux
runtime as the preceding artifacts, using the initialized mkosi submodule and
Scout builder image:

```bash
docker run --rm --privileged \
  --mount "type=bind,src=$PWD,dst=/src,readonly" \
  --mount type=bind,src=/tmp/nico-scout-artifacts,dst=/state \
  --workdir /src nico-scout-builder:local \
  python3 dev/deployment/lab/build-imager.py --output /state/imager
cp "/tmp/nico-scout-artifacts/imager/$(uname -m)/qcow-imager.efi" \
  "/tmp/nico-boot-image/$(uname -m)/"
docker build -f dev/deployment/lab/Dockerfile.boot-artifacts \
  -t nico-boot-artifacts:imager /tmp/nico-boot-image
kind load docker-image nico-boot-artifacts:imager --name nico-tilt
```

Restart Tilt with `--boot-artifact-image nico-boot-artifacts:imager` and the same
BMC mode flags. Use a new image tag after rebuilding any payload.

`build-imager.py` requires `--output` with no default. It accepts a new or empty
directory and requires root on native Linux ARM64 or AMD64. It copies the
production `qcow-imager-aarch64` or `qcow-imager` profile and the real
`disk_imaging.sh` without changing their source. Both profiles already use the
virtual kernel. The staged profile adds the same kind DNS routing as Scout.
No OS image, tenant credentials or cloud-init data is embedded in this artifact.

Successful output is `ARCH/qcow-imager.efi`, with staged sources and caches in
`build/`. `build-info.json` records the architecture, selected profile, payload
SHA-256 and installer SHA-256. Failure leaves the build workspace for diagnosis;
retry with a new or empty output directory. The helper never cleans an existing
directory or changes a cluster. Distribution package resolution happens during
the build, so these artifacts are not promised to be byte-for-byte reproducible.
The artifact image includes the imager when present and rejects an empty one;
it still requires all three Scout/iPXE payloads.

## SSH to an installed guest

The guest address is on the target pod's private bridge. Use the helper below
from a client with Python 3.9+, OpenSSH and `kubectl` access to the selected kind
cluster. It relays TCP port 22 through `kubectl exec`; it does not install an SSH
client or copy a private key into the target pod.

```bash
python3 dev/deployment/lab/ssh-guest.py \
  --context kind-nico-tilt --address 10.84.40.10 \
  --identity /tmp/nico-instance-key --known-hosts /tmp/nico-instance-known-hosts \
  -- 'uname -m; findmnt -no SOURCE /; cloud-init status'
```

Supply a private key whose public key was installed through the instance's
cloud-init data. The helper does not create keys, allocate an instance or grant
access. Required flags have no defaults: `--context`, `--address` (IPv4),
`--identity` (existing private key file) and `--known-hosts` (a writable file or
new path in an existing directory). Both resolved file paths must exclude
whitespace, quotes, `%`, `$` and backslashes, which OpenSSH can interpret as
configuration syntax or expansions. The whitespace, quote and backslash restrictions are deliberately conservative
for identity paths; the token-expansion restrictions apply to both arguments.
The defaults are `--namespace bmc-lab`,
`--release target-one` and `--user lab`. Repeated options use the last value.
Put an optional remote command after `--`; omitting it opens the guest shell.
OpenSSH's remote-shell command semantics apply.

The helper rejects non-kind contexts, deployments without the native target
label, and addresses outside the target bridge subnet or equal to its gateway,
network or broadcast address. It does not discover the guest's lease; use the
address reported by Core. The helper opens the known-hosts file without truncation before SSH, creating
it with mode 0600 when absent and rejecting nonregular or unwritable files.
It ignores SSH configuration files and the global known-hosts file to keep
the selected identity, proxy and host-key file authoritative. First use accepts
and saves the guest host key;
subsequent key changes fail. Use a separate known-hosts file for each newly
provisioned instance. SSH uses batch authentication, only the selected identity,
and a 15-second connection timeout; an established interactive session has no
overall timeout. The helper returns the SSH exit status and reports validation
or Kubernetes errors before connecting.

## Allocate and recycle an instance

Use a fresh lab target that has completed the preceding Scout admission checks.
Run Core commands through `dev/deployment/core-admin.sh` with
`LOCAL_DEV_KUBE_CONTEXT` set to the lab's explicit kind context. The Core admin
path bypasses the REST database; use it only in this isolated lab. The
`--cloud-unsafe-op` argument is required for VPC and instance mutations.

The exercised image boundary is a native Ubuntu 24.04 cloud image with root
filesystem label `cloudimg-rootfs`, the installer default. Other images need
explicit filesystem identification (`--rootfs-label` or `--rootfs-id`) and
separate validation: without a matching root filesystem, the installer cannot
seed cloud-init or install the SSH key.

Register the image with `os-image create`: supply a unique UUID, the
image download URL, its SHA-256 as 64 hexadecimal characters, the tenant
organization and `--boot-disk /dev/vda`. The virtual target's whole disk is
replaced during installation. Match the image architecture to the target and
verify the digest against the image publisher. Keep the chosen URL and digest
in the validation record. The imager fetches the OS at provisioning time;
container build success does not prove download or disk installation.

Create a `flat` VPC for that same tenant using
`vpc create --name NAME --org-id ORG --virtualization-type flat` and save the
returned VPC ID. Flat instances use the admitted host's `HostInband` segment.
They do not require a DPU or tenant overlay. Put an OS configuration in a private
JSON file, substituting the registered image UUID and your public SSH key:

```json
{
  "phone_home_enabled": false,
  "run_provisioning_instructions_on_every_boot": false,
  "user_data": "#cloud-config\nusers:\n  - name: lab\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    ssh_authorized_keys:\n      - REPLACE_WITH_PUBLIC_KEY\nwrite_files:\n  - path: /etc/nico-lab-generation\n    content: generation-1\n",
  "variant": {"OsImageId": {"value": "REPLACE_WITH_IMAGE_UUID"}}
}
```

With `machine_id`, `vpc_id` and `os_config` set to the selected host ID, VPC ID
and JSON file path:

```bash
bash dev/deployment/core-admin.sh --cloud-unsafe-op 'Local instance validation' \
  instance allocate --prefix-name native-lifecycle \
  --machine-id "$machine_id" --flat-vpc-id "$vpc_id" --os "$(cat "$os_config")"
bash dev/deployment/core-admin.sh -f json instance show "$machine_id"
```

Save the created instance ID. Check the API response even if allocation exits
zero: the nontransactional CLI can log an allocation failure without a nonzero
exit status. API tenant `Ready` does not prove the OS has booted. Follow the
selected target's `/data/logs/console.log` through imager completion and the
installed OS boot before using the SSH helper. Scout and the installed OS have
different SSH host keys; do not enroll Scout's key as the tenant's key.

Validate the real disk root with `findmnt -no SOURCE /`, native architecture
with `uname -m`, completed cloud-init with `sudo cloud-init status --wait`, and
the generation marker. Then create and verify a test file on the installed root disk, run `sudo sync`
and record `/proc/sys/kernel/random/boot_id`. Flush the write before testing
persistence: the Core reboot path can force a reset without an OS shutdown.
Reboot through Core:

```bash
bash dev/deployment/core-admin.sh --cloud-unsafe-op 'Local instance validation' \
  instance reboot --instance "$instance_id"
```

After SSH returns, require a different boot ID and the retained test file.
Release through Core and wait for the old instance to disappear and the host to
return to `Ready` with a successful Scout ping:

```bash
bash dev/deployment/core-admin.sh --cloud-unsafe-op 'Local instance validation' \
  instance release --instance "$instance_id"
bash dev/deployment/core-admin.sh -f json instance show "$machine_id"
bash dev/deployment/core-admin.sh -f json machine show "$machine_id"
bash dev/deployment/core-admin.sh scout-stream ping "$machine_id"
```

Reallocate with `generation-2` in the cloud-init data and a new known-hosts file.
Require a new instance ID, the new generation marker and absence of the old test
file. Finally release the replacement instance before removing the disposable
cluster and target PVC. Keep the database, Vault and target storage together
until that lifecycle has finished.

This proves installation and replacement of a virtual disk image, not hardware
secure erase. Scout's hardware cleanup discovers NVMe and `/dev/sd*` devices;
the target's `/dev/vda` is outside those paths. Do not infer tenant data erasure
at release from a successful reprovisioning test. Virtio inventory, hardware
sanitize coverage and tenant IPv6 remain separate acceptance boundaries in
[the migration issue](https://github.com/dsx-ai-factory/infra-controller/issues/8124).

## Two-instance IPv4 connectivity

Use two targets on the same node of the dedicated single-node kind cluster.
The target pods use `hostNetwork`: their separate bridges share that node's
network namespace. Connected routes and the existing provisioning forwarding
rules provide the peer path. Each target exempts packets destined for its own
subnet and exiting through its bridge from NAT before kindnet's catch-all
masquerade rule. This preserves guest source addresses on the peer path.
This topology is an isolated flat lab, not a DPU overlay or tenant-isolation test.

Create a separate PVC and values file for each target using the
[BMC target chart procedure](../../../helm/nico-bmc-target/README.md).
Keep the same cluster UID, node, architecture, image and provisioning settings,
with these distinct values:

| Value | `target-one` | `target-two` |
| --- | --- | --- |
| `storage.existingClaim` | `target-one-data` | `target-two-data` |
| `target.instanceIndex` | `1` | `2` |
| `network.bridgeAddress` | `10.84.40.1/24` | `10.84.41.1/24` |
| `service.clusterIP` | `10.96.40.10` | `10.96.40.11` |
| `service.targetPort` | `2443` | `2444` |
| Host MAC | `02:00:00:01:00:02` | `02:00:00:02:00:02` |
| BMC MAC | `02:00:00:01:00:01` | `02:00:00:02:00:01` |
| Chassis serial | `020000010001` | `020000020001` |

The release, PVC and identity must remain distinct. Budget for both guests and
runtime overhead before deploying them. Register a HostInband segment for each
bridge subnet, using its bridge address as gateway and reserving the first ten
addresses. The existing `10.96.40.8/29` underlay segment contains both BMC Service
addresses. Register each expected machine with its own identity, BMC address and
metadata name. Admit both through real Scout, then allocate one disk-installed
instance per machine in the same flat VPC using the preceding lifecycle
procedure. Check the Core-assigned addresses; do not assume these examples were
allocated. Wait for both installed operating systems and cloud-init to finish.

The checker needs a Linux or macOS client with Python 3.9+, OpenSSH and kubectl,
Python 3 and
SSH access inside both guests, and guest firewall permission for TCP connections
to ephemeral ports. Use the SSH key installed in both guests' cloud-init data:

```bash
python3 dev/deployment/lab/check-connectivity.py \
  --context kind-nico-tilt \
  --first-address 10.84.40.10 --second-address 10.84.41.10 \
  --identity /tmp/nico-instance-key \
  --first-known-hosts /tmp/nico-first-known-hosts \
  --second-known-hosts /tmp/nico-second-known-hosts
```

Required options have no defaults: `--context`, `--first-address`,
`--second-address` (both IPv4), `--identity`, `--first-known-hosts` and
`--second-known-hosts`. Defaults are `--namespace bmc-lab`, `--user lab`,
`--first-release target-one` and `--second-release target-two`. Repeated options
use the last value. The addresses and release names must be distinct; the
context must start with `kind-`. Each connection uses the
[SSH helper's validation and host-key rules](#ssh-to-an-installed-guest).
Use separate host-key files for newly provisioned instances.

The check runs both directions sequentially. In each direction the receiver
binds its guest address and an OS-selected TCP port, while the sender binds its
own guest address. The receiver verifies the peer source address and a random
32-byte payload, then echoes the payload for the sender to verify. SSH only
starts these programs; the test payload travels directly between the guests.
No guest files, persistent services, firewall rules or routes are changed.
The receiver accepts one connection, waits at most 30 seconds for it, and uses
10-second socket timeouts for data. It expires if the client is interrupted.
Client-side waits for listener startup and the sender are each bounded to 30
seconds, followed by at most 15 seconds for the receiver's result. Local SSH
and relay process groups are stopped on completion, Python exceptions or Ctrl-C.
SIGTERM, SIGHUP and forced termination do not guarantee local process cleanup.

Success prints one JSON object with `directions`, containing the verified
`source`, `destination` and payload `bytes` for each direction, and exits 0.
A failed probe prints an error to stderr and exits 1 without a success object;
argument parsing errors, non-kind contexts and duplicate addresses/releases exit 2.
Validation failures inside the SSH helper (such as a missing key, invalid path
or address outside the target subnet) instead fail the probe with exit 1.
`--help` prints usage and exits 0. A failed second
direction does not erase the fact that the first connection was attempted.
The check neither allocates nor releases instances and leaves their disks intact.

Repeat after rebooting one instance through Core, verifying that the other
instance's boot ID stays unchanged. Release one instance and confirm the other
remains reachable before releasing it too. Keep both targets, Core database and
Vault together until release completes; then discard the dedicated cluster.
IPv6, Internet access, DPU routing and secure erase require separate validation.
