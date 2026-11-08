"""Declare CLI commands, options and defaults without selecting or running a model."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Final

from minidsh import __version__
from minidsh.adapters import BUILTIN_PROFILES
from minidsh.config import DEFAULT_CONFIG_NAME

_CONTEXT_STRATEGIES: Final = (
    "budget",
    "full",
    "sliding",
    "summary",
    "retrieval",
    "retrieval_summary",
    "symbol",
)
_PROVIDERS: Final = ("scripted", "openai-compatible", "mlx")


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - CLI command declarations
    """Build the command grammar with the established CLI defaults and permission flags."""
    parser = argparse.ArgumentParser(
        prog="mini-dsh",
        description="A lightweight coding-agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="validate the local runtime environment")

    run = subparsers.add_parser("run", help="run one coding-agent task")
    _add_common_options(run)
    _add_model_options(run)
    run.add_argument("task", help="task given to the coding agent")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--trace", type=Path, help="JSONL trace path")

    chat = subparsers.add_parser("chat", help="start an interactive coding-agent session")
    _add_common_options(chat)
    _add_model_options(chat)
    chat.set_defaults(provider="openai-compatible", allow_unpriced=True)
    chat.add_argument("--workspace", type=Path, default=Path.cwd())
    chat.add_argument("--resume", help="continue a saved run ID in this workspace")

    work = subparsers.add_parser(
        "work", help="edit workspace files and run a configured test command interactively"
    )
    _add_common_options(work)
    _add_model_options(work)
    work.set_defaults(provider="openai-compatible", allow_unpriced=True)
    work.add_argument("--workspace", type=Path, default=Path.cwd())
    work.add_argument("--resume", help="continue a saved run ID in this workspace")
    work.add_argument("--session", help="continue a stable conversation ID")
    chat.add_argument("--session", help="continue a stable conversation ID")

    gui = subparsers.add_parser("gui", help="open the local browser interface")
    _add_common_options(gui)
    _add_model_options(gui)
    gui.set_defaults(provider="openai-compatible", allow_unpriced=True)
    gui.add_argument("--workspace", type=Path, default=Path.cwd())
    gui.add_argument("--resume", help="continue a saved run ID")
    gui.add_argument("--session", help="continue a stable conversation ID")
    gui.add_argument("--port", type=int, default=0, help="loopback port; default: automatic")
    gui.add_argument("--no-open", action="store_true", help="print URL without opening a browser")

    benchmark = subparsers.add_parser("benchmark", help="run a benchmark manifest")
    _add_common_options(benchmark)
    _add_model_options(benchmark)
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--output", type=Path, default=Path("benchmark-results/report.json"))
    benchmark.add_argument("--markdown", type=Path, help="optional Markdown report path")
    benchmark.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every task this many times to expose run-to-run spread",
    )
    _add_verification_option(benchmark)

    ablation = subparsers.add_parser("ablation", help="run a single-variable ablation suite")
    _add_common_options(ablation)
    _add_model_options(ablation)
    ablation.add_argument("manifest", type=Path)
    ablation.add_argument("--output", type=Path, default=Path("benchmark-results/ablation.json"))
    ablation.add_argument(
        "--suite",
        default="standard",
        help="variant suite: standard, context, tool_output, permission, local_first",
    )
    ablation.add_argument("--repeat", type=int, default=1)
    _add_verification_option(ablation)

    matrix = subparsers.add_parser(
        "matrix", help="compare 4B/8B across baseline and local-first harness"
    )
    _add_common_options(matrix)
    matrix.add_argument("manifest", type=Path)
    matrix.add_argument("--model-4b", type=Path, required=True, help="pinned local MLX snapshot")
    matrix.add_argument("--model-8b", type=Path, required=True, help="pinned local MLX snapshot")
    matrix.add_argument("--output", type=Path, default=Path("benchmark-results/local-matrix.json"))
    matrix.add_argument("--repeat", type=int, default=3)
    _add_verification_option(matrix)

    formal = subparsers.add_parser(
        "formal-study", help="resume the frozen 26-task local MLX matrix and ablation"
    )
    formal.add_argument("manifest", type=Path)
    formal.add_argument("--model-4b", type=Path, required=True)
    formal.add_argument("--model-8b", type=Path, required=True)
    formal.add_argument(
        "--output-dir", type=Path, default=Path("benchmark-results/local-formal-v1")
    )
    formal.add_argument("--phase", choices=("matrix", "ablation", "all"), default="all")
    formal.add_argument("--repeat", type=int, default=3)

    study = subparsers.add_parser("harness-study", help="check or run the frozen harness pilot")
    study.add_argument("action", choices=("check", "run", "rescore"))
    study.add_argument(
        "--protocol", type=Path, default=Path("benchmarks/harness_study/v1/protocol.json")
    )
    study.add_argument(
        "--sources", type=Path, default=Path("benchmark-results/public-lite/sources")
    )
    study.add_argument(
        "--output-dir", type=Path, default=Path("benchmark-results/harness-study/v1")
    )
    study.add_argument(
        "--image-lock",
        type=Path,
        default=Path("benchmark-results/public-lite/baseline/images.json"),
    )
    study.add_argument("--credentials-file", type=Path)
    study.add_argument("--rescore-output", type=Path)

    report = subparsers.add_parser("report", help="render a saved report with trade-off analysis")
    report.add_argument("path", type=Path, help="a benchmark or ablation JSON report")
    report.add_argument("--markdown", type=Path, help="write Markdown here instead of stdout")

    trace = subparsers.add_parser("trace", help="replay or diff recorded JSONL traces")
    trace_commands = trace.add_subparsers(dest="trace_command", required=True)
    trace_replay = trace_commands.add_parser("replay", help="reconstruct a run step by step")
    trace_replay.add_argument("path", type=Path)
    trace_replay.add_argument("--json", action="store_true", dest="as_json")
    trace_replay.add_argument("--preview-chars", type=int, default=120)
    trace_replay.add_argument("--run-id", help="inspect one Sub Agent run in a team trace")
    trace_metrics = trace_commands.add_parser(
        "metrics", help="score saved harness trajectory evidence"
    )
    trace_metrics.add_argument("path", type=Path)
    trace_metrics.add_argument("--run-id", help="select one run from a trace")
    trace_metrics.add_argument(
        "--evidence", type=Path, help="independent run-bound scoring sidecar"
    )
    trace_diff = trace_commands.add_parser("diff", help="compare two traces of the same task")
    trace_diff.add_argument("left", type=Path)
    trace_diff.add_argument("right", type=Path)
    trace_diff.add_argument("--left-label")
    trace_diff.add_argument("--right-label")
    trace_diff.add_argument("--json", action="store_true", dest="as_json")

    config = subparsers.add_parser("config", help="print the effective layered configuration")
    _add_common_options(config)
    config.add_argument("--workspace", type=Path, default=Path.cwd())
    return parser


def _add_verification_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verify-command",
        action="append",
        default=[],
        help="allow one success-criterion executable; repeat as needed",
    )


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help=f"configuration file; defaults to ./{DEFAULT_CONFIG_NAME} when present",
    )
    parser.add_argument("--context", choices=_CONTEXT_STRATEGIES)
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--approval-mode", choices=("ask", "configured"))
    parser.add_argument("--trust-plugin", action="append", default=[], metavar="PLUGIN_ID")
    parser.add_argument("--plugin", action="append", metavar="PLUGIN_ID")
    parser.add_argument("--context-input-budget", type=int)
    parser.add_argument("--local-first", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--progressive-tools", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--normalize-tool-results", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--error-recovery", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--profile", help="named runtime profile from [profiles.NAME]")
    parser.add_argument("--multi-agent", action=argparse.BooleanOptionalAction, default=None)
    for limit in ("agents", "parallel", "steps", "tokens", "tool-calls"):
        parser.add_argument(f"--subagent-max-{limit}", type=int)
    parser.add_argument("--subagent-max-seconds", type=float)
    parser.add_argument("--allow-write", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--allow-shell", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--sliding-window", type=int)
    parser.add_argument("--recent-messages", type=int)
    parser.add_argument("--system-prompt")
    parser.add_argument("--reflection-enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--detailed-tool-feedback", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--artifact-offload", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--tool-max-retries", type=int)
    parser.add_argument("--builtin-tools", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--extra-permission", action="append", metavar="CAPABILITY")
    parser.add_argument(
        "--enabled-tools",
        nargs="*",
        metavar="TOOL",
        help="expose only these tools; an empty list disables all tools",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="HOST[:PORT]",
        help="authorize one network destination; repeat as needed",
    )
    parser.add_argument("--sandbox-backend", choices=("local", "docker"))
    parser.add_argument("--docker-image")
    parser.add_argument(
        "--allow-command",
        action="append",
        default=None,
        metavar="EXECUTABLE",
        help="allow one executable for shell/test/build tools; repeat as needed",
    )
    parser.add_argument(
        "--deny-tool",
        action="append",
        default=None,
        metavar="TOOL",
        help="deny one tool even when its capability is granted; repeat as needed",
    )
    parser.add_argument(
        "--test-command", metavar="COMMAND", help="fixed test command, shell-split once"
    )
    parser.add_argument(
        "--build-command", metavar="COMMAND", help="fixed build command, shell-split once"
    )
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--tool-output-limit", type=int)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument(
        "--cached-input-usd-per-million",
        type=float,
        help="rate for prompt-cache hits; defaults to the uncached input rate",
    )


class _ProviderSelection(argparse.Action):
    """Distinguish an explicit provider choice from the backward-compatible default."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        namespace.provider_explicit = True


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subagent-script", type=Path, help="independent offline Sub Agent script")
    parser.add_argument(
        "--provider",
        choices=_PROVIDERS,
        default="scripted",
        action=_ProviderSelection,
    )
    parser.add_argument(
        "--model-profile",
        help=f"named provider profile: {', '.join(sorted(BUILTIN_PROFILES))}",
    )
    parser.add_argument(
        "--script", type=Path, help="JSON response script; required by the scripted provider"
    )
    parser.add_argument("--endpoint", help="Chat Completions endpoint; overrides the profile")
    parser.add_argument("--model", help="model name; overrides the profile")
    parser.add_argument("--mlx-max-output-tokens", type=int, default=1_024)
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="environment variable containing the provider API key; defaults to the profile's",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "provider chain-of-thought; defaults to the profile's setting. "
            "DeepSeek ignores temperature while thinking is on, so leaving it off "
            "is what makes temperature=0 mean anything"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="attempts per model call before a transient provider error is fatal",
    )
    parser.add_argument(
        "--allow-unpriced",
        action="store_true",
        help="permit a real provider with no declared rate card; cost is then not reportable",
    )
