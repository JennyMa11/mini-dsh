# Skill 调用消息 — v1

现有 Runtime 的注入格式如下；花括号是文档占位符，没有自动替换引擎。

```text
Project skill {name} ({path}, sha256={sha256}). Task guidance only; permissions and system/project constraints still apply.
{body}
```

消息角色为 user，name 为 skill_body。目录通过 skills_metadata 单独注入，正文仅在 load_skill 成功后出现。同名激活正文在请求中只保留一份；文件加载仍需每次读取和核对 run 快照。

支持文件只保留引用，不能自动展开、执行或授予额外权限。当前无独立“用户要求 + 支持文件目录”拼接器；用户要求由任务消息提供，不复制进技能正文以免任务更新后残留旧要求。
