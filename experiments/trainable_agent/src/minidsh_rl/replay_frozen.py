"""Replay offline v2 analysis from exact archived sources after subsequent code edits."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory


def materialize(training: Path, destination: Path) -> None:
    """Only regular files named in the bound source manifest may be materialized."""
    expected = json.loads((training / "source.json").read_text())
    seen: set[str] = set()
    with tarfile.open(training / "source.tar.gz") as archive:
        for member in archive.getmembers():
            name = member.name
            path = PurePosixPath(name)
            if (
                name not in expected
                or name in seen
                or not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
            ):
                raise ValueError("archive contains an unexpected or unsafe member")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("missing archive member data")
            data = stream.read()
            if hashlib.sha256(data).hexdigest() != expected[name]:
                raise ValueError("archive/source hash mismatch")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            seen.add(name)
    if seen != set(expected):
        raise ValueError("archive does not cover the complete source manifest")


def replay(training: Path, evaluation: Path, output: Path, artifact_root: Path) -> None:
    """No training or sampling: run the archived read-only analyzer in a fresh directory."""
    training, evaluation, output = training.resolve(), evaluation.resolve(), output.resolve()
    if output.exists():
        raise ValueError("analysis output already exists")
    with TemporaryDirectory(prefix="minidsh-frozen-analysis-") as temporary:
        workspace = Path(temporary)
        materialize(training, workspace)
        (workspace / "benchmark-results").symlink_to(
            artifact_root.resolve(), target_is_directory=True
        )
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
            if key in os.environ
        }
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(workspace / "experiments/trainable_agent/src"),
                str(workspace / "src"),
            ]
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "minidsh_rl.analyze_v2",
                "--training",
                str(training),
                "--evaluation",
                str(evaluation),
                "--output",
                str(output),
            ],
            cwd=workspace,
            env=environment,
            check=True,
            timeout=120,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()
    replay(args.training, args.evaluation, args.output, args.artifact_root)


if __name__ == "__main__":
    main()
