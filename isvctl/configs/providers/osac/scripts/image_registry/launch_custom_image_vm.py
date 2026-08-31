#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Launch a VM from a custom container disk image via OpenShift Virtualization.

Uses a containerDisk source to boot a VirtualMachine with cloud-init SSH
key injection. Exposes SSH via a NodePort Service so the test runner on
the hypervisor can reach the VM.

Outputs JSON consumed by VmBootedFromCustomImageCheck and
VmFromCustomImageReadyCheck:
  success, instance_id, public_ip, key_path, state, ssh_port, ssh_user
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
POLL_TIMEOUT = 300
POLL_INTERVAL = 10


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def generate_ssh_key(key_dir: str) -> str:
    key_path = os.path.join(key_dir, "id_ed25519")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"],
        check=True,
    )
    return key_path


def ensure_namespace(ns: str) -> bool:
    rc, _, _ = run_kubectl("get", "namespace", ns)
    if rc == 0:
        return True
    rc, _, err = run_kubectl("create", "namespace", ns)
    return rc == 0


def create_vm_manifest(
    vm_name: str, namespace: str, container_image: str,
    ssh_pub_key: str, ssh_user: str,
) -> dict:
    cloud_init = (
        "#cloud-config\n"
        f"user: {ssh_user}\n"
        "ssh_authorized_keys:\n"
        f"  - {ssh_pub_key}\n"
    )

    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": vm_name,
            "namespace": namespace,
            "labels": {"app": "isv-image-registry-test"},
        },
        "spec": {
            "running": True,
            "template": {
                "metadata": {
                    "labels": {"app": "isv-image-registry-test", "vm.kubevirt.io/name": vm_name},
                },
                "spec": {
                    "domain": {
                        "cpu": {"cores": 1},
                        "devices": {
                            "disks": [
                                {"name": "containerdisk", "disk": {"bus": "virtio"}},
                                {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                            ],
                            "interfaces": [{"name": "default", "masquerade": {}}],
                        },
                        "resources": {"requests": {"memory": "1Gi"}},
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {
                            "name": "containerdisk",
                            "containerDisk": {"image": container_image},
                        },
                        {
                            "name": "cloudinitdisk",
                            "cloudInitNoCloud": {"userData": cloud_init},
                        },
                    ],
                },
            },
        },
    }


def create_nodeport_service(vm_name: str, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"{vm_name}-ssh",
            "namespace": namespace,
        },
        "spec": {
            "type": "NodePort",
            "selector": {"vm.kubevirt.io/name": vm_name},
            "ports": [{"port": 22, "targetPort": 22, "protocol": "TCP"}],
        },
    }


def wait_vm_running(vm_name: str, namespace: str) -> tuple[bool, str]:
    deadline = time.time() + POLL_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "vmi", vm_name, "-n", namespace,
            "-o", "jsonpath={.status.phase}",
        )
        if rc == 0 and out:
            last_status = out
            if out == "Running":
                return True, "Running"
        time.sleep(POLL_INTERVAL)
    return False, last_status


def get_node_ip_and_nodeport(vm_name: str, namespace: str) -> tuple[str, int]:
    rc, out, _ = run_kubectl(
        "get", "svc", f"{vm_name}-ssh", "-n", namespace,
        "-o", "jsonpath={.spec.ports[0].nodePort}",
    )
    nodeport = int(out) if rc == 0 and out.isdigit() else 0

    rc, out, _ = run_kubectl(
        "get", "nodes", "-l", "node-role.kubernetes.io/worker",
        "-o", "jsonpath={.items[0].status.addresses[?(@.type==\"InternalIP\")].address}",
    )
    node_ip = out if rc == 0 and out else ""
    return node_ip, nodeport


