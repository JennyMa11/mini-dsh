# 提示词模板与配置示例 v1

本目录基于用户提供的七类模板职责人工编写，未复制或核验 Hermes 源码，不声明与其实现兼容。来源为 2026-11-05 的项目配置补全请求；后续直接维护这些源文件，无生成步骤。

这是配置/模板交付，不新增 Runtime 加载器、模型摘要调用、后台复盘或自动技能维护。Markdown 不会被自动发现或注入。不要把本目录配置为 skill_roots；它们不采用 SKILL.md 契约。

2026-11-05 用户决定：自动提炼/生成 Skill 保持关闭，不接入会话结束或后台 Hook，不自动调用 Skill Review/Curator Review、不从会话写入或修订技能。模板与既有真实模型样例仅作为人工参考；后续重新启用需要用户明确要求。人工维护技能的发现与 load_skill 显式加载保持原行为。当前没有对应运行开关，不添加解析器不支持的 enabled 配置键。

| 模板 | 作用 | 当前接入状态 |
| --- | --- | --- |
| [system_assembly.md](system_assembly.md) | 稳定性分层、来源与生命周期约定 | 装配规范草案；现有排序仍由 ContextAssembly 决定 |
| [context_handoff.md](context_handoff.md) | 结构化任务交接摘要、状态及技能重载信息 | 可供人工显式使用；未接入 summary/budget |
| [memory_review.md](memory_review.md) | 从证据中提出项目记忆候选 | 人工审阅模板；不绕过现有提议/验证/批准/提交状态机 |
| [skill_review.md](skill_review.md) | 将经历整理为可复用步骤与决策规则 | 人工审阅模板；无自动写入或激活 |
| [curator_review.md](curator_review.md) | 识别技能重叠、冲突并提出迁移建议 | 人工审阅模板；无后台整理 |
| [skill_message.md](skill_message.md) | 说明现有 Skill 正文注入格式 | 对应 ContextAssembly._guidance；此文件本身不参与渲染 |
| [soul.md](soul.md) | 默认身份和行为风格建议 | 人工参考；无 SOUL 文件自动加载机制 |
| [runtime.toml](runtime.toml) | 使用现有配置键选择预算、上下文和行为 | 可直接解析；内含精简身份提示词，不会引用相邻模板 |

从 Mini-dsh 仓库根目录检查配置，不调用模型：

```bash
uv run mini-dsh config --config examples/prompt_templates/runtime.toml
uv run mini-dsh config --config examples/prompt_templates/runtime.toml --profile summary
uv run mini-dsh config --config examples/prompt_templates/runtime.toml --profile full
```

显式启动一次工作会话：

```bash
uv run mini-dsh work --config examples/prompt_templates/runtime.toml
```

本配置明确禁用写入和 Shell，覆盖 work 的相应预设；不配置模型凭据、Memory 存储或独立验证命令。Skills 路径相对目标工作区；用于其他项目时必须更换为该项目实际维护的技能目录。

`budget` 仍保留受保护状态并裁剪完整工具回合；`summary` 仍保留近期 12 条消息并使用 4000 字符的确定性摘要正文。切换到 summary 不启用结构化交接。实际字段来源由 config 命令报告，CLI/环境变量可覆盖示例。

人工使用交接或复盘模板时，只提供允许使用的已脱敏事实和证据引用。模板输出属于待核对的派生资料，不直接回写 Session/Trace，不作为系统指令、独立验收证据或数据库导入格式。不得把 Secret 或隐藏判据交给模型。

后续若接入结构化摘要，须先冻结字段/来源契约、摘要调用预算与失败行为，测试关键状态、工具因果配对、事实不可变、技能重载和受保护段超限。Memory/Skill/Curator 自动化需另行记录范围与架构决策；本目录不构成运行验收或机制收益证据。

## 本次配置检查（2026-11-05）

本次 T01 仅增加本目录、README 入口与主计划变更记录，未修改 Runtime 源码。三个 config 命令均退出 0；另以空 environ 调用 resolve_config 核对三种策略、16384 输入预算、禁止写入/Shell、Skills 根和未配置 Memory，并检查本目录相对链接，均通过。

后续补充自动化 case：`uv run pytest -q tests/test_prompt_templates.py`。共 15 项：七类模板的约束文本契约、交接章节完整顺序、三种配置解析与权限、Skill 格式和实际渲染一致性、Context 排序和工具因果配对、旧 summary 不隐式加载新模板、目录链接。Review/SOUL 的文本检查只防止约束从模板中丢失，不评判模型输出；Skill 和排序 case 调用真实装配方法，未请求模型。模板的同义改写需要同步人工审核 case。

补充 case 后四门禁复验通过：格式检查 301 files，Lint 通过，mypy 166 files 通过，全量 `uv run pytest -q` 为 1024 passed（47.21 秒）。首轮全量有两项文档计数检查失败，更新当前文档中的规模数字后重新全量通过；未改写历史结果。

四项工程门禁通过：`uv run ruff format --check .`（300 files）、`uv run ruff check .`、`uv run mypy src tests`（165 files）、`uv run pytest`（1009 passed）。这些结果只说明当前配置与回归检查通过；未调用真实模型，未对新模板执行语义恢复、复盘质量、缓存收益或新增 OS 副作用探针。本次不推进 PR 4/M5 验收状态。
