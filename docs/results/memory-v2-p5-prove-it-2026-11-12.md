# P5开发复核：prove-it（阶段未验收）

当前分支`feat/memory-system`，独立工作区`/Users/jennyma/Projects/mini-dsh-memory`。P2–P4历史验收保持；本记录只核对P5开发，**不签署P5、不声明Memory收益**。完整路径、日志和hash见[机器记录](memory-v2-p5-prove-it-2026-11-12.checks.json)。

| 检查 | 实际结果 |
| --- | --- |
| 当前四门禁 | `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy src tests`、`uv run pytest`全部通过；**1290通过、0失败、0跳过**，mypy 186文件 |
| 固定90槽位脚本演练 | 六题×五组×三次，**90条独立验收记录/90个声明机会**；独立任务判据通过90/90。保留90份Trace/sidecar和180份Actor/验证器容器回执；真实模型调用0 |
| 五组机制及取消 | 六项实际Docker用例，**53/53判据通过**：off无投影/Memory事件、v1正文、索引组拒绝主动工具、主动组读回精确批准正文、私库不变、取消后保留产物和独立检查 |
| OS边界及子进程 | 三项实际Docker用例，**19/19判据通过**：主机路径不可见、同路径guest写入不改变host、控制端canary环境不传入、真实socket禁网、原生1 CPU/1GiB、已启动子进程的超时/取消清理及容器移除 |
| 报告回归 | 恢复旧D-only机会数规则时新测试真实失败；修正后通过。五组正文暴露均按18个声明机会统计；无独立语义标签时不推定适用性 |

执行源码为`7379361453de7a91ee768f9646b23bd7ed33e7d17b846a25d103f01336db4a8b`，当前工程门禁与报告器源码为`81a897caf7624e2cfafe5f5c10f25e95f0aa9bbea0b28ae6b44ff5740fbb4162`。90槽位演练和53项机制判据属于执行源码；报告器随后修正旧M5比较器的暴露机会数，并新增回归测试/更新当前数量声明。源码差异另存`source_delta.json`，production模块和原始Trace未变；当前报告是只读重算，**不是新版本重新实测**。19项OS判据在当前源码下另行运行。两版均有完整四门禁；前版1289、后版1290通过。

本轮脚本估算共181587 Token（含整理及失败消耗），用量来源为`scripted_prompt_estimates`，不能当作真实API用量或Token节省结论。普通脚本队列没有主动调用search/read_memory；该路径由单列的主动机制探针覆盖，不能据此断言真实模型会使用这些工具。派生报告明确`real_model_results=false`、`benefit_evaluation=not_assessable`。

失败记录保持：首轮全仓测试有4条过期文档数量声明失败，修正后完整重跑；首个native驱动把“仅v1首请求预加载正文”误写成“整个run仅v1出现正文”，主动读后出现正文因此触发失败。保留`attempt_0/native_active/probe_check.json`和原驱动，修正为检查首请求并单列主动读取后正文投影后，在`native_retry_1`重新跑完六项，没有覆盖原证据。

原始材料位于Git忽略目录`benchmark-results/extension-v1/memory-v2/p5-prove-it`。`attempt_1/artifact_index.json`逐文件记录2801项hash，机器记录固定其hash。仓库报告只引用受控证据，不包含密钥、真实服务缓存或原始敏感材料。

尚未验证：P501–P508完整覆盖签署与最终preflight归并、真实整理质量及真实配对任务收益、任意语义/编码Secret检测。目前本机未配置可用DeepSeek凭据；生成真实提议后仍须批准具体正文/修订/hash，旧M5批准和fixture_operator不授权新真实队列。当前运行结束，没有后台实验在继续；没有界面变更，截图不适用。
