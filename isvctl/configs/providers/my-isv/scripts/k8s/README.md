<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes networking probes

For managed control-plane and node-pool upgrade evidence, see
[Kubernetes upgrade checks](../../../../../../docs/references/kubernetes-upgrades.md).

`networking_probe.py` supplies provider-neutral runtime evidence for `K8S29-01`,
`K8S30-01`, and `K8S31-01`. The validators grade that evidence independently.
The probes use the current kubeconfig, or the command prefix selected by `KUBECTL`.
They do not change the caller's kubeconfig or provision a cluster.

Copy [networking-example.json](networking-example.json), replace the provider
settings and ranges with those used for your test cluster, and set an absolute path.
The `k8s_*_test` steps in
[my-isv/config/k8s.yaml](../../config/k8s.yaml) run the probes:

```bash
export K8S_NETWORKING_CONFIG=/absolute/path/networking.json
uv run isvctl test run -f isvctl/configs/providers/my-isv/config/k8s.yaml --phase test -- \
  -k 'K8sLoadBalancerCheck or K8sDnsForwardingCheck or K8sCidrRangesCheck'
```

`--phase test` selects lifecycle steps; `-k` filters validations only. The config
sets `skip: true` on the IAM, GPU reset, and node cordon steps so this command runs
only the networking probes. To enable those other steps, follow their config
comments and remove `skip: true`; restore it for networking-only runs.

To collect one probe's JSON directly:

```bash
uv run python isvctl/configs/providers/my-isv/scripts/k8s/networking_probe.py \
  --check=dns_forwarding --config=/absolute/path/networking.json
```

A missing section skips that check. An empty `dns_forwarding` object opts in using
the default CoreDNS settings. An unreadable/malformed file, failed API call, timeout,
or failed cleanup is a failure. By default no probe runs without explicit configuration.
A direct probe emits observations; run its validator (as the suite does) to grade
mismatched answers, IP ranges and HTTP bodies. Exit code zero alone is not proof of
conformance.

## LoadBalancer services: K8S29-01

The probe creates a backend with a unique response, a private-network client pod,
and three Services: public, private, and static-public-IP. It resolves every
reported ingress hostname/IP and contacts each address. Public/static HTTP requests
originate from the controller, so run the controller on an external network to
exercise the public access path. Private HTTP requests originate from the client pod.
Each response must equal the unique backend value. Public IPs must be globally
routable; private IPs must be RFC1918 or IPv6 ULA. The static Service's observed
addresses must exactly match `static_ips`; ignored annotations cannot pass.

The example uses an **already installed AWS Load Balancer Controller**, suitable
subnets, and pre-reserved EIPs. Replace annotations/`loadBalancerClass` for your
provider. `loadBalancerIP` is also accepted for controllers that still support it.
Supply one EIP allocation and matching expected address per selected static subnet.
The probe does not allocate/release your reserved IPs or install a cloud controller.
See the controller's [Service annotation reference](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/).

## Conditional forwarding: K8S30-01

For standard CoreDNS, this check temporarily edits the selected Corefile ConfigMap.
Use a test cluster and an identity allowed to read and patch that ConfigMap. The
Corefile must have the `reload` plugin enabled. The probe adds a unique `.invalid`
zone forwarding to a disposable upstream resolver, then resolves its unique record
from a pod using cluster DNS. It checks the answer, the upstream query log, and a
normal `kubernetes.default.svc` lookup. Cleanup removes only the exact block added
by this run and refuses to overwrite a recreated ConfigMap or concurrent edits.

For OpenShift, use [networking-openshift-example.json](networking-openshift-example.json)
and set `backend` to `openshift_operator`. The probe adds a temporary forwarding
entry through `dns.operator.openshift.io/default`, waits for the DNS Operator to
publish the generated Corefile and complete its DaemonSet rollout, then restores the
original `spec.servers` list. Set `allow_operator_rollout` to `true` only when
running against a test cluster where a cluster DNS rollout is acceptable. This
requires permission to update the cluster-scoped DNS Operator resource, and the
forwarding change can briefly affect DNS service while its pods roll. Restoration
failure or a concurrent change to `spec.servers` fails the check rather than
overwriting another administrator's change.

