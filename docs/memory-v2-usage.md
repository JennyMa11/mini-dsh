# 渐进式项目 Memory（P2）

当前实现支持 `progressive_v2`：先发现已批准主题，再主动搜索，按修订和内容 hash 读取正文。阶段验收进度见[执行记录](memory-v2-execution.md)；P3 结构化笔记已验收，P4 手动整理已验收，P5 收益实验尚未开始。P1 [设计](memory-v2-design.md)和[验收规格](memory-v2-acceptance.md)保留原冻结文本。

在既有私有 Memory 配置中增加以下字段即可选择新模式，其他 project/database/evidence/grants 配置沿用 [v1 说明](extension-memory.md)。Store 和证据目录必须在 Actor 工作区之外；使用进程/Git 能力时仍需真实 OS 隔离。

```toml
[memory]
project_id = "my_project"
applicable_version = "v1"
database = "/absolute/private/memory.sqlite3"
storage_root = "/absolute/private"
retrieval_mode = "progressive_v2"
index_token_limit = 512
search_token_limit = 512
read_token_limit = 2048
```

上面仅展示配置结构；没有证据 grants 或已提交记录时没有可读取知识。不要照抄示例路径到真实配置。通用 Memory 默认关闭；省略 retrieval_mode 或设为 deterministic_v1 使用旧路径，mini-dsh-work 不隐式升级。回退旧二进制前移除新配置字段；仅使用P2不会迁移数据库；P3迁移须显式执行。

Context 每次模型准备调用受控隐藏工具 prepare_memory_index，最多提供五张无正文卡片；实际数量还受 Token 限制。模型使用 search_memory(query, limit, paths?, error_type?) 发现索引外条目，read_memory(references) 读取正文。每个引用包含 entry_id、expected_revision、expected_content_sha256，最多五个不同 ID。query 最多256字符/1KiB；不做同义改写或向量匹配。条件卡片可提前发现，正文需要已经执行的路径/错误事实；查询条件不能伪造适用性。

索引/搜索/正文上限只能下调，均包含完整 JSON 包裹和来源元数据；超限从尾部省略完整记录，避免留下半条规则。所有准备/读取计入全局工具、时间与 Token 预算。每次暴露前检查原快照引用、当前状态和来源；修改/拒绝/来源变化会从派生 ModelRequest 撤回旧正文，事实历史不回写。单run不纳入外部新增条目。

提议仍不具备批准权。六项操作者维护工具只在运行外可用；独立证据、明确操作者批准、修订/hash绑定和commit流程保持。正文属于低信任项目资料，即使包含命令式句子也不会获得新工具或权限。

在 progressive_v2 中显式配置 forbidden_content_sha256 时，Model port 的响应入口检查已登记的完整/trimmed文本和规范JSON指纹。命中会拒绝整个响应，保留实际用量及unknown标记；流式显示等最终响应检查后再返回。该能力不是任意子串、语义或编码泄漏检测器。未登记指纹时不加此装饰器。Secret 继续使用既有敏感值保护。

新增事实事件 memory_index_prepared、memory_search_completed、memory_read_completed 包含选择器/快照、引用、排除原因、裁剪及耗时，不另记 query 或经验正文；受控工具结果仍进入既有 Trace。Context来源以消息hash绑定索引/正文包；包内含逐项revision/hash/evidence/approval。

离线例子：`uv run python examples/progressive_memory_demo.py`。所有维护都在临时目录，fixture_operator仅批准合成夹具，真实模型调用为零。该例子不能证明机制提升真实任务成功率。

## P3 结构化笔记与历史（已验收）

结构化MemoryNote把trigger、rule、verification、exceptions、sources及supersedes一起放入2KiB规范JSON正文，沿用内容hash和批准指纹。verification是验证方法说明，不是验收结果。旧文本/旧批准不改写；结构化写入需要显式历史迁移。

```bash
uv run mini-dsh memory migrate --config ./memory.toml --operator my_operator
uv run mini-dsh memory review --config ./memory.toml --operator my_operator --arguments '{"entry_id":"draft"}'
uv run mini-dsh memory history --config ./memory.toml --operator my_operator --arguments '{"entry_id":"draft","after_sequence":0}'
```

