# Native guest lab prerequisites

The optional BMC-backed guest lab requires a Linux runtime with native KVM.
Run the preflight on that runtime before building images or allocating targets.
Ordinary Tilt development with machine-a-tron does not require KVM or this check.
On macOS, run it inside the Linux VM that will host the guests; running it on the
macOS client reports an unsupported platform.

## Ubuntu host setup

For an existing Ubuntu 22.04 or newer checkout on native ARM64 or AMD64, prepare
packages and tools before creating the kind cluster:

```bash
bash dev/deployment/setup-host.sh
export PATH="$HOME/.local/bin:$PATH"
```

`--user` defaults to the invoking account (`SUDO_USER` when run as root). It must
be an existing non-root user with a home directory. Missing values and unknown
arguments fail; `--help` prints usage without changes. The script invokes sudo
when needed. Start a new login session afterward for Docker group membership.

Setup installs Git, curl, jq, Python 3, rsync, OpenSSH client and CA certificates
through apt without upgrading already-installed host packages, and the [pinned native tools](../install-tools.md) in that user's
`~/.local/bin`. With no Docker/containerd installation, it adds Docker's Ubuntu
apt repository and installs Docker CE, containerd and Buildx/Compose plugins.
It enables the services, configures `GODEBUG=tlsmlkem=0` in their systemd drop-ins
for registry TLS compatibility, and checks daemon access as the developer.
Packages follow the configured apt repositories; they are not a frozen image.

An existing complete runtime must have containerd and Buildx, a Docker group,
and a working local daemon at `/var/run/docker.sock`. It is reused without
service restarts or configuration changes; partial or stopped installations
fail before package installation and require operator repair. Setup still
installs the host packages and replaces the selected user tools on repeated
runs. It does not change shell profiles, create a cluster, or configure KVM,
huge pages, watcher limits or host networking. Use the capacity and KVM checks
below before allocating target guests.

