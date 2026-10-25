# Native guest lab prerequisites

The optional BMC-backed guest lab requires a Linux runtime with native KVM.
Run the preflight on that runtime before building images or allocating targets.
Ordinary Tilt development with machine-a-tron does not require KVM or this check.
On macOS, run it inside the Linux VM that will host the guests; running it on the
macOS client reports an unsupported platform.

## Host budget

Python 3.9 or newer and a Linux AMD64 or ARM64 host are required. Run as the identity that
will access KVM. The tool does not change device permissions or invoke `sudo`.
All budget flags below are required, positive integers with no defaults:

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
  \
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
The artifact image includes only the native architecture's nonempty `ipxe.efi`,
`scout.efi` and `scout.squashfs`; missing payloads fail the image build. It does
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
error. The image must already be available to the cluster. BMC mode deploys
Core, BMC proxy, DHCP and PXE, with real Scout authentication and hardware polling.
It disables MAT, MAT's readiness check and the MAT-specific SSH console resource.
Tilt service readiness therefore does **not** establish guest admission or
instance readiness. The checks below establish Scout connectivity separately.
An existing opposite-mode Helm release makes Tilt refuse the mode change;
manually removing a release does not migrate retained site data.

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
prove OS installation, instance assignment, release, or tenant IPv6 connectivity;
those remain separate milestones in
[issue 8124](https://github.com/dsx-ai-factory/infra-controller/issues/8124).
