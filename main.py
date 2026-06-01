"""插件工具化（Plugin Toolifier）。

功能：
1. 向 Agent 注册三个核心 LLM Tool：
   - list_plugins: 列出所有有 LLM Tool 的插件
   - call_plugin: 按名称调用指定插件的 LLM Tool
   - search_plugin_tools: 按关键词搜索可用工具
2. 同时提供 IM 命令 /list_plugins 和 /search_plugins 供用户直接调用。

设计说明：
- 只暴露有 LLM Tool（function-calling）的插件，过滤纯辅助性质的插件（如分段发送、消息装饰）。
- 仅在有事件触发时重建缓存（插件加载、卸载、工具激活/停用、插件启停），不再使用定时 TTL。

缓存失效覆盖场景：
- 插件安装/加载 → on_plugin_loaded 事件
- 插件卸载 → on_plugin_unloaded 事件
- 工具单独停用/激活 → FunctionToolManager 方法猴子补丁
- 插件禁用/启用 → PluginManager 方法猴子补丁
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter


class Main(star.Star):
    """Agent 插件发现插件。

    自动将所有已加载插件的 LLM Tool 暴露给 Agent，
    使 Agent 能感知、查询、调用任何插件的功能。
    """

    author = "Chen"
    name = "plugin_toolifier"
    desc = "插件工具化 — 自动将 AstrBot 插件注册为 LLM 工具，使 Agent 能发现、查询、调用所有插件的能力。"

    def __init__(self, context: star.Context) -> None:
        self.context = context
        # Cached catalog: None means cache is stale
        self._catalog_cache: list[dict[str, Any]] | None = None
        # Track whether monkey-patching has been applied (global, one-time)
        self._patched = False

    async def initialize(self) -> None:
        from astrbot.core.provider.register import llm_tools

        llm_tools.add_func(
            "list_plugins",
            [],
            (
                "List all available AstrBot plugins that expose LLM tools "
                "(function-calling capabilities). Only plugins with active "
                "callable tools are listed. Pure helper plugins (like message "
                "formatting) are excluded. Each entry includes name, description, "
                "version, available tools list, and commands list. "
                "Call this when the user asks what you can do, wants to see "
                "capabilities, or lists all available features."
            ),
            self._list_plugins_handler,
        )

        llm_tools.add_func(
            "call_plugin",
            [
                {
                    "type": "string",
                    "name": "plugin_name",
                    "description": "Name of the plugin to call. E.g. 'weather', 'image_generation', etc.",
                },
                {
                    "type": "string",
                    "name": "tool_name",
                    "description": "Name of the specific tool to call within the plugin. Use list_plugins first to see available tools.",
                },
                {
                    "type": "string",
                    "name": "tool_args",
                    "description": "Arguments for the tool as a string. Can be JSON format like '{\"key\": \"value\"}' or natural language description.",
                },
            ],
            (
                "Call a specific tool from a plugin to execute a task. "
                "Use list_plugins first to see what plugins and tools are available. "
                "Call this when the user says 'help me do XX', 'execute XX operation', "
                "or 'use XX plugin'."
            ),
            self._call_plugin_handler,
        )

        llm_tools.add_func(
            "search_plugin_tools",
            [
                {
                    "type": "string",
                    "name": "keywords",
                    "description": "Search keywords in natural language, e.g. 'search image', 'translate', 'check weather'.",
                },
            ],
            (
                "Search for available plugin tools by keywords. "
                "Call this when the user describes a need but you're unsure "
                "which plugin fits."
            ),
            self._search_plugin_tools_handler,
        )

        logger.info(
            "Plugin Toolifier: registered list_plugins, call_plugin, search_plugin_tools tools"
        )

        # Monkey-patch method to intercept tool/ plugin state changes that
        # do NOT trigger on_plugin_loaded / on_plugin_unloaded events.
        self._monkey_patch()

    # ---- Monkey-patching for complete cache invalidation coverage ----

    def _monkey_patch(self) -> None:
        """Patch FunctionToolManager and PluginManager methods.

        Covers the following scenarios that do NOT emit on_plugin_loaded /
        on_plugin_unloaded events:

        1. Tool-level toggle: activate_llm_tool / deactivate_llm_tool
           Called from WebUI (POST /api/tools/toggle)
        2. Plugin toggle: turn_on_plugin / turn_off_plugin
           Called from WebUI (POST /api/plugin/on and /api/plugin/off)
        """
        if self._patched:
            return

        # Patch 1: FunctionToolManager.activate_llm_tool / deactivate_llm_tool
        from astrbot.core.provider.register import llm_tools

        _orig_activate = llm_tools.activate_llm_tool
        _orig_deactivate = llm_tools.deactivate_llm_tool

        def _wrap_activate(name: str, star_map: dict) -> bool:
            result = _orig_activate(name, star_map)
            if result:
                self._invalidate_cache()
            return result

        def _wrap_deactivate(name: str) -> bool:
            result = _orig_deactivate(name)
            if result:
                self._invalidate_cache()
            return result

        llm_tools.activate_llm_tool = _wrap_activate
        llm_tools.deactivate_llm_tool = _wrap_deactivate

        # Patch 2: PluginManager.turn_on_plugin / turn_off_plugin
        # These are async, so wrapped versions must also be async.
        from astrbot.core.star.star_manager import PluginManager

        _pm_orig_turn_off = PluginManager.turn_off_plugin
        _pm_orig_turn_on = PluginManager.turn_on_plugin

        async def _pm_wrap_turn_off(self_inst: PluginManager, plugin_name: str) -> None:
            await _pm_orig_turn_off(self_inst, plugin_name)
            self._invalidate_cache()
            logger.debug(f"Plugin {plugin_name} turned off, cleared discovery cache")

        async def _pm_wrap_turn_on(self_inst: PluginManager, plugin_name: str) -> None:
            await _pm_orig_turn_on(self_inst, plugin_name)
            self._invalidate_cache()
            logger.debug(f"Plugin {plugin_name} turned on, cleared discovery cache")

        PluginManager.turn_off_plugin = _pm_wrap_turn_off
        PluginManager.turn_on_plugin = _pm_wrap_turn_on

        self._patched = True
        logger.info(
            "Plugin Toolifier: monkey-patched cache invalidation for "
            "tool toggle and plugin enable/disable"
        )

    # ---- Cache management ----

    def _invalidate_cache(self) -> None:
        """Mark cache as stale. The next catalog read will rebuild it."""
        self._catalog_cache = None

    def _build_plugin_catalog(self) -> list[dict[str, Any]]:
        """Build a catalog of all loaded plugins that expose active LLM tools.

        Only scans plugins that have registered @llm_tool decorated functions.
        Pure helper plugins (e.g. message formatting, segmented reply) are excluded.

        Uses event-driven cache: cache is rebuilt only when stale (plugin loaded,
        unloaded, tool activated/deactivated, or plugin turned on/off).
        """
        if self._catalog_cache is not None:
            return self._catalog_cache

        from astrbot.core.provider.register import llm_tools
        from astrbot.core.star.star import star_map as _star_map

        catalog: dict[str, dict[str, Any]] = {}

        # 1. Collect plugin basic info from star_registry
        for metadata in star.star_registry:
            key = metadata.module_path
            if not key:
                continue
            if key not in catalog:
                catalog[key] = {
                    "name": metadata.name or key.split(".")[-1],
                    "author": metadata.author,
                    "desc": metadata.desc,
                    "version": metadata.version,
                    "activated": metadata.activated,
                    "reserved": metadata.reserved,
                    "tools": [],
                    "commands": [],
                }

        # 2. Collect LLM Tools from func_list (excludes builtin tools)
        for func_tool in llm_tools.func_list:
            # Only include active tools -- inactive ones are hidden from the Agent
            if not getattr(func_tool, "active", True):
                continue
            mp = getattr(func_tool, "handler_module_path", None)
            if not mp:
                continue

            # Find the owning plugin
            meta = _star_map.get(mp)
            if not meta or not meta.name:
                continue

            name_key = meta.module_path or mp
            if name_key not in catalog:
                catalog[name_key] = {
                    "name": meta.name,
                    "author": meta.author,
                    "desc": meta.desc,
                    "version": meta.version,
                    "activated": meta.activated,
                    "reserved": getattr(meta, "reserved", False),
                    "tools": [],
                    "commands": [],
                }

            catalog[name_key]["tools"].append({
                "name": func_tool.name,
                "description": func_tool.description or "",
                "parameters": func_tool.parameters or {},
            })

        # 3. Collect command info from handler registry
        from astrbot.core.star.star_handler import star_handlers_registry
        for handler_md in star_handlers_registry:
            mp = handler_md.handler_module_path
            if not mp:
                continue
            meta = _star_map.get(mp)
            if not meta or not meta.name:
                continue
            name_key = meta.module_path or mp
            if name_key not in catalog:
                continue
            for ef in handler_md.event_filters:
                if hasattr(ef, "command_name"):
                    cmd = ef.command_name
                    if hasattr(ef, "parent_command_names"):
                        for pn in (getattr(ef, "parent_command_names") or []):
                            cmd = f"{pn} {cmd}"
                    if cmd not in catalog[name_key]["commands"]:
                        catalog[name_key]["commands"].append(cmd)

        # 4. Filter: only include active plugins that have at least one tool
        #    Inactive non-reserved plugins are excluded entirely.
        result = []
        for info in catalog.values():
            if not info["activated"] and not info["reserved"]:
                continue
            if not info["tools"]:
                continue
            result.append(info)

        self._catalog_cache = result
        return result

    # ---- Handlers for the three meta tools ----

    async def _list_plugins_handler(self, event: AstrMessageEvent) -> str:
        catalog = self._build_plugin_catalog()

        if not catalog:
            return "当前没有加载任何提供 LLM Tool 的插件。"

        lines = [f"\U0001f4e6 \u5df2\u52a0\u8f7d {len(catalog)} \u4e2a\u63d2\u4ef6\uff08\u63d0\u4f9b LLM \u5de5\u5177\uff09\uff1a\n"]

        for info in catalog:
            status = "\u2705" if info["activated"] else "\u23f8\ufe0f"
            reserved_tag = " (\u5185\u7f6e)" if info["reserved"] else ""
            lines.append(f"  {status} **{info['name']}**{reserved_tag}")
            if info["author"]:
                lines.append(f"     \u4f5c\u8005: {info['author']}")
            if info["version"]:
                lines.append(f"     \u7248\u672c: {info['version']}")
            if info["desc"]:
                lines.append(f"     \u63cf\u8ff0: {info['desc']}")

            lines.append(f"     LLM \u5de5\u5177 ({len(info['tools'])}):")
            for tool in info["tools"]:
                lines.append(f"       - `{tool['name']}`: {tool['description']}")
            lines.append("")

            if info["commands"]:
                lines.append(f"     \u547d\u4ee4 ({len(info['commands'])}):")
                for cmd in info["commands"][:10]:
                    lines.append(f"       - {cmd}")
                if len(info["commands"]) > 10:
                    lines.append(f"       ... \u8fd8\u6709 {len(info['commands']) - 10} \u4e2a")
                lines.append("")

        return "\n".join(lines)

    async def _call_plugin_handler(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        tool_name: str,
        tool_args: str = "",
    ) -> str:
        from astrbot.core.provider.register import llm_tools

        catalog = self._build_plugin_catalog()

        # 精确匹配 > 大小写不敏感匹配 > 子串匹配（降级）
        plugin_info = None
        for info in catalog:
            if info["name"] == plugin_name:
                plugin_info = info
                break

        if not plugin_info:
            for info in catalog:
                if (info.get("name") or "").lower() == plugin_name.lower():
                    plugin_info = info
                    break

        if not plugin_info:
            matches = [
                info for info in catalog
                if plugin_name.lower() in (info.get("name") or "").lower()
            ]
            if matches:
                plugin_info = matches[0]
                plugin_name = plugin_info["name"]
            else:
                return f"\u672a\u627e\u5230\u63d2\u4ef6 '{plugin_name}'\u3002\u8bf7\u4f7f\u7528 `list_plugins` \u67e5\u770b\u63d0\u4f9b LLM \u5de5\u5177\u7684\u63d2\u4ef6\u3002"

        # 工具只精确匹配
        tool_info = None
        for t in plugin_info["tools"]:
            if t["name"] == tool_name:
                tool_info = t
                break

        if not tool_info:
            tool_names = [t["name"] for t in plugin_info["tools"]]
            return f"\u63d2\u4ef6 '{plugin_info['name']}' \u4e2d\u672a\u627e\u5230\u5de5\u5177 '{tool_name}'\u3002\n\u53ef\u7528\u5de5\u5177: {', '.join(tool_names)}"

        func_tool = llm_tools.get_func(tool_info["name"])
        if not func_tool or not func_tool.active:
            return f"\u5de5\u5177 '{tool_name}' \u672a\u6fc0\u6d3b\u6216\u4e0d\u5b58\u5728\u3002"

        parsed_args = self._parse_args(tool_info["parameters"], tool_args)
        if isinstance(parsed_args, str):
            return parsed_args

        try:
            result = await func_tool.handler(event, **parsed_args)
            if result is None:
                return f"\u5de5\u5177 '{tool_name}' \u6267\u884c\u5b8c\u6210\u3002"
            if hasattr(result, "message"):
                return str(result.message)
            return str(result)
        except Exception as e:
            logger.exception("\u8c03\u7528\u63d2\u4ef6\u5de5\u5177\u5931\u8d25: %s", tool_name)
            return f"\u8c03\u7528\u63d2\u4ef6\u5de5\u5177 '{tool_name}' \u5931\u8d25: {e!s}"

    def _parse_args(self, parameters: dict, tool_args: str) -> dict | str:
        """Parse tool arguments.

        Supports:
        - JSON format: '{"key": "value"}'
        - Simple string (assigned to the first parameter)
        - Empty string (returns empty dict)
        """
        if not tool_args or not tool_args.strip():
            return {}

        try:
            parsed = json.loads(tool_args)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        stripped = tool_args.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return f"\u53c2\u6570\u89e3\u6790\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 JSON \u683c\u5f0f: {tool_args}"
        if stripped.startswith("[") and stripped.endswith("]"):
            return f"\u53c2\u6570\u89e3\u6790\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5 JSON \u683c\u5f0f: {tool_args}"

        props = parameters.get("properties", {})
        if props:
            first_key = next(iter(props), None)
            if first_key:
                return {first_key: tool_args}

        return {}

    async def _search_plugin_tools_handler(
        self,
        event: AstrMessageEvent,
        keywords: str,
    ) -> str:
        catalog = self._build_plugin_catalog()

        keywords_lower = keywords.lower()
        results = []
        for info in catalog:
            for tool in info["tools"]:
                if (keywords_lower in tool["name"].lower() or
                    keywords_lower in tool["description"].lower()):
                    results.append((info, tool))

        if not results:
            return f"\u672a\u627e\u5230\u5339\u914d\u5173\u952e\u8bcd '{keywords}' \u7684\u5de5\u5177\u3002\n\n\u8bf7\u4f7f\u7528 `list_plugins` \u67e5\u770b\u6240\u6709\u63d0\u4f9b LLM \u5de5\u5177\u7684\u63d2\u4ef6\u3002"

        lines = [f"\U0001f50d \u627e\u5230 {len(results)} \u4e2a\u5339\u914d\u7684\u5de5\u5177\uff1a\n"]
        for info, tool in results:
            lines.append(f"  **{info['name']}** / `{tool['name']}`")
            lines.append(f"    {tool['description']}")
            lines.append("")

        return "\n".join(lines)

    # ---- IM command handlers ----

    @filter.command("list_plugins")
    async def cmd_list_plugins(self, event: AstrMessageEvent) -> None:
        """\u5217\u51fa\u6240\u6709\u63d0\u4f9b LLM \u5de5\u5177\u7684\u63d2\u4ef6"""
        output = await self._list_plugins_handler(event)
        event.set_result(event.message_chain.message(output).use_t2i(False))

    @filter.command("search_plugins")
    async def cmd_search_plugins(
        self, event: AstrMessageEvent, keywords: str = ""
    ) -> None:
        """\u641c\u7d22\u63d0\u4f9b LLM \u5de5\u17f7\u5177\u7684\u63d2\u4ef6\u5de5\u5177"""
        if not keywords.strip():
            event.set_result(
                event.message_chain.message("\u8bf7\u63d0\u4f9b\u641c\u7d22\u5173\u952e\u8bcd\uff0c\u4f8b\u5982\uff1a/search_plugins \u7ffb\u8bd1").use_t2i(False)
            )
            return

        output = await self._search_plugin_tools_handler(event, keywords)
        event.set_result(event.message_chain.message(output).use_t2i(False))

    # ---- Cache invalidation hooks ----

    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata: star.StarMetadata) -> None:
        """插件加载后自动清除缓存"""
        self._invalidate_cache()
        logger.debug(f"Plugin {metadata.name} loaded, cleared discovery cache")

    @filter.on_plugin_unloaded()
    async def _on_plugin_unloaded(self, metadata: star.StarMetadata) -> None:
        """插件卸载后自动清除缓存"""
        self._invalidate_cache()
        logger.debug(f"Plugin {metadata.name} unloaded, cleared discovery cache")
