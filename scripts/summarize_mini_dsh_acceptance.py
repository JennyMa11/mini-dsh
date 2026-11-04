"""Derive a small, shareable report without changing the frozen acceptance evidence."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "benchmark-results/mini-dsh/v1-20261013"
POSTFIX = ROOT / "benchmark-results/mini-dsh/final-20261013"
DESTINATION = ROOT / "docs/results/mini-dsh-runtime-2026-10-13.json"


def main() -> None:
    """Copy evidence-backed counts and identify the independently checked later version."""
    report = json.loads((LIVE / "report.json").read_text())
    original = json.loads((LIVE / "gates.json").read_text())
    current = json.loads((POSTFIX / "gates.json").read_text())
    inputs = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            LIVE / "report.json",
            LIVE / "gates.json",
            LIVE / "kernel.json",
            POSTFIX / "gates.json",
            POSTFIX / "kernel.json",
            POSTFIX / "recovery_contract/verification.json",
            POSTFIX / "gui_smoke_verification.json",
        )
    }
    derived = {
        "generated_by": "uv run python scripts/summarize_mini_dsh_acceptance.py",
        "schema_version": "1.0",
        "source_hashes": inputs,
        "real_model_cohort": report,
        "component_gate": original["component_passed"],
        "kernel_gate": original["kernel_passed"],
        "postfix_component_gate": current["component_passed"],
        "postfix_kernel_gate": current["kernel_passed"],
        "postfix_recovery_contract": json.loads(
            (POSTFIX / "recovery_contract/verification.json").read_text()
        ),
        "gui_smoke": json.loads((POSTFIX / "gui_smoke_verification.json").read_text()),
        "changed_since_real_cohort": sorted(
            key
            for key, value in original["input_hashes"].items()
            if current["input_hashes"].get(key) != value
        ),
        "final_version_real_cohort_retested": False,
    }
    DESTINATION.write_text(json.dumps(derived, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
