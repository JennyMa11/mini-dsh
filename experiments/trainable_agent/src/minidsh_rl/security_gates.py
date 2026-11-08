"""Real Docker side-effect checks, separate from simulated adapter contracts."""

import argparse
import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

from pytrio.config.config import auth_state  # type: ignore[import-untyped]

from minidsh.infrastructure.containers import ContainerResources, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner, ProcessRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox


async def absent(host: ProcessRunner, name: str) -> bool:
    result = await host.run(("docker", "inspect", name))
    return result.exit_code != 0


def _credential_scan(key: str) -> tuple[int, int]:
    paths = [
        p
        for p in Path("benchmark-results/trainable_agent").rglob("*")
        if p.suffix in {".json", ".jsonl", ".txt", ".py"} and p.is_file()
    ]
    return len(paths), sum(bool(key and key in p.read_text(errors="replace")) for p in paths)


async def gates(output: Path, image: str) -> None:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    host = LocalCommandRunner(WorkspaceSandbox(output), CommandPolicy(frozenset({"docker"})))
    results: dict[str, object] = {}
    with TemporaryDirectory(prefix="minidsh-secret-probe-") as temporary:
        sentinel = Path(temporary) / "private_sentinel"
        value = secrets.token_hex(32)
        await asyncio.to_thread(sentinel.write_text, value)
        digest = hashlib.sha256(value.encode()).hexdigest()
        async with DockerSandbox(
            host,
            image=image,
            resources=ContainerResources(
                platform="linux/arm64", cpus=1, memory_mb=256, pids_limit=64
            ),
        ) as box:
            inspected = await host.run(("docker", "inspect", box.name))
            info = json.loads(inspected.stdout)[0]
            results["no_host_mounts"] = not info["Mounts"]
            results["network_none"] = info["HostConfig"]["NetworkMode"] == "none"
            results["capabilities_dropped"] = info["HostConfig"]["CapDrop"] == ["ALL"]
            probe = await box.execute(
                (
                    "python3",
                    "-c",
                    "import json,os,socket,sys; from pathlib import Path; "
                    "s=socket.socket(); s.settimeout(.2); "
                    "r=s.connect_ex(('1.1.1.1',443)); "
                    "print(json.dumps({'host_file_hidden':not Path(sys.argv[1]).exists(),"
                    "'network_blocked':r!=0,'credential_env_absent':not any("
                    "'API_KEY' in k or 'TOKEN' in k for k in os.environ)}))",
                    str(sentinel),
                )
            )
            if probe.exit_code:
                raise ValueError("security probe failed to execute")
            results.update(json.loads(probe.stdout))
            name = box.name
        results["probe_container_removed"] = await absent(host, name)
        after = await asyncio.to_thread(sentinel.read_bytes)
        results["host_file_unchanged"] = hashlib.sha256(after).hexdigest() == digest
    for mode in ("timeout", "cancel"):
        async with DockerSandbox(
            host,
            image=image,
            resources=ContainerResources(
                platform="linux/arm64", cpus=1, memory_mb=256, pids_limit=64
            ),
        ) as box:
            name = box.name
            command = (
                "python3",
                "-c",
                (
                    "import subprocess,time; "
                    "subprocess.Popen(['python3','-c','import time;time.sleep(30)']);time.sleep(30)"
                ),
            )
            if mode == "timeout":
                result = await box.execute(command, timeout_seconds=0.2)
                results["timeout_reported"] = result.timed_out
            else:
                task = asyncio.create_task(box.execute(command, timeout_seconds=10))
                await asyncio.sleep(0.2)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    results["cancellation_propagated"] = True
        results[mode + "_children_removed"] = await absent(host, name)
    key = auth_state.api_key
    files, hits = await asyncio.to_thread(_credential_scan, key)
    results["credential_detector_configured"] = bool(key)
    results["credential_scan_files"] = files
    results["credential_scan_hits"] = hits
    results["passed"] = (
        all(
            v
            for k, v in results.items()
            if k not in {"credential_scan_files", "credential_scan_hits"}
        )
        and hits == 0
    )
    await asyncio.to_thread((output / "result.json").write_text, json.dumps(results, indent=2))
    if not results["passed"]:
        raise ValueError("critical sandbox/security gate failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    asyncio.run(gates(args.output.resolve(), args.image))


if __name__ == "__main__":
    main()