For the optional macOS-hosted lab, first create the Ubuntu VM using the
[standalone VM launcher](../vfkit/README.md#create-a-managed-ubuntu-vm), then run
this command inside its synced checkout. Ordinary Tilt use can instead use a
local Docker runtime and the shared tool installer directly.

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

For the [IPv6 boot profile](../tilt/README.md#ipv6-boot-profile),
add `--ipv6-boot` to both `build-scout.py` and `build-imager.py` invocations.
This boolean option defaults to false. It omits the pinned IPv4 resolver for
`cluster.local` so each boot stage uses DHCP-provided DNS. Scout's rootfs
fallback uses `http://carbide-static-pxe.forge:8080`; the matching Tilt profile
supplies the API and PXE URLs. Deploy the resolver and advertise it through
DHCPv6 before boot. Use a new output directory and artifact tag when switching
profiles; the helpers never rewrite an existing artifact directory. The Scout
payload includes `scout.network-profile` and the imager payload includes
`qcow-imager.network-profile`, each containing `default` or `ipv6` plus a newline.
Copy these files into the artifact image with the payloads. The matching Tilt
`--ipv6-boot` profile requires the IPv6 markers and targets with IPv4 provisioning
disabled; see its documented fresh-cluster setup.

Successful output contains `ARCH/scout.efi`, `ARCH/scout.squashfs` and
`ARCH/scout.network-profile`, where `ARCH` is `aarch64` or `x86_64`. `build/` retains the staged profiles,
mkosi output and downloaded package cache. `build-info.json` records the native
architecture, package version, requested virtual-kernel package and SHA-256
hashes of the input package and both payloads, plus the `ipv6_boot` boolean. The manifest is written only after
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
  "/tmp/nico-scout-artifacts/boot/$(uname -m)/scout.network-profile" \
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

Without `--ipv6-boot`, the VM Scout loader's fallback rootfs URL points to
`http://nico-pxe.nico-system.svc.cluster.local/public/blobs/internal/ARCH/scout.squashfs`.
An explicit `newrootfs=` kernel argument still takes precedence. Hardware source
profiles retain their original fallback. In this default profile, both VM stages route `cluster.local`
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
Continue with the [instance lifecycle checks](#allocate-and-recycle-an-instance)
and [explicit-address IPv6 routing checks](#two-instance-ipv6-routing).
See [managed DHCPv6 guests](#managed-dhcpv6-guests) for dynamic IPv6 addressing.
The [guest profile generator](#allocate-and-recycle-an-instance) supports an
IPv6-only installed OS. SLAAC and Internet IPv6 need separate validation in
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
  "/tmp/nico-scout-artifacts/imager/$(uname -m)/qcow-imager.network-profile" \
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
virtual kernel. The default staged profile adds the same kind DNS routing as Scout.
With `--ipv6-boot` it instead relies on DHCP-provided DNS.
No OS image, tenant credentials or cloud-init data is embedded in this artifact.

Successful output is `ARCH/qcow-imager.efi` and `ARCH/qcow-imager.network-profile`,
with staged sources and caches in
`build/`. `build-info.json` records the architecture, selected profile, payload
SHA-256, installer SHA-256 and the `ipv6_boot` boolean. Failure leaves the build workspace for diagnosis;
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
access. Required flags have no defaults: `--context`, `--address` (IPv4 or IPv6),
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

For a native Ubuntu 24.04 image using systemd-networkd, the shared generator
produces this OS configuration and its on-disk network profile:

```bash
python3 dev/deployment/lab/guest-config.py \
  --os-image-id "$os_image_id" --mac 02:00:00:01:00:02 \
  --public-key /tmp/nico-instance-key.pub --generation generation-2 \
  --network ipv6-only > /tmp/nico-instance-os.json
```

The image UUID, guest MAC, public key file and nonempty printable generation marker are required.
The marker and public key line must contain only characters accepted by
Python's `str.isprintable()` (ASCII space is allowed); a whitespace-only marker
is rejected. The MAC must be a nonzero unicast Ethernet address. The key
file must contain one OpenSSH public key; `ssh-keygen` validates it. Private keys
are rejected. The command only prints JSON: it does not contact Core, create
files or allocate resources. Redirect its output to your chosen OS configuration
file and use that file with the allocation command below.

`--network` accepts `ipv4` (default), `dual-stack` and `ipv6-only`. The profiles
select DHCPv4, DHCPv4 plus DHCPv6, and DHCPv6 respectively. IPv6 profiles accept
router advertisements and explicitly solicit DHCPv6; they require the
[managed DHCPv6 target setup](#managed-dhcpv6-guests). IPv4 link-local fallback is
disabled in every profile. No static address or DNS server is supplied; DNS must
come from the lab's DHCP/RA configuration. No SLAAC addresses are requested by
this lab's router advertisements.

The generated cloud-init creates user `lab` with passwordless sudo and the
provided public key, writes `/etc/nico-lab-generation`, and installs
`/etc/systemd/network/05-nico-lab.network` matched to the guest MAC. That file
takes precedence over the image's generated networkd file. Cloud-init reloads
networkd after writing it; before that reload, Core's default NoCloud network
configuration enables both DHCPv4 and DHCPv6. This fixture targets the lab's
single guest NIC; it does not disable networking on additional interfaces. Subsequent disk boots use the persistent selected profile.
These profiles do not alter firmware, Scout or imager provisioning networking.
Other operating systems or network managers require their own configuration.

For `ipv6-only`, connect using Core's allocated IPv6 address, wait for cloud-init
to finish, and verify that `ip -j address` has no IPv4 address outside loopback
and that `ip -4 route show table all` has no non-loopback route. Repeat those
checks after Core reboot. Keep a separate known-hosts file for the new generation
and confirm that the old generation's disk marker is absent.

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
sanitize coverage, SLAAC and Internet IPv6 remain separate acceptance boundaries in
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
`--second-address` (both IPv4 or both IPv6), `--identity`, `--first-known-hosts` and
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
argument parsing errors, non-kind contexts, mixed address families and duplicate addresses/releases exit 2.
Validation failures inside the SSH helper (such as a missing key, invalid path
or address outside the target subnet) instead fail the probe with exit 1.
`--help` prints usage and exits 0. A failed second
direction does not erase the fact that the first connection was attempted.
The check neither allocates nor releases instances and leaves their disks intact.

Repeat after rebooting one instance through Core, verifying that the other
instance's boot ID stays unchanged. Release one instance and confirm the other
remains reachable before releasing it too. Keep both targets, Core database and
Vault together until release completes; then discard the dedicated cluster.
Internet access, DPU routing and secure erase require separate validation.

## Two-instance IPv6 routing

The same SSH and connectivity helpers accept literal IPv6 addresses. The SSH
helper validates them against the selected target's retained `bridgeAddressV6`,
rejects the bridge gateway, subnet-router anycast address and scoped addresses,
and keeps the private key on the client. A target without an IPv6 bridge cannot
be accessed through this helper using IPv6. The checker requires both endpoints
to use the same address family; it binds IPv6 sockets for IPv6 inputs and checks
the actual peer source. Its payload, timeout, process-cleanup and exit-status
contracts are the same as the preceding IPv4 procedure.

Start with two fresh targets and the [IPv6 chart prerequisites](../../../helm/nico-bmc-target/README.md#optional-ipv6-guest-routing).
Add these values to the respective target files before deploying them:

| Value | `target-one` | `target-two` |
| --- | --- | --- |
| `network.bridgeAddressV6` | `fd42:8124:40::1/64` | `fd42:8124:41::1/64` |
| Guest address | `fd42:8124:40::10/64` | `fd42:8124:41::10/64` |
| Peer route | `fd42:8124:41::/64` via `fd42:8124:40::1` | `fd42:8124:40::/64` via `fd42:8124:41::1` |

These are explicit test addresses, not NICo DHCPv6 allocations. Verify that the
prefixes do not overlap existing routes. Keep the preceding IPv4 provisioning,
OS installation and SSH-key setup. For the Ubuntu systemd-networkd image, merge
this fragment into target one's cloud-init user-data, alongside its SSH user and
generation marker. For target two, use its MAC ending `02:00:02`, address,
gateway and peer prefix from the table:

```yaml
write_files:
  - path: /etc/systemd/network/05-lab-ipv6.network
    permissions: '0644'
    content: |
      [Match]
      MACAddress=02:00:00:01:00:02

      [Network]
      DHCP=ipv4
      IPv6AcceptRA=no
      Address=fd42:8124:40::10/64

      [Route]
      Destination=fd42:8124:41::/64
      Gateway=fd42:8124:40::1
runcmd:
  - [networkctl, reload]
```

The early networkd filename takes precedence over the image's generated network
file for the matched NIC. DHCPv4 remains enabled; the explicit IPv6 address and
peer route persist on disk. This fragment intentionally disables RA acceptance
and adds no IPv6 default route. It does not test DHCPv6, SLAAC, IPv6-only guests
or Internet IPv6. For dynamic addresses and IPv6-only installed guests, use the
[managed DHCPv6 setup](#managed-dhcpv6-guests) and the
[guest profile generator](#allocate-and-recycle-an-instance). SLAAC and Internet
IPv6 have separate acceptance gates in
[the migration](https://github.com/dsx-ai-factory/infra-controller/issues/8124).

After both installed guests complete cloud-init, run:

```bash
python3 dev/deployment/lab/check-connectivity.py \
  --context kind-nico-tilt \
  --first-address fd42:8124:40::10 --second-address fd42:8124:41::10 \
  --identity /tmp/nico-instance-key \
  --first-known-hosts /tmp/nico-first-known-hosts \
  --second-known-hosts /tmp/nico-second-known-hosts
```

Repeat the IPv4 check to verify both families, then repeat the IPv6 check after
Core reboots one instance. Verify its boot ID changes, the peer's does not, and
both disk configurations persist. Release both through Core and require Ready
with live Scout before destroying the disposable cluster.

The portable socket regression uses IPv6 loopback when available and reports a
skip when the runner lacks it. This does not count as native IPv6 acceptance.
Both native architectures need the real two-guest IPv6 checks even when an
upstream Internet IPv6 route is unavailable.

## Managed DHCPv6 guests

For DHCPv6, create the dedicated lab cluster with `setup-kind.sh --ip-family dual`
and pass `--dhcpv6` with the existing `--bmc-lab --persistent-vault
--boot-artifact-image IMAGE` Tilt options. The flag requires BMC mode and exactly
one node with an IPv6 pod CIDR. It enables the existing `nico-dhcp` chart's
DHCPv6 deployment, defaulting to stable development server identifier
`020000000006` unless `nico-dhcp.dhcp.v6ServerIdentifier` is configured.
The DHCP image contains both daemons on AMD64 and ARM64; no upstream IPv6 route
is needed for private guest-to-guest tests.

Before allocating guests that need DNS, set `nico-dhcp.dhcp.v6DnsServers` in the
Tilt values file to a comma-separated list of IPv6 resolvers reachable from the
guest networks. With direct Helm, use `dhcp.v6DnsServers` in the DHCP release's
values. The default is empty. The relay's advertisements do not supply DNS.
Set the resolver before first boot: the exercised Ubuntu 24.04 networkd client
did not publish a resolver added during an existing lease's rebinding until its
interface was reconfigured. A planned guest `networkctl reconfigure INTERFACE`
or reboot reacquires configuration; it can briefly interrupt access.

Obtain the Service address after the DHCP release is ready:

```bash
kubectl -n nico-system get service nico-dhcp6 -o jsonpath='{.spec.clusterIP}'
```

Set each target's `network.dhcpv6.server` to that result and
`network.dhcpv6.podCIDR` to the node’s IPv6 entry in `spec.podCIDRs`. Configure distinct
`network.bridgeAddressV6` /64 prefixes, and enable `network.dhcpv6.defaultRoute`
when the node should route between those target networks. This enables managed
RA, not SLAAC. Add each IPv6 prefix to its corresponding host-inband segment by
repeating `--prefix` in the fresh-site registration command. Use `--reserve-first`
to exclude the bridge gateway, as in the IPv4 procedure. Do not recreate an
existing segment or replace credentials to change a running site's configuration.

For the installed guest, use the [OS configuration generator](#allocate-and-recycle-an-instance)
with `--network dual-stack` instead of the explicit-address fixture. It installs
an early systemd-networkd file matched to the guest MAC, enables both DHCP
families and RA acceptance, and reloads networkd after cloud-init writes it.

This keeps IPv4 provisioning and installed IPv4 networking while exercising
DHCPv6, including clients with non-MAC DUIDs. Read the allocated address from
Core's instance network status and confirm the same dynamic address inside the
guest before running the existing IPv6 connectivity checker. Do not assume the
first available address or reuse the explicit fixture's `::10` address. Validate
both peer directions, reboot, lease renewals and release with live Scout.

The lease-handoff behavior is tracked independently in
[PR #7934](https://github.com/dsx-ai-factory/infra-controller/pull/7934).
Record the DHCP image's source revision when validating transitions between
firmware, Scout, imager and installed-OS identities. Enabling the deployment
alone does not prove that those transitions work.

### DHCPv6 Service recovery

The DHCPv6 Service address is allocated by Kubernetes. After recreating that
Service (including removal and redeployment of the DHCP chart), read its address
again and update each target's `network.dhcpv6.server` through Helm. This triggers
the [retained-state restart](../../../helm/nico-bmc-target/README.md#configuration-and-retention); resume the saved guests through Redfish.
Target readiness checks the runtime, not DHCP lease availability. Run the guest
address and connectivity checks after this recovery.

## Repeat the instance lifecycle

`lifecycle.py` operates on two explicitly selected, already admitted no-DPU lab
machines. Deploy the stack and targets, register the native OS image with its
SHA-256 digest, and create a flat VPC using the preceding instructions first.
Both machines must be Ready with live Scout connections. The OS image must
support the Ubuntu/networkd cloud-init fixture; the root disk must be `/dev/vda`.
For IPv6 guests, deploy the managed DHCPv6 relay, resolver and routes first.
The DHCP runtime must include the
[boot-stage lease handoff fix](https://github.com/dsx-ai-factory/infra-controller/pull/7934).

Select a new private directory whose parent exists. Replace the example Core IDs
with those returned by your lab's Core API:

```bash
python3 dev/deployment/lab/lifecycle.py \
  --context kind-nico-tilt --state-dir /tmp/nico-lifecycle-run \
  --target "target-one=$FIRST_MACHINE_ID" \
  --target "target-two=$SECOND_MACHINE_ID" \
  --vpc "$VPC_ID" --os-image "$OS_IMAGE_ID" --network ipv6-only
```

The context, two target pairs, VPC UUID, OS-image UUID and state directory are
required for a new run. Releases and machine IDs must both be distinct. Only
explicit `kind-` contexts and deployments labeled `nico-bmc-target` are accepted.
The runner verifies cluster UID, inventory owner and Core BMC address/MAC before
allocating; an existing state directory is rejected. Directory paths cannot
contain whitespace, quotes, `%`, `$` or backslashes because SSH pins keys there.

`--namespace` defaults to `bmc-lab`; both targets use it. `--core-namespace`
defaults to `nico-system`. `--network` accepts `ipv4` (default), `dual-stack` or
`ipv6-only`. `--cycles` accepts 1 or 2 and defaults to 2. `--timeout` is the
per-transition polling deadline in seconds, defaults to 1800 and accepts 60–7200;
individual command deadlines can extend a final poll beyond that boundary.
There is no total run deadline. `--internet-ipv6` is false by default and requires
an IPv6 guest profile. It adds certificate-verified IPv6 HTTPS to
`https://www.google.com` for both guests before and after each reboot. Omission
means external IPv6 was not tested; internal IPv6 remains tested for IPv6
profiles. It is suitable for an environment without an external IPv6 route.

Each cycle allocates two distinct instances, verifies the current disk generation,
cloud-init and finite DHCP leases, and checks bidirectional TCP with unchanged
peer source addresses. IPv6-only guests must have no non-loopback IPv4 address or
route and must fail an IPv4 Internet ping. Each guest is then rebooted through
Core: its disk marker must persist and its boot ID must change while its peer's
boot ID stays unchanged. Connectivity is checked again. The runner releases the
first guest and checks that its peer remains reachable with the same boot ID,
then releases the second, waiting for Ready
and live Scout after each release. A second cycle repeats allocation from these
released machines. This does not rebuild the stack or prove a fresh-cluster run.

Readiness verifies the disk root, cloud-init completion and allocation generation
over SSH using provisional host keys. Only a successful verification persists
the generation's key; failed probes cannot pin an imager or previous guest.
Serial-log rotation does not affect readiness. Existing pins remain authoritative.

The directory contains a newly generated SSH key, per-generation pinned host
keys, allocated identities, network observations and an atomic `state.json`
journal. Treat it as private; do not commit or share it. Only one process may use
a journal at a time. Allocation intent is saved before the API call, so a lost
response can be reconciled without adopting an unrelated instance. A failure
retains the journal and allocation output, and collects target consoles and
machine state in a new diagnostic directory without overwriting prior evidence.
Diagnostic collection skips targets whose ownership cannot be verified; it never
force-clears Core state or silently retries allocation.

After a failed run, release only its recorded instances with:

```bash
python3 dev/deployment/lab/lifecycle.py \
  --state-dir /tmp/nico-lifecycle-run --release-only
```

Cleanup reads context and identities from the journal, revalidates target
ownership, and rejects replaced clusters, targets or instances. It attempts each
target independently, reports all failures, and returns nonzero if any remain. Do not supply
`--context`, `--target`, `--vpc` or `--os-image` with `--release-only`. Other run
settings are read from the journal. Repeating cleanup after successful release
is harmless. Run directories support cleanup, not resuming an interrupted test;
start a new directory for another run. Neither mode deletes VPCs, OS catalog
entries, target PVCs, the stack or its credentials. Exit status is zero only when
all selected checks or cleanup complete; failures return nonzero and retain
evidence.
