# Local development with Tilt

Tilt runs the complete local NICo development stack from the repository's Helm
charts and Kubernetes sources.

> [!WARNING]
> This workflow is intended only for local development and is
> provided as-is. Use it at your own risk.

## Requirements

- Docker connected to a Linux AMD64 or ARM64 daemon, with BuildKit available
- Tilt
- Helm 3 or Helm 4, plus Python 3 for Tilt's `helm_resource` extension
- OpenSSL for validating the local Vault public CA certificate
- `kubectl` connected to a native kind cluster; the setup helper below requires
  kind 0.32.0 or newer and `jq`

The Tiltfile refuses to load unless the active Kubernetes context starts with
`kind-`. This is stricter than Tilt's built-in local-cluster safety check and
prevents this development stack from targeting another cluster type.

## Create the development cluster

Start a local Docker runtime and select its Docker context. Run the shared
cluster helper with Docker, kind, `kubectl` and `jq` on `PATH`. The runtime must
provide Linux AMD64 or ARM64 containers through a local Docker Unix socket.

From the repository root:

```bash
bash dev/deployment/setup-kind.sh --name nico-tilt
export KUBECONFIG="$HOME/.kube/kind-nico-tilt.yaml"
kubectl --context kind-nico-tilt get nodes
```

Select an unused cluster for Tilt.

For an isolated dual-stack cluster, select a different name:

```bash
bash dev/deployment/setup-kind.sh --name nico-dual --ip-family dual
export KUBECONFIG="$HOME/.kube/kind-nico-dual.yaml"
kubectl --context kind-nico-dual get nodes -o wide
```

Both architectures use the same helper and configuration. Dual stack configures
Kubernetes pod/service networking and requires IPv6 support in the container
runtime's Linux kernel and Docker network, but not Internet IPv6.

The helper accepts these options; repeated options use the last value:

| Option | Default | Contract |
| --- | --- | --- |
| `--name NAME` | `nico-tilt` | Lowercase DNS label, 1–49 characters. Selects one cluster with one `NAME-control-plane` node. |
| `--ip-family FAMILY` | `ipv4` | `ipv4` or `dual`. An existing cluster's pod CIDRs must match. IPv6-only clusters are not accepted by this helper. |
| `--image IMAGE` | The digest-pinned Kubernetes 1.36.1 node image from the [kind 0.32.0 release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.32.0) | Used only when creating a cluster; existing nodes are never upgraded. Custom images must work with the installed kind version and selected daemon architecture. |
| `--kubeconfig PATH` | `$HOME/.kube/kind-NAME.yaml` | Exported after node readiness, architecture and pod-family validation. Kind merges an existing file and selects the cluster context in that file. Relative paths resolve from the working directory. |
| `--help`, `-h` | — | Print usage and exit without host changes. |

Missing values, invalid names/families and unknown options fail before contacting
Docker. The helper requires kind 0.32.0 or newer for the pinned node image. It
captures the active Docker context, requires a local Unix socket and a Linux
AMD64/ARM64 daemon, and forces kind's Docker provider. Node images use the daemon
architecture. On Linux, the client and daemon architectures must also match.
The helper does not detect runtime hardware emulation. SSH/TCP
Docker endpoints are rejected because this helper does not arrange remote API
access; run it on that Docker host instead.
A `DOCKER_HOST` Unix socket is honored with Docker's default context.

Validation uses a private temporary kubeconfig with explicit context selection;
inherited `KUBECONFIG` is ignored. Your normal kubeconfig and current context
remain unchanged unless you explicitly select that file with `--kubeconfig`.
New clusters publish their API on `127.0.0.1`, on a port chosen by kind. Run Tilt
from the same client, using the exported kubeconfig and the same
Docker context.

Setup reuses compatible clusters without pulling a node image, restarting nodes
or changing installed workloads. It rejects multiple-node clusters and mismatched
pod families or architectures. It passes `--wait 180s` to kind and
`--timeout=180s` to the node readiness check; these do not impose an overall
timeout on image downloads or setup. Failure returns nonzero, removes temporary
files and lets kind clean up a failed creation. A cluster that was created
successfully but fails subsequent validation remains available for diagnosis.
The helper does not delete/recreate preexisting clusters or repair incompatible
settings. Choose another name or explicitly remove an unused
cluster through kind after accounting for its workloads and data.