def wait_ssh_ready(
    host: str, port: int, user: str, key_path: str, timeout: int = 180,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                 "-o", "BatchMode=yes", "-i", key_path,
                 "-p", str(port), f"{user}@{host}", "echo SSH_OK"],
                capture_output=True, text=True, timeout=20,
            )
            if proc.returncode == 0 and "SSH_OK" in proc.stdout:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch VM from custom container disk image (OSAC)")
    parser.add_argument("--namespace", default="isv-image-test")
    parser.add_argument("--vm-name", default="isv-custom-image-vm")
    parser.add_argument("--container-image", default="quay.io/containerdisks/fedora:latest")
    parser.add_argument("--ssh-user", default="fedora")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "image_registry",
        "test_name": "launch_custom_image_vm",
        "instance_id": "",
        "public_ip": "",
        "key_path": "",
        "key_name": "",
        "state": "",
        "ssh_port": 22,
        "ssh_user": args.ssh_user,
    }

    if DEMO_MODE:
        result.update({
            "success": True,
            "instance_id": "demo-custom-image-vm",
            "public_ip": "10.128.0.99",
            "key_path": "/tmp/demo-key",
            "key_name": "demo-key",
            "state": "running",
        })
        print(json.dumps(result, indent=2))
        return 0

    try:
        key_dir = tempfile.mkdtemp(prefix="isv-imgtest-")
        key_path = generate_ssh_key(key_dir)
        pub_key_path = f"{key_path}.pub"
        with open(pub_key_path) as f:
            ssh_pub_key = f.read().strip()

        result["key_path"] = key_path
        result["key_name"] = "isv-image-test-key"

        if not ensure_namespace(args.namespace):
            result["error"] = f"Failed to create namespace {args.namespace}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write(f"Using container disk image: {args.container_image}\n")

        manifest = create_vm_manifest(
            args.vm_name, args.namespace, args.container_image,
            ssh_pub_key, args.ssh_user,
        )

        rc, _, _ = run_kubectl("get", "vm", args.vm_name, "-n", args.namespace)
        if rc == 0:
            sys.stderr.write(f"VM {args.vm_name} already exists, deleting...\n")
            run_kubectl("delete", "vm", args.vm_name, "-n", args.namespace,
                        "--wait=true", timeout=120)
            run_kubectl("delete", "svc", f"{args.vm_name}-ssh", "-n", args.namespace,
                        "--ignore-not-found=true", timeout=30)
            time.sleep(5)

        manifest_path = os.path.join(key_dir, "vm.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        rc, out, err = run_kubectl("apply", "-f", manifest_path)
        if rc != 0:
            result["error"] = f"Failed to create VM: {err}"
            print(json.dumps(result, indent=2))
            return 1

        result["instance_id"] = args.vm_name

        svc_manifest = create_nodeport_service(args.vm_name, args.namespace)
        svc_path = os.path.join(key_dir, "svc.json")
        with open(svc_path, "w") as f:
            json.dump(svc_manifest, f)
        rc, _, err = run_kubectl("apply", "-f", svc_path)
        if rc != 0:
            sys.stderr.write(f"Warning: failed to create NodePort service: {err}\n")

        sys.stderr.write(f"VM {args.vm_name} created, waiting for Running...\n")

        running, phase = wait_vm_running(args.vm_name, args.namespace)
        if not running:
            result["state"] = phase.lower() if phase else "unknown"
            result["error"] = f"VM did not reach Running within {POLL_TIMEOUT}s (last phase: {phase})"
            print(json.dumps(result, indent=2))
            return 1

        node_ip, nodeport = get_node_ip_and_nodeport(args.vm_name, args.namespace)
        if not node_ip or not nodeport:
            result["error"] = "Could not determine NodePort access"
            print(json.dumps(result, indent=2))
            return 1

        result["public_ip"] = node_ip
        result["ssh_port"] = nodeport
        sys.stderr.write(f"VM accessible at {node_ip}:{nodeport}\n")
        sys.stderr.write("Waiting for SSH to become ready...\n")
        ssh_ok = wait_ssh_ready(node_ip, nodeport, args.ssh_user, key_path)
        if not ssh_ok:
            result["state"] = "running"
            result["error"] = f"SSH did not become ready within timeout at {node_ip}:{nodeport}"
            print(json.dumps(result, indent=2))
            return 1

        result["state"] = "running"
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
