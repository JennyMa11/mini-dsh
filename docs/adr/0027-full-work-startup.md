# ADR 0027：显式完整开发启动配置

2026-11-04，按用户“工具和可选能力两种都启动”的要求新增。

## 决策与范围

新增 `uv run mini-dsh-work` 项目开发入口，同时配置 13 个编码/扩展工具、
examples/skills、完成前固定 pytest 验证和 Project Memory。复用现有 work、
ContextAssembly、Tool Runtime、权限、预算和生命周期，不修改核心 Loop。
现有 `mini-dsh work`、chat、run、评测和通用 Runtime 默认值保持原行为。

完整入口明确用于当前 Mini-dsh 仓库，project_id=mini_dsh，
applicable_version=work_v1，不从目录 basename 推断身份。
Memory 默认存储在仓库父目录 `.mini-dsh-private/mini_dsh`，可用
`--memory-root` 显式指定工作区外目录。新建目录使用 0700；不自动批准、
不生成独立验收来源，不迁移历史数据库。没有证据授权时只能提议候选。
操作者维护能力不进入模型工具集合。

Shell/Git 与 Memory 同时存在要求 Docker 隔离，不允许伪造隔离声明或退回本地。
专用开发镜像包含 Python、uv、Git、锁定开发依赖和构建后端；执行时沿用
现有 Docker 后端的网络关闭、只读根、权限裁减、工作区绑定及控制目录屏蔽。
镜像构建阶段允许获取依赖，构建上下文只含 pyproject/lock/Dockerfile。
模型 API 请求仍由宿主模型 adapter 发出。镜像标签和额外构建后端未按 digest
冻结，属于日常开发配置，不是可复现 Benchmark 镜像。

写入/执行仍逐次审批，Shell 保留显式程序 allowlist。
固定 test 为 `uv run --no-sync pytest -q`，build 为
`uv build --no-build-isolation`。配置解析和检查不调用模型。

## 替代方案与迁移

不修改全局默认值：其他工作区未必使用 Python/pytest，也未必准备独立存储。
不自动开启 Memory 批准或向量检索。M0–M5 历史报告保持原样；本入口是用户
另行授权的启动范围变更，不沿用历史 hash 宣称新版本已验收或有收益。
需要用于其他项目时应提供该项目的显式身份、技能及测试/构建配置，沿用普通
work 配置入口；本入口不猜测其他项目类型。
