# ADR 0009：显式组件扩展与命名运行配置

- 状态：Accepted

## 背景

已有端口、PluginManager 和 ServiceRegistry 支持替换与生命周期，但本地 composition 固定创建 Context、Session、Permission、工具和执行器。调用方只能自行重写组装逻辑；CLI 中多项已有配置无法覆盖，布尔参数也不能撤销配置文件的授权。

## 决策

保留同步 `build_local_runtime` 兼容入口，新增 `RuntimeComponents` 显式组件注入。模型通过已有参数注入，事件总线抽象为可订阅的 EventStream。工具扩展始终进入统一 ToolRuntime，不用插件工具绕过 Schema、Permission 或 Observation。

新增异步上下文管理器 `plugin_runtime`：用现有 PluginManager 验证依赖、启动插件，按 `RuntimeServices` 指定服务键选择各端口实现，退出时逆序释放。运行期间不暴露热替换入口。插件代码为可信的宿主扩展；OS 隔离仍由工具 infrastructure adapter 负责。

新增 `[profiles.NAME]` 浅层配置覆盖，选择顺序为 CLI、环境变量、文件默认方案。生效字段顺序为 CLI、环境变量、已选方案、基础配置、安全默认值，逐字段保留来源。配置不包含 Python 导入路径。

工具可显式筛选或关闭整套内置实现。新增能力需通过 `extra_permissions` 明确授权，内置能力保留专用授权选项，自定义 Permission 只能收紧基础策略。

## 替代方案

- 通过仓库配置自动导入 Python 工厂：容易将读取配置变成执行代码，难以保持显式信任边界。
- 由插件直接返回完整 AgentLoop：会复制安全执行链并使预算、Trace 和生命周期行为失去一致性。
- 引入全局服务定位器：破坏构造函数注入和运行间隔离。
- 支持方案继承和动态热重载：首版无需此复杂度；步骤边界重新绑定已有对象需要独立契约。

## 后果与迁移

默认配置和现有 CLI 行为保持兼容。调用方可先用 RuntimeComponents 替换单个 adapter，再将需管理资源的组件迁移为 Plugin。CLI 可选择内置方案，外部插件由 Python 嵌入 API 显式传入。公共协议使用 runtime_checkable 进行结构校验；行为语义仍由 adapter 的契约测试验证。
