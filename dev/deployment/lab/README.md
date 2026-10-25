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
