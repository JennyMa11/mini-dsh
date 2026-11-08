import hashlib
import json
from pathlib import Path

root = Path("benchmark-results/trainable_agent")
a = root / "core-analysis-03.json"
full = json.loads(a.read_text())
summary = {
    k: full[k] for k in ("schema_version", "training", "evaluation", "budget", "limitations")
}
summary["source"] = "derived from local core-analysis-03.json, no new model runs"
summary["full_analysis_sha256"] = hashlib.sha256(a.read_bytes()).hexdigest()
summary["method"] = {
    "success": "independent lightweight_v2 verifier",
    "test_attempts_per_policy": 32,
    "test_cases": 16,
    "inference_repeats": 2,
    "training_seeds": [17],
    "ci": "family-cluster bootstrap, 10000 draws, seed20261106, nearest-rank",
    "cache": "unavailable, derived metrics null",
}
summary["evidence"] = {
    str(p): {
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        "content": json.loads(p.read_text()),
    }
    for p in [
        root / "core-protocol-v2/training-audit.json",
        root / "core-artifacts-01/result.json",
        root / "security-gates-final/result.json",
    ]
}
summary["billing"] = {
    "posted_workspace_model_tokens_cny": "3.37",
    "observed_local": "2026-11-06 approximately 22:03 Asia/Shanghai",
    "final_invoice": False,
    "scope": "workspace-level current hour may update; storage not finally reconciled",
}
Path("docs/results/trainable-agent-core-2026-11-06.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
)