Copy the OpenShift example to a private working path, change
`allow_operator_rollout` to `true` only after confirming this is a disposable test
cluster, and run just the DNS validation:

```bash
cp isvctl/configs/providers/my-isv/scripts/k8s/networking-openshift-example.json /tmp/networking-openshift.json
# Edit /tmp/networking-openshift.json and explicitly set allow_operator_rollout to true.
export K8S_NETWORKING_CONFIG=/tmp/networking-openshift.json
uv run isvctl test run -f isvctl/configs/providers/my-isv/config/k8s.yaml --phase test -- \
  -k K8sDnsForwardingCheck
```

The example includes no LoadBalancer or CIDR probe settings, so those steps skip.
The DNS probe also skips until the explicit rollout opt-in is enabled.

Both backends test conditional forwarding with a controlled resolver; they do not
certify reachability of every enterprise DNS server. See CoreDNS
[forward](https://coredns.io/plugins/forward/) and the OpenShift
[DNS Operator guide](https://docs.openshift.com/container-platform/latest/networking/networking_operators/dns-operator.html).

## Configured ranges: K8S31-01

Supply the service, node and pod CIDRs used when provisioning the cluster. The probe
reads the live ServiceCIDR API and node `podCIDRs`, then creates a new pod and
ClusterIP Service. The validator compares configured service ranges exactly,
checks node pod-CIDR containment, and verifies the node, new pod and new Service
addresses are within the supplied ranges. IPv4 and IPv6 are supported.

This is **partial coverage** of configurable ranges: it verifies a preconfigured
cluster and fresh allocations, but does not reprovision alternative configurations
or read provider-specific node subnet APIs. To test another range choice, provision
a disposable cluster with those ranges and run again. A CNI such as default AWS VPC
CNI that does not publish node `podCIDRs` needs a provider-specific probe; this check
skips rather than inferring a configured range from one observed address.
The ServiceCIDR API must be enabled; see [Service IP ranges](https://kubernetes.io/docs/tasks/network/extend-service-ip-ranges/).

For OpenShift OVNKubernetes, set `backend` to `openshift_ovn`. This reads the
configured service and cluster networks from `Network.config.openshift.io/cluster`
and per-node pod subnets from the `k8s.ovn.org/node-subnets` annotation, then
creates a temporary pod and Service to verify fresh allocations. The
`networking-openshift-example.json` contains the ranges from the example BareMetal
cluster; replace them if your cluster uses different ranges. Run it with:

```bash
cp isvctl/configs/providers/my-isv/scripts/k8s/networking-openshift-example.json /tmp/networking-openshift.json
export K8S_NETWORKING_CONFIG=/tmp/networking-openshift.json
uv run isvctl test run -f isvctl/configs/providers/my-isv/config/k8s.yaml --phase test -- \
  -k K8sCidrRangesCheck
```

The controller needs read access to the cluster Network resource, nodes and
services, plus permission to create and delete the temporary namespace, pod and
Service. The DNS forwarding probe in this example remains disabled unless its
separate operator-rollout opt-in is enabled.

## Fixtures and cleanup

The controller needs `kubectl`, Python 3.12, Kubernetes permissions to create/delete
namespaces, pods, ConfigMaps and Services, pod exec/log access, and read access to
nodes/ServiceCIDRs for the range check. Pods use `python:3.12-alpine`; the temporary
DNS resolver uses `registry.k8s.io/coredns/coredns:v1.12.3`. Set `python_image` or
`coredns_image` within each check's settings for approved mirrors.

Resources use a unique `isv-net-*` namespace. Cleanup waits for namespace deletion
(including Service finalizers) and reports failures. CoreDNS restoration runs before
namespace deletion, even after lookup failures. Hard process termination or lost
cluster access can interrupt cleanup: inspect the reported namespace and CoreDNS
zone before retrying. Normal cleanup never deletes a preexisting namespace after a
creation conflict.
