# Lab IPv6 provisioning DNS

This standalone chart provides DNS for the optional native BMC lab. It is not
included in the production NICo chart. It requires a cluster with IPv6 Services,
a working cluster DNS resolver, and IPv6 companion Services in its namespace.
Tilt's `--ipv6-endpoints` profile creates those companions before this release.

The release exposes one IPv6-only ClusterIP with UDP and TCP port 53. It rewrites
these exact compatibility names, which remain in the boot artifacts:

| Query name | Default destination in the release namespace |
| --- | --- |
| `carbide-api.forge` | `nico-api-external.<namespace>.svc.cluster.local` |
| `carbide-pxe.forge` | `nico-pxe-external.<namespace>.svc.cluster.local` |
| `carbide-static-pxe.forge` | `nico-pxe-external.<namespace>.svc.cluster.local` |

AAAA queries return the companion Service addresses; A queries have no address
when those Services are IPv6-only. Other queries are forwarded through the
pod's `/etc/resolv.conf`, including public names. External DNS resolution and
Internet routing depend on the surrounding lab. This resolver does not create
routes, DHCP options, BMC addresses or boot entries.

`apiService` and `pxeService` optionally override the destination Service names;
`clusterDomain` defaults to `cluster.local`. The schema rejects malformed DNS
names. `image` must contain an immutable SHA-256 digest; the default is a shared
ARM64/AMD64 CoreDNS manifest. `resources` accepts Kubernetes resource requests
and limits; defaults are 10m CPU and 32Mi memory requested, with a 128Mi memory
limit. The chart uses no Kubernetes credentials. CoreDNS runs as a non-root
user with only `NET_BIND_SERVICE`, needed by the pinned binary's file capability.

Helm owns the resolver ConfigMap, Deployment and Service. ConfigMap changes
roll out the pod; an unchanged upgrade retains it. Deleting and reinstalling
the release can allocate a different ClusterIP. Update the DHCPv6 DNS option
before booting guests after that operation. Existing clients may need a network
reconfiguration or reboot to adopt a changed resolver. Preserve the release
through target provisioning and release so Scout can resolve its API endpoint.

See the [Tilt profile](../../dev/deployment/tilt/README.md#ipv6-provisioning-endpoints)
for integration. IPv6 DNS and HTTP/TLS reachability are prerequisites; they do
not by themselves validate IPv6 firmware boot or full lifecycle operation.