## Start

From the repository root, run:

```bash
tilt up -f dev/deployment/tilt/Tiltfile
```

The default stack includes:

- cert-manager
- the CloudNativePG operator and one PostgreSQL cluster
- Vault in local development mode
- NICo API, BMC proxy, DHCP server, machine-a-tron, and mat-k8s-controller

Enable REST with:

```bash
tilt up -f dev/deployment/tilt/Tiltfile -- --rest=true
```

This adds:

- Temporal and its local namespaces
- Keycloak and the `nico-dev` realm
- NICo REST API, database migrations, certificate manager, site manager,
  workflow workers, and site agent
- automatic local site registration

Enable MCP separately with `--mcp=true`; it also enables REST.

Tilt forwards these endpoints:

| Service | URL |
| --- | --- |
| NICo Web UI | `https://localhost:1079/admin/` |
| Machine-A-Tron UI | `https://localhost:1266/` |
| REST API | `http://localhost:18388` |
| Keycloak | `http://localhost:18082` |
| MCP | `http://localhost:18080/mcp` |
| Temporal UI | `http://localhost:18233` |
| Grafana | `http://localhost:13000` |
| Prometheus | `http://localhost:19090` |

## Core prerequisites

The `nico-core-prereqs` release uses the shared `helm-prereqs` chart and
[local development profile](../../../helm-prereqs/values/local-development.yaml).
It owns Core's CA, Issuer, SSH host key, Vault Secrets and connection ConfigMaps.
Tilt supplies the existing local CA and OpenSSH key fixture with Helm's
`--set-file`; the same profile and files work with
[direct Helm](../../../helm-prereqs/README.md#local-core-prerequisites).
With REST enabled, the same release also owns the REST trust resources described
below. Vault and PostgreSQL installation, database provisioning and site
registration remain separate steps. Use a fresh development cluster when switching from
Tilt-created Core prerequisites; Helm does not adopt their existing objects.

## MAT networking

Helm owns the local network Services. The DHCP chart pins `nico-dhcp` to
`10.96.0.20` through `service.clusterIP`; the local values set Kea's
`config.kea.serverIdentifier` to the same address for unicast renewals. An empty
`service.clusterIP` retains Kubernetes address allocation for other chart users.
An explicit address must be IPv4, available in the cluster's ServiceCIDR, and is
immutable after creation. The chart does not infer the Kea identifier from it.

The MAT chart uses `dhcpRelay.baseIP: 10.96.0.21`, `listenPort: 67` and
`serverAddress: 10.96.0.20:67` for both its relay Service and the placeholders
in the local TOML configuration. Its optional `service.ssh.headless: true`
provides direct pod DNS for console ports reported by Redfish. Both settings
are ordinary chart values; Tilt no longer creates separate network Services.
The same values render these resources with Helm without evaluating a Tiltfile.
This does not replace the remaining Tilt prerequisite and site-registration steps.

Use a fresh development cluster when switching from a checkout that created
these Services directly in Tilt. Helm will reject a relay or console Service
owned outside its release, and an existing DHCP Service may already have a
different immutable ClusterIP. There is no in-place ownership or address migration
for these Services. Keep any cluster with needed lab data until that data
has been accounted for; the cluster setup helper does not recreate it.

## REST trust prerequisites

With `--rest=true` or `--mcp=true`, `nico-core-prereqs` also owns the
`ca-signing-secret` Secrets in `cert-manager` and the REST namespace, the
`nico-rest-ca-issuer` ClusterIssuer, and the three Temporal server Certificates.
They use the same supplied local CA as Core. Tilt forwards the REST and Temporal
namespace settings from `tilt.localConfig` and the issuer name from
`tilt.rest.global.certificate.issuerRef`; the local issuer must be a ClusterIssuer.
The chart's [REST trust settings](../../../helm-prereqs/README.md#local-rest-trust)
also work with direct Helm. Namespace creation precedes the release, and Temporal
startup waits for all three Certificates to become Ready.

Use a fresh development cluster when switching from raw Tilt CA Secrets and
Temporal Certificates; Helm does not adopt them. Reconciliation with unchanged
CA inputs preserves trust and certificate identities. Disabling REST updates the
prerequisite release to remove its REST trust resources, so stop REST consumers
before switching to Core-only mode. Uninstalling the prerequisite release removes
both Core and REST trust resources. Preserve its CA and SSH key inputs for
restoration against retained application data; neither removal nor restoration
registers a new site or changes database ownership.

## REST common prerequisites

With `--rest=true` or `--mcp=true`, the `nico-rest-common` Helm release owns
`db-creds`, `keycloak-client-secret`, `temporal-encryption-key`,
`image-pull-secret` and the `temporal-client-cloud-cert` Certificate in the REST
namespace. Tilt supplies the database, Keycloak and Temporal inputs from
`tilt.localConfig` and the issuer from `tilt.rest.global.certificate.issuerRef`.
The shared [chart](../../../helm/rest/README.md#installing-common-resources-separately)
can also be installed directly.

Tilt sets `secrets.migrationHooks=false`: these are ordinary Helm resources
installed before the migration Job. The `rest-certificate-ready` resource waits
for certificate issuance before starting API and workflow services. The chart
leaves CA signing, database provisioning and site registration to their existing
setup steps.

Use a fresh development cluster when switching from a checkout where Tilt
created these resources directly. Helm does not automatically adopt those
objects. On a stack whose common resources were installed by the
`nico-rest-common` Helm release, reconcile them from the repository root with:

```bash
helm upgrade nico-rest-common helm/rest/nico-rest/charts/nico-rest-common \
  --namespace nico-rest --reuse-values --wait --timeout 5m
kubectl -n nico-rest wait --for=condition=Ready \
  certificate/temporal-client-cloud-cert --timeout=5m
```

Select the stack's kubeconfig first; use its REST namespace if it differs from
`nico-rest`. Reconciliation keeps the supplied credential values and site
identity. Uninstalling `nico-rest-common` removes its four Secrets and Certificate
and interrupts consumers that need them. Preserve the release's credential
values before removal; restoring against retained data requires the same
Temporal encryption key and database credentials. This release does not own
PostgreSQL storage or the site-registration Secret.

## Verify stack readiness

The `stack-ready` resource runs after Core and MAT start. It requires a nonempty
set of distinct MAT host identities matching Core, every host in `Ready` or
`Assigned/Ready`, and a responsive Scout stream for every idle `Ready` host.
Switches, DPUs and power shelves do not count as hosts. Assigned hosts run a
tenant OS and do not require Scout.

With `--rest=true` or `--mcp=true`, verification also waits for matching site
registration IDs, an online `Registered` site, the same usable host identities in
REST, and a successful final-page completion logged after verification started,
with the expected nonzero host count. This can take several minutes because REST
inventory is periodic. The completion record must contain structured page
metadata; rebuild the REST workflow image from this checkout when upgrading an
existing stack. Older worker images without that metadata cannot pass this
check. This receipt proves final-page processing, not atomic completion of every
page in an inventory batch.
A failure makes the resource fail, including in `tilt ci`; Kubernetes pod
readiness alone is insufficient. Verification checks discovery and inventory,
not instance allocation or tenant network connectivity.

While Tilt is running, rerun the same check from the repository root using the
cluster kubeconfig selected above:

```bash
python3 dev/deployment/verify-stack.py
python3 dev/deployment/verify-stack.py --rest
```

The verifier uses Python 3, Bash, `kubectl`, `jq` and `curl`. It captures the active
Kubernetes context, or accepts `--context NAME`; Tilt passes its selected
`kind-` context explicitly. It uses the bundled Core admin CLI with a short-lived certificate
issued by the local Vault, without changing Core or Helm configuration during
verification. Tilt's `vault-admin-ca` prerequisite exports the public Vault CA to
an ignored `.local/CONTEXT/vault-admin-ca.json` file beside the Tiltfile. The Core
Helm release consumes it through `siteConfig.adminRootCertPem`; its local auth
configuration also recognizes the Vault issuer. The export contains no private
key. If the ephemeral Vault restarts with a new CA, rerun `vault-admin-ca` and
then update `nico-api` before rerunning readiness.
It polls every five seconds with a 900-second overall deadline, limits individual
commands to 45 seconds, and returns nonzero on failure. `--timeout SECONDS`
accepts 1–3600; repeated options use the last value. `--help` lists the options.

Defaults are `--core-namespace nico-system`,
`--mat-service nico-machine-a-tron-mat-0-bmc-mock`, and
`--rest-namespace nico-rest`. This check supports one MAT control Service at
HTTPS port 1266 and, with REST enabled, at most 100 hosts in `test-org` using the
local `nico-dev` Keycloak realm. `--api-url` defaults to
`http://localhost:18388` and `--keycloak-url` to `http://localhost:18082`;
REST checks require these port-forwards to remain available. REST namespace and
URL options are ignored without `--rest`. The verifier creates no port-forwards
and does not register sites, reset machines or repair failed services.

To rerun from Tilt, trigger `stack-ready` in the UI or use:

```bash
tilt trigger stack-ready
```

## NICo PostgreSQL dashboard

With `--observability=true`, Tilt installs the shared
[`nico-postgres-exporter` Helm chart](../../../helm/charts/nico-postgres-exporter/README.md)
as a separate release in `nico-system`. It builds the exporter image through the
shared Rust artifacts image and provisions **NICo / PostgreSQL** alongside
**CloudNativePG** in Grafana's **PostgreSQL** folder.

```bash
tilt up -f dev/deployment/tilt/Tiltfile -- --observability=true
```

Open [Grafana at localhost:13000](http://localhost:13000) and choose
**PostgreSQL → NICo / PostgreSQL**. The dashboard includes connection health and
a cumulative **Slow queries** table. See the chart README for collection behavior,
metric contracts, binary configuration, and installation with your own Grafana.

Tilt passes `tilt.observability.postgresExporter` as chart values, supplying the
image and the `postgresql` host, port, and database name from the shared
local prerequisite profile. Development
values use `database.credentialsSecret: nico-pg-cluster-app` and
`database.sslMode: disable`; the credentials Secret is in the release namespace.
For verified TLS, configure `database.sslMode` and `database.sslRootCertSecret`
under `tilt.observability.postgresExporter`. Tilt enables the ServiceMonitor and
places the dashboard ConfigMap in `observability`, waiting for Prometheus,
PostgreSQL, and local configuration before installation. Disabling observability
excludes the exporter release and dashboard.

With observability enabled, Tilt sets `pg_stat_statements.track: top` and
`pg_stat_statements.track_utility: off`, excluding utility statements that could
contain password literals from new query statistics. It also grants the application
owner `pg_read_all_stats` membership. CloudNativePG manages the
preload library and installs the extension in connectable databases when that
parameter is present; changing the preload configuration can restart PostgreSQL.
See [CloudNativePG's managed-extension contract](https://cloudnative-pg.io/docs/1.28/postgresql_conf/#enabling-pg_stat_statements).
The shared chart itself does not configure extensions or database permissions.
For external databases and existing statistics collected before disabling utility
tracking, follow the [database setup instructions](../../../helm/charts/nico-postgres-exporter/README.md#database-collection-and-dashboard).

The dashboard JSON now lives in the chart at
[`dashboards/nico-postgres.json`](../../../helm/charts/nico-postgres-exporter/dashboards/nico-postgres.json).
Tilt watches the chart directory; edits update the Helm-managed ConfigMap and
Grafana's existing sidecar loads them. Allow one collection and scrape interval
for fresh data. Chart-only installs can enable the optional umbrella dependency
or install this chart as a separate release; both are disabled by default.

If upgrading a Tilt environment from the direct Kubernetes exporter wiring,
remove the old Tilt-owned `nico-postgres-exporter` Deployment, Service, and
ServiceMonitor in `nico-system`, plus the old
`grafana-dashboards-nico-postgres` ConfigMap in `observability`, then retry the
exporter resource. Helm cannot adopt those existing objects. This is a one-time
migration; do not remove resources already owned by the new Helm release.

## Pod logs

Grafana Alloy collects stdout and stderr from every pod through the Kubernetes
API and sends the logs to the local Loki instance. Grafana has Loki provisioned
as its default data source and permits anonymous access because its Tilt
port-forward listens only on localhost.

Open **Explore** in Grafana and use a LogQL selector such as:

```text
{cluster="local-carbide"}
```

Narrow the results with the `namespace`, `workload`, `pod`, `container`, or
`app` labels. For example, machine-a-tron logs can be selected with:

```text
{namespace="nico-system", app="nico-machine-a-tron"}
```

Loki stores logs on a 2 GiB persistent volume and retains them for 24 hours.
The volume survives pod restarts but is removed with the local Kind cluster.

## Pod metrics

Prometheus discovers ServiceMonitor and PodMonitor resources across the local
cluster. It also automatically scrapes every Kubernetes Service labeled
`app.kubernetes.io/metrics` whose service name ends in `-metrics` or whose port
is named `metrics`. This collects NICo service metrics without redeploying the
manual application resources. Grafana provisions Prometheus as a data source
alongside Loki.

Open **Explore** in Grafana, select the Prometheus data source, and start with:

```promql
up{namespace="nico-system"}
```

Prometheus stores metrics on a 2 GiB persistent volume and retains them for 24
hours. Its UI at `http://localhost:19090` also shows discovered endpoints under
**Status > Targets**.

## Dashboards

Tilt provisions the checked-in **NICo Core Health**, **NICo / Site Overview**,
**NICo / Site Explorer**, **NICo / Object Lifecycle**, and **NICo / API
Performance** dashboards into the **NICo** Grafana folder. The Tilt-local Site
Explorer dashboard adapts the canonical Forge dashboard panels to the current
`carbide_*` metric names. Grafana's dashboard sidecar watches their labeled
ConfigMap, so changes to the dashboard JSON files are loaded without a manual
import or Grafana restart.

The Prometheus chart also publishes its standard Kubernetes dashboards for the
same sidecar to load. These cover cluster, namespace, workload, pod, node,
kubelet, API server, and persistent-volume metrics.

With the observability profile enabled, Tilt also enables PodMonitors for the
CloudNativePG operator and PostgreSQL cluster. The operator chart provisions the
**CloudNativePG** dashboard in Grafana's **PostgreSQL** folder, covering operator
and database metrics. Tilt places its dashboard ConfigMap in the observability
namespace and waits for Prometheus before installing the operator's PodMonitor.

If Tilt is already running, it reloads changes to the Tiltfile and values file
and updates the `cnpg-operator` and `nico-postgres` Helm releases automatically.
Enable the profile with `tilt args -- --observability=true` if needed. To retry
an update, use the resource's update button in Tilt or run:

```bash
tilt trigger cnpg-operator
tilt trigger nico-postgres
```

Grafana's sidecar loads the dashboard automatically. Allow time for the Helm
updates and a Prometheus scrape, then refresh Grafana; no manual dashboard
import is required.

All Tilt settings are in [`values.yaml`](values.yaml). NICo Core values are at
the document root, while the `tilt` section contains the prerequisite and REST
settings.

## Machine-a-tron BMC routing

Tilt runs machine-a-tron in controller mode. `mat-k8s-controller` polls the
machine-a-tron status endpoint and creates one Kubernetes Service per simulated
BMC, publishing the BMC address assigned by NICo as the Service's `externalIPs`.
NICo therefore connects directly to each simulated BMC instead of routing every
Redfish request through the shared machine-a-tron proxy.

The Tilt BMC underlay is `10.200.0.0/18`. BMC addresses are Service externalIPs,
which the apiserver neither allocates nor validates and for which kube-proxy
programs forwarding rules on every node, so this range must stay outside the
Kubernetes ServiceCIDR, the pod CIDR, the node network, and any network the
nodes or pods must otherwise reach. On Kind the first two default to
`10.96.0.0/16` and `10.244.0.0/16`. Refer to the chart's
[Requirements](../../../helm/charts/nico-machine-a-tron/README.md#requirements)
for the full contract.

The DPU OOB / switch NVOS underlay (`192.168.128.0/18`) has the same
constraint: the controller publishes each simulated NVLink switch's NVOS lease
as the externalIP of a `mat-nvos-*` Service, through which NICo reaches
machine-a-tron's hosted NMX-C mock on port 9370 and the switch's NVUE REST API
mock on port 443.

## Image builds

Tilt and `make images-dev` share the Core service recipes and build-context
ignore files in `dev/docker/Dockerfile.dev-services` and
`dev/docker/Dockerfile.dev-runtime`. The compiler-container Dockerfiles in
`dev/docker` are also shared with other build workflows. Tilt retains the
MAT controller recipe in this directory and uses the REST service Dockerfiles
under `rest-api`.

Both workflows select the native Linux AMD64 or ARM64 architecture from the
active Docker daemon. Other daemon platforms fail before builds start. The Docker
daemon and the kind cluster must use the same architecture.

For a standalone image build, use `make images-dev` from the repository root.
See [building one local development service](../../../docs/manuals/building_nico_containers.md#build-one-local-development-service)
for target selection, image names, prerequisites and the isolated DHCP image
runtime check. It builds into the selected Docker daemon without publishing
images or deploying Kubernetes resources.

All Tilt runtime images derive from one digest-pinned Debian Bookworm slim base
with CA certificates. Service images install their additional runtime packages;
the compiler toolchains remain in build stages. DHCP includes both Kea DHCPv4
and DHCPv6 servers, using the version in `dev/docker/kea.version` to match the
compiler container. Its hook library is installed at
`/usr/lib/kea/hooks/libdhcp.so` on both architectures. Tilt values select that
path for DHCPv4; the DHCPv6 chart already uses it directly.

`Dockerfile.dev-services` has separate targets for API, BMC proxy, DHCP,
machine-a-tron, SSH console, the MAT protocol gateway and PostgreSQL exporter.
Each target builds and packages its own binary (or DHCP hook library), sharing
locked Cargo dependency and target caches separated by architecture. The Rust
build stages use `line-tables-only` debug information for development and release
profiles, retaining source-line
backtraces without full debugger type and variable information. The API image
contains a copy of nico-admin-cli for convenience.

The MAT controller and its gateway also use separate images in Tilt. Other chart
users keep the combined controller image unless they set
`mat-k8s-controller.gateway.image.repository` or `.tag`; each empty field falls
back to the corresponding controller image field, and both containers use the
controller image pull policy. REST API images contain only the API service
binary, without the debugging CLI.

Each deployable image has its own Tilt resource and rebuild control. Images build
once during startup, then wait for their resource's update button before
rebuilding. REST build contexts are restricted to the source directories copied
by that image's Dockerfile. Docker and Cargo or Go build caches are reused across
rebuilds. Repeated changed-source builds still retain historical layers until
Docker, Tilt, or the cluster removes them; this layout does not set storage
retention policies.

## Optional profiles

The `tilt.profiles` switches in [`values.yaml`](values.yaml) control REST, MCP,
DSX Exchange, and observability. REST brings Temporal and Keycloak, MCP
automatically brings REST, and DSX Exchange brings NATS. Observability manages
Loki, Alloy, Prometheus, Grafana, and the NICo metrics monitors as one unit. All
optional profiles are disabled by default.

Changes to `values.yaml` reload the running Tilt session. For temporary
overrides, use `tilt args`. Start only Core with:

```bash
tilt args -- --rest=false --mcp=false
```

Run REST without MCP with:

```bash
tilt args -- --rest=true --mcp=false
```

Add DSX Exchange with:

```bash
tilt args -- --dsx-exchange=true
```

Enable the complete observability stack with:

```bash
tilt args -- --observability=true
```

Return to the values-file defaults with:

```bash
tilt args --clear
```

## Existing local clusters

An earlier draft of this Tilt setup deployed Core as one Helm release named
`nico`. Remove that release once before starting the component-based setup:

```bash
helm uninstall nico -n nico-system
```

## Stop and remove

Stopping `tilt up` leaves the stack running. Remove Tilt-managed resources with:

```bash
tilt down -f dev/deployment/tilt/Tiltfile
```