普通CLI维护只执行指定操作者工具，不创建模型；arguments是最多64KiB的JSON对象，不读取任意草稿路径。operator是调用者明确提供的身份。操作结果包含私有Trace文件名，保存在storage_root；deny_tools仍优先，输出服从配置tool_output_limit，超限显式truncated。历史每页一个转移，next_after用于下一页；较长历史状态可使用显式更大的输出上限审阅。

物理SQLite user_version从1显式迁移到2：保留当前payload与批准bytes，并为当前状态建立migration_snapshot，更早过程保持未知。迁移先保留hash命名的私有备份；在最后受控发布检查点前失败/取消保留旧库，可能留下完整可审计备份；跨过该点后的取消/通知失败按可能已提交处理，读回确认，不声称回滚。旧二进制拒绝version2；回退应使用迁移前备份或受控导出。库仍限8MiB，当前条目每项目100条，满时拒绝而不淘汰。

模型工具propose_memory_note仅在progressive_v2中可用，需要memory.propose/read/evidence.read，且只产生proposed。原propose_memory不能用结构化JSON绕过这些要求。sources中的ID、revision、完整提议指纹、证据hash与source_run_id必须对应真实committed修订；supersedes是sources子集，且必须对应当前仍committed的旧记录。

运行外merge创建新提议；revise-note修改结构与来源run，递增修订并撤销旧授权。validate必须使用该新主张的专属证据，不能重用来源关系中的旧hash/run；来源关系本身不算新主张验证。明确approve绑定审阅过的正文hash和修订；commit若含supersedes，在一笔事务中提交新记录并使全部旧记录superseded，过期引用或发布故障时全部不变。

CLI action还支持merge、revise-note、validate、approve、commit、reject及旧文本revise，通过对应工具的严格JSON参数调用。approve参数包含entry_id、expected_revision、content_sha256、approved_at（UTC）；commit包含entry_id、expected_revision、approval_sha256。只有运行外操作者工具拥有这些能力，模型不能通过正文句子取得它们。

P3矩阵、完整示例与迁移后OS复验见[阶段报告](results/memory-v2-p3-2026-11-12.md)。这些结果不代表P4整理或真实收益已经交付。

P3完整离线示例：uv run python examples/structured_memory_demo.py。临时私有库先保存两条v1批准记录，明确迁移并逐bytes核对备份；人工合并只产生proposed，新证据与新批准后才原子替代旧记录，随后新run读取结构化正文与历史。使用合成fixture_operator与ScriptedModel，真实模型调用0。

## P4 手动整理（已验收）

`memory consolidate`是唯一会构造模型的Memory维护命令；需要显式的provider配置或离线`--script`。模型参数沿用配置解析的profile/模型选择，整理单独使用参数中的预算（默认12步、16工具、8000 Token、30秒，只能缩小），不继承Actor运行额度，不统计金额。省略命令不会触发整理，没有后台调度。

```bash
uv run mini-dsh memory consolidate --config ./memory.toml --operator my_operator --script ./responses.json --arguments '实际授权材料的JSON'
uv run mini-dsh memory review-consolidation --config ./memory.toml --operator my_operator --arguments '{"source_run_id":"实际consolidation_run_id"}'
```

命令中的占位参数须替换为实际值；可直接运行的合成例子是 `uv run python examples/memory_consolidation_demo.py`。真实调用前应自行审阅明确授权的材料，不能把fixture身份当作用户授权。

arguments包含`materials_authorized: true`和`redacted: true`，两者必须为真正的布尔值。`sources`是至多五个来源引用，每个包含entry_id、revision、proposal_sha256、evidence_sha256、source_run_id，须与`review`回执中的实际当前committed记录一致。`facts`是至多五条已授权脱敏事实，每条包含fact_id、source_run_id、content、content_sha256；content最多2KiB、总共8KiB。至少提供一项来源或事实，不接受任意Trace/sidecar路径、隐藏判据、accepted/批准字段；原始事实本身不能直接包装成新规则。

整理器每次模型准备经受权工具重新核对来源，写入前再核对；模型只获得`propose_consolidated_note`，字段为kind、topic、trigger、rule、verification、exceptions、paths、error_type及supersede_sources。来源由运行时固定；supersede_sources只表示请求后续人工替代，不能当场撤销旧知识。一次最多尝试五次提议写入，不自动重试。实际提议仍受完整2KiB包裹、现有Store配额、来源与内容保护。

