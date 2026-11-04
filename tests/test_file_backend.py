"""Container file-worker confinement and shared backend contracts."""

import shutil
from pathlib import Path

import pytest

from minidsh.adapters.file_backend import DockerFileBackend
from minidsh.domain import JsonValue
from minidsh.infrastructure import CommandPolicy, LocalCommandRunner, WorkspaceSandbox
from minidsh.infrastructure.file_worker import dispatch
from minidsh.tools.builtin import PatchFileInput, PatchFileTool
from minidsh.tools.runtime import ToolError


def test_worker_patch_and_symbol_search_share_workspace(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def add(a, b):\n    return a - b\n")
    result = dispatch(
        tmp_path, "patch_file", {"path": "a.py", "expected": "a - b", "replacement": "a + b"}
    )
    assert result["path"] == "a.py"
    symbols = dispatch(tmp_path, "find_symbol", {"name": "add"})
    assert symbols["symbols"][0]["name"] == "add"
    assert "a + b" in (tmp_path / "a.py").read_text()


def test_worker_rejects_sensitive_and_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret=hidden")
    with pytest.raises(ValueError, match="sensitive"):
        dispatch(tmp_path, "read_file", {"path": ".env"})
    with pytest.raises(ValueError, match="not model-writable"):
        dispatch(
            tmp_path,
            "prepare",
            {
                "tool": "write_file",
                "arguments": {"path": ".minidsh/artifacts/result", "content": "bad"},
            },
        )
    outside = tmp_path.parent / "outside-file"
    outside.write_text("outside")
    (tmp_path / "alias").symlink_to(outside)
    with pytest.raises(ValueError, match="escape"):
        dispatch(tmp_path, "read_file", {"path": "alias"})


@pytest.mark.asyncio
@pytest.mark.parametrize("occurrence", [1, 2])
async def test_local_and_worker_patch_previews_match_applied_occurrence(
    tmp_path: Path, occurrence: int
) -> None:
    original = "first=x\nsecond=x\n"
    (tmp_path / "a.py").write_text(original)
    tool = PatchFileTool(WorkspaceSandbox(tmp_path))
    args = PatchFileInput(path="a.py", expected="x", replacement="y", occurrence=occurrence)
    _, preview = await tool.prepare(args)
    worker = dispatch(tmp_path, "prepare", {"tool": "patch_file", "arguments": args.model_dump()})
    assert preview == worker["preview"]
    assert (tmp_path / "a.py").read_text() == original
    await tool.execute(args)
    updated = (tmp_path / "a.py").read_text()
    assert f"+{'first' if occurrence == 1 else 'second'}=y" in preview
    (tmp_path / "a.py").write_text(original)
    dispatch(tmp_path, "patch_file", args.model_dump())
    assert (tmp_path / "a.py").read_text() == updated


@pytest.mark.integration
@pytest.mark.asyncio
async def test_node_worker_preserves_file_and_path_contracts_without_python(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node interpreter is not installed")
    runner = LocalCommandRunner(
        WorkspaceSandbox(tmp_path), CommandPolicy(allowed_commands=frozenset({"node"}))
    )
    backend = DockerFileBackend(runner, worker_runtime="node", worker_root=str(tmp_path))
    (tmp_path / "solution.js").write_text("function add(a,b) { return a-b; }\n")
    arguments: dict[str, JsonValue] = {
        "path": "solution.js",
        "expected": "a-b",
        "replacement": "a+b",
    }
    scope, preview = await backend.prepare("patch_file", arguments)
    assert scope == str(tmp_path / "solution.js")
    assert "a-b" in preview
    assert "a+b" in preview
    result = await backend.invoke("patch_file", arguments)
    assert isinstance(result, dict)
    assert result["occurrence"] == 1
    read = await backend.invoke("read_file", {"path": "solution.js"})
    assert isinstance(read, dict)
    assert "return a+b" in str(read["content"])
    found = await backend.invoke("search", {"query": "a+b", "glob": "solution.[jt]s"})
    assert isinstance(found, dict)
    assert found["matches"] == [
        {"path": "solution.js", "line": 1, "text": "function add(a,b) { return a+b; }"}
    ]
    (tmp_path / ".env").write_text("fixture_secret_must_not_escape")
    outside = tmp_path.parent / "outside-node.txt"
    outside.write_text("outside")
    (tmp_path / "escape").symlink_to(outside)
    for name in (".env", "escape", "../outside-node.txt", str(outside)):
        with pytest.raises(ToolError, match="file_backend_denied"):
            await backend.invoke("read_file", {"path": name})
    with pytest.raises(ToolError, match="file_exists"):
        await backend.invoke("write_file", {"path": "solution.js", "content": "unsafe overwrite"})
