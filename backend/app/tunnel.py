import asyncio
import socket
import subprocess

from .config import Settings


class Tunnel:
    """Own only the child we started; never terminate an arbitrary port owner."""

    def __init__(self):
        self.process = None
        self.signature = None
        self.lock = asyncio.Lock()

    async def ensure(self, cfg: Settings):
        if not cfg.ssh_enabled:
            return
        async with self.lock:
            signature = (
                cfg.ssh_host,
                cfg.ssh_port,
                cfg.ssh_remote_host,
                cfg.ssh_remote_port,
                cfg.ssh_local_port,
            )
            if self.process and self.process.poll() is None and signature == self.signature:
                return
            await self.close()
            if not cfg.ssh_host:
                raise ValueError("请填写 SSH 用户及主机")
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", cfg.ssh_local_port)) == 0:
                    raise ValueError(f"SSH 本地端口 {cfg.ssh_local_port} 已被占用，请更换端口")
            command = [
                "ssh",
                "-N",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                "-p",
                str(cfg.ssh_port),
                "-L",
                f"127.0.0.1:{cfg.ssh_local_port}:{cfg.ssh_remote_host}:{cfg.ssh_remote_port}",
                cfg.ssh_host,
            ]
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.signature = signature
            for _ in range(60):
                if self.process.poll() is not None:
                    raise ValueError("SSH 隧道失败：检查密钥登录、known_hosts、主机和端口")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", cfg.ssh_local_port)
                    writer.close()
                    await writer.wait_closed()
                    return
                except OSError:
                    await asyncio.sleep(0.2)
            await self.close()
            raise ValueError("SSH 隧道启动超时")

    async def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.signature = None
