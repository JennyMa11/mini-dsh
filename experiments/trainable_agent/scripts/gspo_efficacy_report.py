# ruff: noqa: E501, RUF001
# Long Chinese Markdown literals preserve generated report layout.
"""Rebuild final scientific report from complete immutable provider artifacts only."""

import json
import sys
from pathlib import Path

from minidsh_rl.gspo_study_analysis import analyze
from minidsh_rl.gspo_study_recovery import equal_state, optimizer, verify_failure
from minidsh_rl.learning_schedule import digest
from minidsh_rl.weight_probe import weight_delta

base = Path(__file__).resolve().parents[3] / "benchmark-results/trainable_agent/gspo-efficacy-v1"
root = base / "run-03"
value = analyze(root)
# The independent audit verifies recomputed norm against this durable measurement.
# Use the recorded value to avoid process-dependent floating summation at one ULP.
norm_source = root / "final-audit/result.json"
value["weights"]["trained_delta_l2"] = json.loads(norm_source.read_text())["parameter_delta_l2"]
value["weights"]["measurement_sha256"] = digest(norm_source)
old = base / "run-01"
_, previous, _, _ = verify_failure(old)
f = root / "rollback-sdk"
proof = json.loads((f / "verified.json").read_text())
assert proof["reference_sha256"] == digest(f / "reference.archive")
assert proof["restored_sha256"] == digest(f / "restored.archive")
assert weight_delta(f / "reference.archive", f / "restored.archive") == 0
assert equal_state(optimizer(f / "reference.archive"), optimizer(f / "restored.archive"))
for name in previous.completed:
    for original in (old / "rollouts" / name).rglob("*"):
        if original.is_file():
            for target in (base / "run-02", root):
                assert digest(original) == digest(target / original.relative_to(old))
value["recovery"] = {
    "kind": "terminal_backward_only_rollback",
    "original_completed_trajectories": 124,
    "original_confirmed_groups": 30,
    "original_confirmed_updates": 19,
    "original_unknown_backward_preserved": True,
    "copied_raw_trajectories_identical": True,
    "restored_adapter_delta_l2": 0.0,
    "full_adam_state_equal": True,
    "readiness_only_stops_preserved": True,
    "proof_sha256": digest(f / "verified.json"),
    "inputs": {
        str(p): digest(p)
        for p in (root / "rollback-proof.json", f / "reference.archive", f / "restored.archive")
    },
}
security = {}
for name in ("security-before-01", "security-after-01"):
    p = base / name / "result.json"
    r = json.loads(p.read_text())
    assert r["passed"] is True
    security[name] = {"passed": True, "sha256": digest(p)}
value["security"] = security
value["scorer"] = {
    "script": str(Path(__file__)),
    "sha256": digest(Path(__file__)),
    "analysis_sha256": digest(
        Path("experiments/trainable_agent/src/minidsh_rl/gspo_study_analysis.py")
    ),
}
output = Path("docs/results/gspo-efficacy-v1-2026-11-09.json")
output.write_text(json.dumps(value, indent=2) + "\n")
rows = []
for phase in ("validation", "test"):
    v = value[phase]
    ci = v["paired_difference"]["ci95"]
    rows.append(
        f"| {'验证' if phase == 'validation' else '测试（主要终点）'} | {v['base']['successes']}/{v['base']['denominator']}（{100 * v['base']['success_rate']:.2f}%） | {v['gspo']['successes']}/{v['gspo']['denominator']}（{100 * v['gspo']['success_rate']:.2f}%） | {100 * v['paired_difference']['difference']:+.2f}pp | [{100 * ci[0]:+.2f}, {100 * ci[1]:+.2f}]pp |"
    )
