"""Probe Linux namespaces and wrap explicitly requested shell execution."""

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

FLAGS = ("--user", "--map-root-user", "--mount", "--ipc", "--pid", "--uts", "--fork")


@lru_cache(maxsize=2)
def probe(network=False):
    if sys.platform != "linux" or not shutil.which("unshare"):
        return None
    for candidate in (FLAGS, (*FLAGS[:2], "--map-auto", *FLAGS[2:])):
        args = (*candidate, *(("--net",) if network else ()))
        try:
            result = subprocess.run(
                ["unshare", *args, "true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode == 0:
                return args
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def container_markers(env=None):
    env = os.environ if env is None else env
    markers = [
        str(path) for path in (Path("/.dockerenv"), Path("/run/.containerenv")) if path.exists()
    ]
    markers.extend(
        "env:" + key
        for key, value in env.items()
        if key.lower() in {"container", "docker", "podman", "kubernetes_service_host"} and value
    )
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        markers.extend(
            "/proc/1/cgroup:" + key
            for key in ("docker", "containerd", "kubepods", "podman", "libpod")
            if key in cgroup
        )
    except OSError:
        pass
    return sorted(set(markers))


def status():
    namespace, network = bool(probe()), bool(probe(True))
    markers = container_markers()
    return {
        "platform": sys.platform,
        "supported": namespace,
        "active": False,
        "namespace_supported": namespace,
        "network_supported": network,
        "filesystem_active": False,
        "filesystem_mode": "off",
        "in_container": bool(markers),
        "container_markers": markers,
        "fallback_reason": None
        if namespace
        else "namespace isolation unavailable (requires Linux with working unshare)",
        "note": "Namespace availability is a probe; filesystem containment is not enforced by unshare.",
    }


def wrap(argv, cwd, request):
    if request.get("filesystemMode", "off") != "off" or request.get("allowedMounts"):
        raise ValueError("没有 OS 沙箱文件系统隔离；不能用 unshare 环境变量冒充目录隔离")
    if not (request.get("namespaceRestrictions") or request.get("isolateNetwork")):
        return (
            argv,
            {},
            {"namespace_active": False, "network_active": False, "filesystem_active": False},
        )
    flags = probe(bool(request.get("isolateNetwork")))
    if not flags:
        raise ValueError("当前本机执行器没有 OS 沙箱：Linux unshare 探测未通过")
    home, temp = Path(cwd) / ".sandbox-home", Path(cwd) / ".sandbox-tmp"
    home.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    return (
        ["unshare", *flags, *argv],
        {"HOME": str(home), "TMPDIR": str(temp)},
        {
            "namespace_active": True,
            "network_active": bool(request.get("isolateNetwork")),
            "filesystem_active": False,
        },
    )
