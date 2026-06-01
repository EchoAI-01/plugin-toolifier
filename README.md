# Plugin Toolifier（插件工具化）

自动将 AstrBot 插件注册为 LLM 工具，使 Agent 能发现、查询、调用所有插件的能力。

## 功能

向 Agent 暴露三个核心 LLM Tool：

- **`list_plugins`** — 列出所有有 LLM Tool 的插件
- **`call_plugin`** — 按名称调用指定插件的 LLM Tool
- **`search_plugin_tools`** — 按关键词搜索可用工具

同时提供 IM 命令 `/list_plugins` 和 `/search_plugins` 供用户直接调用。

## 设计

- 只暴露有 LLM Tool（function-calling）的插件，过滤纯辅助性质的插件（如分段发送、消息装饰）
- 纯事件驱动缓存：插件加载/卸载、工具激活/停用、插件启停时自动刷新，无定时轮询开销

## 安装

1. 克隆或下载此仓库到 `data/plugins/` 目录
2. 重启 AstrBot

## 许可证

MIT

## 作者

[EchoAI-01](https://github.com/EchoAI-01)
