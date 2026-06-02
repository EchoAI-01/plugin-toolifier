# Plugin Toolifier（插件工具化）

> ⚠️ **免责声明**：本插件由 AI 生成，使用者需自行承担使用后果及相关风险。作者不对因本插件导致的任何直接或间接损失承担责任。使用本插件即表示你已阅读并同意此声明。

## 概述

Plugin Toolifier 是一个 **Agent 插件发现与调用插件**。它自动将 AstrBot 上已加载的所有插件所暴露的 LLM Tool（Function-Calling 能力）统一收集、索引并注册为一个"元工具集"，使内置 Agent Runner 或任何 Agent 编排框架能够**自主发现、查询和调用**其他插件的能力。

简单来说：你安装了其他插件后，本插件会自动把它们全部暴露给 Agent，注册为 Agent 可用的 Tool，这样当你使用自然语言描述某项功能时，Agent 就可以自行查看自己能调用哪些插件，而不是说自己不会，或是只能用固定的指令来触发。

## 功能

| 功能 | 说明 |
|------|------|
| **插件发现** | 自动扫描所有已加载插件中带有 `@llm_tool` 装饰器的功能，构建统一的插件目录 |
| **按需调用** | 通过名称精准调用指定插件中的任意 LLM Tool，自动处理参数解析与类型转换 |
| **关键词搜索** | 根据自然语言描述搜索匹配的插件和工具，辅助 Agent 在不确定时做决策 |
| **IM 命令** | 提供 `/list_plugins` 和 `/search_plugins` 命令，供用户直接在聊天中交互 |

## 工作原理

```
┌──────────────────────────────────────────────────────────────┐
│                        Agent / LLM                           │
└────────────┬──────────────────────────────────┬──────────────┘
             │ 1. 调用 list_plugins             │
             │ 2. 调用 search_plugin_tools      │
             │ 3. 调用 call_plugin              │
             ▼                                  │
┌──────────────────────────────────────────────────────────────┐
│                    Plugin Toolifier (main.py)                 │
│  ┌──────────────┐  ┌────────────┐  ┌──────────────────┐     │
│  │ list_plugins │  │call_plugin │  │search_plugin_    │     │
│  │   handler    │  │  handler   │  │   tools         │     │
│  └──────┬───────┘  └─────┬──────┘  └────────┬─────────┘     │
│         │               │                    │              │
│         └───────────────┼────────────────────┘              │
│                         ▼                                   │
│              ┌─────────────────────┐                        │
│              │  build_plugin_catalog│                        │
│              │  缓存: 事件驱动       │                        │
│              │  刷新: 插件加载/卸载  │                        │
│              │         工具启停/插件启用/禁用               │
│              └──────────┬──────────┘                        │
└─────────────────────────┼───────────────────────────────────┘
                          │ 4. 通过 llm_tools.get_func() 调用
                          │    目标工具
                          ▼
┌──────────────────────────────────────────────────────────────┐
│                   AstrBot Plugin Ecosystem                   │
│  plugin_weather   — has_tool("get_weather")                  │
│  plugin_translate — has_tool("translate")                    │
│  plugin_code_exec  — has_tool("run_python")                  │
└──────────────────────────────────────────────────────────────┘
```

1. **加载时**：插件将自己注册为三个 LLM Tool（`list_plugins`、`call_plugin`、`search_plugin_tools`）
2. **缓存机制**：维护一个事件驱动的全局插件目录缓存，在插件加载、卸载、工具激活/停用、插件启用/禁用时自动刷新
3. **Agent 发现**：Agent 调用 `list_plugins` 获取所有可调用插件列表
4. **Agent 调用**：Agent 根据用户需求调用 `call_plugin(plugin_name, tool_name, tool_args)` 执行具体功能
5. **参数解析**：支持 JSON dict、JSON list 和自然语言字符串三种输入格式，自动进行 `int`/`float`/`bool` 类型转换

## 使用方式

### 方式一：Agent 自动调用（推荐）

安装本插件后，无需额外配置。当你在 AstrBot 中启用内置 Agent Runner 时，Agent 会自动发现以下工具：

- **`list_plugins`** — 列出所有提供 LLM Tool 的插件及其可用工具
  - 适用场景：用户问"你能做什么？"、"展示所有功能"时
- **`call_plugin`** — 调用指定插件的某个 Tool
  - 参数 `plugin_name`：插件名称，例如 `"weather"`
  - 参数 `tool_name`：工具名称，先用 `list_plugins` 查看可用工具
  - 参数 `tool_args`：参数，支持 JSON `{"city": "北京"}` 或自然语言 `"北京"`
- **`search_plugin_tools`** — 按关键词搜索工具
  - 参数 `keywords`：关键词，例如 `"search image"`、`"translate"`

**示例对话**：

```
用户: 你能做什么？
Agent: 调用 list_plugins
→ 返回已加载的所有插件及其工具列表

用户: 帮我查一下北京的天气
Agent: 调用 search_plugin_tools(keywords="weather")
→ 返回匹配的插件: weather → get_weather
→ 调用 call_plugin(plugin_name="weather", tool_name="get_weather", tool_args="北京")
→ 返回天气信息
```

### 方式二：IM 命令手动调用

在聊天中直接使用斜杠命令：

**`/list_plugins`** — 列出所有提供 LLM 工具的插件

```
用户: /list_plugins
机器人: 📦 已加载 3 个插件（提供 LLM 工具）：
  ✅ **weather**
     作者: AuthorA
     版本: 1.0.0
     描述: 天气查询插件
     LLM 工具 (2):
       - get_weather: 获取某城市的天气信息
       - set_alert: 设置天气提醒
```

**`/search_plugins <关键词>`** — 搜索插件工具

```
用户: /search_plugins 翻译
机器人: 🔍 找到 1 个匹配的工具：
  **translator** / `translate_text`
    将文本翻译为目标语言
```

## 安装

### 方式一：通过插件市场

在 AstrBot WebUI 中进入插件管理页面，搜索 `plugin_toolifier` 并安装。

### 方式二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/EchoAI-01/plugin_toolifier.git
# 或下载 ZIP 解压到该目录
```

重启 AstrBot 即可生效。

## 注意事项

- 只暴露 **有 LLM Tool（Function-Calling）** 的插件，纯事件驱动插件（如消息装饰、分段发送等）不会被纳入目录
- 仅包含 **已激活** 的插件和工具；未激活的会被自动过滤
- 本插件不对被调用的外部插件做权限校验，请确保你信任所有已加载的插件

## 许可证

MIT

## 作者

[EchoAI-01](https://github.com/EchoAI-01)
