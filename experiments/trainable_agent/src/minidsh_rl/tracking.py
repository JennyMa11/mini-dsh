"""Local metrics first, optional SwanLab sink; no raw traces or arbitrary config."""

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol


class MetricSink(Protocol):
    def log(self, data: dict[str, float], *, step: int) -> None: ...


@dataclass(frozen=True, slots=True)
class RunSpec:
    """A closed public config schema; credentials, prompts and paths have no field."""

    name: str
    phase: Literal["component", "smoke", "historical_train", "historical_test", "development"]
    algorithm: Literal["gspo_multiturn", "grpo_centered", "base"]
    source_sha256: str = ""
    model: Literal["Qwen/Qwen3.5-4B"] = "Qwen/Qwen3.5-4B"
    rank: int = 16
    group_size: int = 4
    seed: int = 17
    learning_rate: float = 1e-5
    clip_low: float = 0.2
    clip_high: float = 0.2

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name):
            raise ValueError("invalid public run name")
        if self.source_sha256 and not re.fullmatch(r"[a-f0-9]{64}", self.source_sha256):
            raise ValueError("invalid source hash")


METRICS = frozenset(
    {
        "reward/mean",
        "rollout/completed",
        "rollout/declared",
        "rollout/zero_signal",
        "usage/total_tokens",
        "usage/group_tokens",
        "usage/stage_total_tokens",
        "budget/stage_upper_cny",
        "budget/cumulative_upper_cny",
        "train/updates",
        "gspo/loss",
        "gspo/ratio_mean",
        "gspo/ratio_min",
        "gspo/ratio_max",
        "gspo/active_clip_fraction",
        "gspo/trajectories",
        "gspo/datums",
        "gspo/assistant_tokens",
        "gspo/remote_scale",
        "gspo/paper_clip_fraction",
        "eval/successes",
        "eval/declared",
        "eval/verified",
        "eval/coverage",
        "eval/success_rate",
        "eval/total_tokens",
        "eval/tokens_per_success",
        "eval/mean_tool_calls",
        "eval/latency_p50_ms",
        "eval/latency_p95_ms",
        "eval/format_rejection_rate",
        "eval/delta_success_rate",
        "eval/delta_ci95_low",
        "eval/delta_ci95_high",
        "numeric/mask_passed",
        "numeric/remote_gradient_verified",
    }
)


class MetricRecorder:
    """A fresh immutable local JSONL, with a best-effort replaceable remote view."""

    def __init__(self, directory: Path, spec: RunSpec) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "public-config.json").open("x") as stream:
            json.dump(asdict(spec), stream, allow_nan=False, indent=2)
        self.directory, self.spec = directory, spec
        self.remote: MetricSink | None = None
        self.last_step = -1

    def connect(
        self,
        *,
        mode: Literal["offline", "online"],
        project: str = "mini-dsh-agent-rl",
        workspace: str | None = None,
    ) -> None:
        """Create a private project; suppress automatic terminal/environment capture."""
        import swanlab  # type: ignore[import-untyped]  # noqa: PLC0415 - optional SDK

        settings = swanlab.Settings(
            interactive=False,
            terminal=swanlab.Settings.Terminal(proxy_type="none"),
            probe=swanlab.Settings.Probe(
                hardware=False,
                runtime=False,
                requirements=False,
                conda=False,
                git=False,
                swanlab=False,
                monitor=False,
            ),
        )
        self.remote = swanlab.init(
            project=project,
            workspace=workspace,
            experiment_name=self.spec.name,
            group="mini-dsh-lab-migration",
            job_type=self.spec.phase,
            config=asdict(self.spec),
            public=False,
            mode=mode,
            settings=settings,
            logdir=str(self.directory / "swanlog"),
            on_online_error="offline",
        )

    def log(self, step: int, metrics: dict[str, float]) -> None:
        """Validate before any I/O; retain local evidence if the remote sink fails."""
        if type(step) is not int or step <= self.last_step:
            raise ValueError("metric steps must increase")
        if not metrics or any(
            key not in METRICS or type(value) not in (int, float) or not math.isfinite(value)
            for key, value in metrics.items()
        ):
            raise ValueError("only allowlisted finite numeric metrics are accepted")
        with (self.directory / "metrics.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "step": step,
                        "metrics": metrics,
                    },
                    allow_nan=False,
                )
                + "\n"
            )
        self.last_step = step
        if self.remote is not None:
            try:
                self.remote.log(metrics, step=step)
            except Exception as error:  # remote observability must not destroy local evidence
                (self.directory / "tracking-error.json").write_text(
                    json.dumps(
                        {
                            "step": step,
                            "error_type": type(error).__name__,
                        }
                    )
                )
                self.remote = None

    def finish(self) -> dict[str, str | None]:
        """Persist observed SDK mode/URL; offline data must never claim a cloud URL."""
        import swanlab  # noqa: PLC0415 - already imported optional SDK

        run = swanlab.get_run()
        mode = str(run.mode) if run else "local_jsonl"
        url = str(run.url) if run and mode == "online" else None
        swanlab.finish()
        result = {"mode": mode, "url": url}
        (self.directory / "tracking-result.json").write_text(json.dumps(result, indent=2))
        return result