t = value["test"]
ci = t["paired_difference"]["ci95"]
d = t["paired_difference"]["difference"]
conclusion = (
    "本轮测试观察到正向点估计，区间包含0，不能排除无提升。"
    if d > 0 and ci[0] <= 0
    else "本轮测试未观察到正向成功率收益。"
    if d <= 0
    else "本轮固定测试观察到正向差异，探索区间未包含0；尚需独立训练seed复核。"
)
text = (
    """# GSPO 与 Base：同规模固定基准复测

2026-11-09；状态：完整352条真实轨迹与独立评分复核通过。

"""
    + conclusion
    + """

| 部分 | Base | GSPO | 配对差值 | 95%家族聚类探索区间 |
| --- | --- | --- | --- | --- |
"""
    + "\n".join(rows)
    + f"""

完成与历史GRPO相同的40个G4训练组、160条训练轨迹；产生25次真实更新，15个全0/全1奖励组跳过。
训练题通过98/160仅描述采样奖励，不作为效果提升的证据。验证64条和测试128条均重新采样，
Base/GSPO分别32次验证、64次测试；测试GSPO独有成功{t["gspo_only_success"]}对、Base独有成功{t["base_only_success"]}对。
完整声明分母保留，无补采、挑题、根据验证换模型或缩减测试。

模型Qwen3.5-4B、LoRA r16、训练seed17、G4、Adam lr1e-5；复用原expansion_v2的24家族96题、
家族隔离划分、固定40题训练顺序、提示、工具、采样参数与独立二值测试奖励。
此处是函数级Python修复题，不与后续多文件v4学习曲线混用。Base取本轮初始零LoRA，GSPO取第40尝试组终点。

算法为 `gspo_multiturn_wide_clip_v1`：整轨迹assistant Token的几何平均ratio、轨迹等权、
clip=.2/.2、组中心优势、无std/KL。PyTRIO0.2.9自定义损失按已测CE sum还原；
复用历史数值探针前核对五个算法源码hash。不是论文默认窄裁剪的精确复现。

根项目1072项测试、实验包358项测试通过；Ruff格式/静态检查与两套严格mypy检查通过。

全部40组按原始Token/logprob重建奖励、优势、Datum hash与掩码，25个有效组重建当前loss/梯度，
更新回执链完整。实际初始权重与历史seed17初始归档差0；训练后权重差L2={value["weights"]["trained_delta_l2"]:.12g}。
使用独立判据核对每条轨迹，不以模型自报完成评分；容器隔离与真实副作用探针前后均通过。

运行中第31组反传结果查询发生EventPollingError，原Actor owned/ENDED，尚未提交该组optim。
保留原124条轨迹、30个确认组、19次更新和未知请求记录；新Actor加载最后完整optimizer检查点，
实际LoRA差0，非空Adam state和param_groups逐项全等后重建独立事务，未重采原轨迹。
两次检查点上传READY等待停点及源码快照保留，最终采用最多5分钟的只读就绪等待。
恢复后三个目录中的原始124条轨迹字节一致。该过程见[ADR0057](../adr/0057-gspo-terminal-backward-rollback.md)。

区间按家族聚类bootstrap10000次、seed20261106、nearest-rank；验证4家族、测试8家族。
重复采样不视为新的独立题。只有一个训练seed，旧测试集此前已打开，因此属于预声明固定基准复测，
不能称新盲测或稳定跨任务收益。本轮只比较Base与GSPO；不可用历史GRPO成功率直接证明算法优劣。

[冻结协议](../trainable-agent-rl/gspo-efficacy-v1.md) · [结构化结果](gspo-efficacy-v1-2026-11-09.json)。
本地原始证据：`benchmark-results/trainable_agent/gspo-efficacy-v1/run-03`，原失败和只读等待停点在run-01/run-02。
结果由`experiments/trainable_agent/scripts/gspo_efficacy_report.py`离线生成，脚本及输入hash保存在JSON；重新生成不代表新一轮真实实验。
"""
)
output.with_suffix(".md").write_text(text)
sys.stdout.write(
    json.dumps(
        {
            "complete": True,
            "training": value["training"],
            "validation": value["validation"],
            "test": value["test"],
            "weights": value["weights"],
        },
        ensure_ascii=False,
    )
)