结果含独立run_id、input_sha256、操作者、来源指纹、候选ID/修订/内容hash与预算/实际用量/超额/未知用量。候选全部是proposed；应使用review审阅完整正文，再用新的专属证据validate、明确approve、commit。`finished`只表示Loop正常结束，不保证提议正确或存在候选。

Trace保存在storage_root下`memory-operator-*.jsonl`，会话事实与快照保存在其私有`.minidsh`目录；Actor工作区不创建整理会话。中断后从Trace的memory_consolidation_started获取run_id和input_sha256，再用review-consolidation查回完整落库记录。发布前取消不写入；发布后取消可能已有完整proposed，通知失败也须读回，不能声称回滚。原始Actor Trace不修改。保留这些私有审计文件，才能核对候选与原始授权输入。

输入加载、提议和外层整理均服从deny优先的权限；整理与同一MemoryService上的Actor/维护互斥。登记禁止hash同时约束输入和模型响应入口；这里只检测已知完整/规范JSON指纹，不保证任意语义或编码脱敏。独立验收材料不得作为facts交给模型。

显式--script优先于provider环境/配置，直接选择离线适配器；普通维护与离线整理不会因环境中存在真实profile而调用真实模型。最终复验覆盖了该分支。

## P5 固定配对实验（阶段未验收）

执行器为`uv run python scripts/memory_acceptance.py`，复用原隔离执行、评分器与配对统计。固定协议见[JSON](../benchmarks/memory_v2/p5-protocol.json)，冻结检查见[覆盖矩阵](memory-v2-p5-matrix.json)。以下路径是操作者占位目录，须替换为实际的新私有目录；source-seed须指向原三个批准笔记的实际manifest，不能使用评测答案/判据。

```bash
uv run python scripts/memory_acceptance.py plan --output /private/p5/snapshot
uv run python scripts/memory_acceptance.py prepare --snapshot /private/p5/snapshot --source-seed /private/approved/seed.json --output /private/p5/offline-review --provider scripted
```

先审阅`offline-review/review.md`及`authorization-template.json`。离线演练复制该模板到另一文件，显式设置`authorization_kind=synthetic_fixture`、`approved_by=fixture_operator`及实际操作者日期，仅授权合成演练，不变更原审阅包。随后执行：

```bash
uv run python scripts/memory_acceptance.py authorize --snapshot /private/p5/snapshot --bundle /private/p5/offline-review --authorization-file /private/p5/fixture-authority.json --output /private/p5/offline-seed --fixture
uv run python scripts/memory_acceptance.py preflight --snapshot /private/p5/snapshot --bundle /private/p5/offline-seed --output /private/p5/preflight
```

preflight顺序运行四门禁、临时源码重建与冻结离线安装、13项机制/故障用例、3项实际OS用例、90槽位脚本演练及只读派生。确认`preflight.json`及其绑定文件；任一缺失/变化/非真布尔判据均阻止真实入口。脚本模型只用于路径和判据，不支持收益声明。

本机配置`DEEPSEEK_API_KEY`后，才可用`prepare --provider deepseek --preflight /private/p5/preflight`生成另一新的真实审阅目录；凭据文件也可通过`--credentials`明确提供，不放进仓库或聊天。真实入口本身检查preflight，先扫描输入及压缩源码中的当前精确密钥，再预留唯一一次整理。失败/取消保留原始Trace、用量及终态；预留文件不表示进程活跃，也不会自动解除后重试。

真实提议仍全部proposed。由用户审阅实际正文/来源/修订/content hash后，新授权文件设置`explicit_user_approval`并绑定当前协议、源码及review hash；历史M5授权不通用。使用`authorize`时不带`--fixture`，在另一新目录提交批准种子；随后运行：

```bash
uv run python scripts/memory_acceptance.py run --snapshot /private/p5/snapshot --bundle /private/p5/approved-real-seed --provider deepseek --preflight /private/p5/preflight --output /private/p5/real-execution
uv run python scripts/memory_acceptance.py report --snapshot /private/p5/snapshot --execution /private/p5/real-execution --output /private/p5/derived-report
```

整轮只预留一次，不自动恢复、扩题或补跑；每任务额度固定，余量不足时停止。报告保留全部90机会、失败消耗及未知值。配对收益只适用于这六个固定已知任务；服务端模型ID/指纹缺失时明确记未知，不能宣称权重固定。原始Trace不修改；新评分器只读重算要另存目录，保留执行源码和派生源码的区别。
