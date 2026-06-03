"""插件工具化（Plugin Toolifier）。

功能：
1. 向 Agent 注册三个核心 LLM Tool：
   - list_plugins: 列出所有有 LLM Tool 的插件
   - call_plugin: 按名称调用指定插件的 LLM Tool
   - search_plugin_tools: 按关键词搜索可用工具
2. 同时提供 IM 命令 /list_plugins 和 /search_plugins 供用户直接调用。

设计说明：
- 只暴露有 LLM Tool（function-calling）的插件，过滤纯辅助性质的插件（如分段发送、消息装饰）。
- 纯事件驱动缓存：插件加载/卸载、工具激活/停用、插件启停时自动刷新，无定时 TTL。

缓存失效覆盖场景（6 个入口全部覆盖）：
- 插件安装/加载 → on_plugin_loaded 事件
- 插件卸载 → on_plugin_unloaded 事件
- 工具单独停用/激活 → FunctionToolManager 猴子补丁
- 插件禁用/启用 → PluginManager 猴子补丁
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.all import Star, Context
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star import star_map as _star_map, star_registry


class Main(Star):
    """Agent 插件发现插件。

    自动将所有已加载插件的 LLM Tool 暴露给 Agent，
    使 Agent 能感知、查询、调用任何插件的功能。
    """

    author = "Chen"
    name = "astrbot_plugin_toolifier"
    desc = "插件工具化 — 自动将 AstrBot 插件注册为 LLM 工具，使 Agent 能发现、查询、调用所有插件的能力。"

    def __init__(self, context: Context, config: dict = None) -> None:
        super().__init__(context, config)
        # Cached catalog: None means cache is stale
        self._catalog_cache: list[dict[str, Any]] | None = None
        self._patched = False
        # Store original references for patch restoration
        self._orig_activate: Any = None
        self._orig_deactivate: Any = None
        self._orig_pm_turn_off: Any = None
        self._orig_pm_turn_on: Any = None

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
        # 设置 handler_module_path，确保工具与插件正确关联
        llm_tools.func_list[-1].handler_module_path = "data.plugins.astrbot_plugin_toolifier.main"

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
        llm_tools.func_list[-1].handler_module_path = "data.plugins.astrbot_plugin_toolifier.main"

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
        llm_tools.func_list[-1].handler_module_path = "data.plugins.astrbot_plugin_toolifier.main"

        logger.info(
            "Plugin Toolifier: registered list_plugins, call_plugin, search_plugin_tools tools"
        )

        # Monkey-patch AstrBot core methods to intercept tool/plugin state changes
        # that do NOT emit on_plugin_loaded / on_plugin_unloaded events.
        self._monkey_patch()

    # Lifecycle cleanup to restore monkey patches
    async def terminate(self) -> None:
        """插件卸载/停用时还原猴子补丁。"""
        # Restore FunctionToolManager patches
        if self._orig_activate is not None:
            from astrbot.core.provider.register import llm_tools
            llm_tools.activate_llm_tool = self._orig_activate
            llm_tools.deactivate_llm_tool = self._orig_deactivate
            logger.info("Plugin Toolifier: restored FunctionToolManager patches")

        # Restore PluginManager patches
        if self._orig_pm_turn_off is not None:
            from astrbot.core.star.star_manager import PluginManager
            PluginManager.turn_off_plugin = self._orig_pm_turn_off
            PluginManager.turn_on_plugin = self._orig_pm_turn_on
            logger.info("Plugin Toolifier: restored PluginManager patches")

    # ---- Monkey-patching for complete cache invalidation coverage ----

    def _monkey_patch(self) -> None:
        """Patch FunctionToolManager and PluginManager methods for cache invalidation.

        AstrBot's event system does not emit events for tool-level toggle or
        plugin enable/disable operations. This method monkey-patches the relevant
        methods so that every state mutation triggers cache invalidation.

        Covered scenarios:
        1. Tool toggle: activate_llm_tool / deactivate_llm_tool
           (called from WebUI POST /api/tools/toggle)
        2. Plugin toggle: turn_on_plugin / turn_off_plugin
           (called from WebUI POST /api/plugin/on and /api/plugin/off)

        Patches are applied once (tracked by _patched flag) to avoid duplication.
        """
        if self._patched:
            return

        # Patch 1: FunctionToolManager.activate_llm_tool / deactivate_llm_tool
        from astrbot.core.provider.register import llm_tools

        # Save original references to instance attributes for restore
        self._orig_activate = llm_tools.activate_llm_tool
        self._orig_deactivate = llm_tools.deactivate_llm_tool

        def _wrap_activate(name: str, star_map: dict) -> bool:
            """Wrap activate_llm_tool to invalidate cache on success."""
            result = self._orig_activate(name, star_map)
            if result:
                self._invalidate_cache()
            return result

        def _wrap_deactivate(name: str) -> bool:
            """Wrap deactivate_llm_tool to invalidate cache on success."""
            result = self._orig_deactivate(name)
            if result:
                self._invalidate_cache()
            return result

        llm_tools.activate_llm_tool = _wrap_activate
        llm_tools.deactivate_llm_tool = _wrap_deactivate

        # Patch 2: PluginManager.turn_on_plugin / turn_off_plugin
        # These are async methods, so wrapped versions must also be async.
        from astrbot.core.star.star_manager import PluginManager

        # Save original references
        self._orig_pm_turn_off = PluginManager.turn_off_plugin
        self._orig_pm_turn_on = PluginManager.turn_on_plugin

        async def _pm_wrap_turn_off(self_inst: PluginManager, plugin_name: str) -> None:
            """Wrap turn_off_plugin to invalidate cache after the plugin is turned off."""
            await self._orig_pm_turn_off(self_inst, plugin_name)
            self._invalidate_cache()
            logger.debug(f"Plugin {plugin_name} turned off, cleared discovery cache")

        async def _pm_wrap_turn_on(self_inst: PluginManager, plugin_name: str) -> None:
            """Wrap turn_on_plugin to invalidate cache after the plugin is turned on."""
            await self._orig_pm_turn_on(self_inst, plugin_name)
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

        catalog: dict[str, dict[str, Any]] = {}

        # 1. Collect plugin basic info from star_registry
        for metadata in star_registry:
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
                    "reserved": getattr(metadata, "reserved", False),
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
        """Return a formatted string listing all plugins with active LLM tools.

        The event parameter is required by AstrBot's tool execution system.
        Used both by the Agent tool and the IM command handler.
        """
        catalog = self._build_plugin_catalog()

        if not catalog:
            return "当前没有加载任何提供 LLM Tool 的插件。"

        lines = [f"\U0001f4e6 已加载 {len(catalog)} 个插件（提供 LLM 工具）：\n"]

        for info in catalog:
            status = "✅" if info["activated"] else "⏸️"
            reserved_tag = " (内置)" if info["reserved"] else ""
            lines.append(f"  {status} **{info['name']}**{reserved_tag}")
            if info["author"]:
                lines.append(f"     作者: {info['author']}")
            if info["version"]:
                lines.append(f"     版本: {info['version']}")
            if info["desc"]:
                lines.append(f"     描述: {info['desc']}")

            lines.append(f"     LLM 工具 ({len(info['tools'])}):")
            for tool in info["tools"]:
                lines.append(f"       - `{tool['name']}`: {tool['description']}")
            lines.append("")

            if info["commands"]:
                lines.append(f"     命令 ({len(info['commands'])}):")
                for cmd in info["commands"][:10]:
                    lines.append(f"       - {cmd}")
                if len(info["commands"]) > 10:
                    lines.append(f"       ... 还有 {len(info['commands']) - 10} 个")
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
                return f"未找到插件 '{plugin_name}'。请使用 `list_plugins` 查看提供 LLM 工具的插件。"

        # 工具名匹配：精确匹配 > 大小写不敏感匹配 > 子串匹配
        tool_info = None
        for t in plugin_info["tools"]:
            if t["name"] == tool_name:
                tool_info = t
                break

        if not tool_info:
            for t in plugin_info["tools"]:
                if t["name"].lower() == tool_name.lower():
                    tool_info = t
                    break

        if not tool_info:
            matches = [
                t for t in plugin_info["tools"]
                if tool_name.lower() in t["name"].lower()
            ]
            if matches:
                tool_info = matches[0]
                tool_name = tool_info["name"]
            else:
                tool_names = [t["name"] for t in plugin_info["tools"]]
                return f"插件 '{plugin_info['name']}' 中未找到工具 '{tool_name}'。\n可用工具: {', '.join(tool_names)}"

        func_tool = llm_tools.get_func(tool_info["name"])
        if not func_tool or not func_tool.active:
            return f"工具 '{tool_name}' 未激活或不存在。"

        parsed_args = self._parse_args(tool_info["parameters"], tool_args)
        if isinstance(parsed_args, str):
            return parsed_args

        try:
            return await self._invoke_func_tool(event, func_tool, parsed_args)
        except Exception as e:
            logger.exception("调用插件工具失败: %s", tool_name)
            return f"调用插件工具 '{tool_name}' 失败: {e!s}"

    async def _invoke_func_tool(
        self,
        event: AstrMessageEvent,
        func_tool: Any,
        kwargs: dict,
    ) -> str:
        """Invoke a FunctionTool handler, supporting both coroutine and async generator.

        Returns the result as a string suitable for LLM tool output.
        """
        handler = func_tool.handler
        if handler is None:
            return f"工具 '{func_tool.name}' 没有实现 handler。"

        # Check if handler is an async generator function
        if inspect.isasyncgenfunction(handler):
            # Async generator: iterate through yields
            results = []
            gen = handler(event, **kwargs)
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            gen.__anext__(),
                            timeout=30.0,
                        )
                        if chunk is not None:
                            if hasattr(chunk, "message"):
                                results.append(str(chunk.message))
                            else:
                                results.append(str(chunk))
                    except StopAsyncIteration:
                        break
            except asyncio.TimeoutError:
                return f"工具 '{func_tool.name}' 执行超时。"
            finally:
                # Guard aclose() against None or missing attribute
                if gen is not None and hasattr(gen, "aclose"):
                    gen.aclose()
            if not results:
                return f"工具 '{func_tool.name}' 执行完成。"
            return "\n".join(results)

        # Exception handling is done by the caller (_call_plugin_handler)
        result = await handler(event, **kwargs)
        if result is None:
            return f"工具 '{func_tool.name}' 执行完成。"
        if hasattr(result, "message"):
            return str(result.message)
        return str(result)

    def _parse_args(self, parameters: dict, tool_args: str) -> dict | str:
        """Parse tool arguments.

        Supports:
        - JSON dict format: '{"key": "value"}'
        - JSON list format: '["val1", "val2"]'
        - Simple string (assigned to the first parameter, with type coercion)
        - Empty string (returns empty dict)
        """
        if not tool_args or not tool_args.strip():
            return {}

        # Normalize parameters: handle both OpenAI style ({properties, required})
        # and AstrBot decorator style ({type, name, description})
        props = parameters.get("properties", {})
        if not props:
            # Some tools may use a different schema format (e.g. list of dicts)
            # Check if parameters is already a list of parameter definitions
            if isinstance(parameters, list):
                if parameters:
                    first_param = parameters[0]
                    first_key = first_param.get("name", "arg")
                    return {first_key: tool_args.strip()}
                return {}
            return {}

        # Try parsing as JSON dict
        try:
            parsed = json.loads(tool_args)
            if isinstance(parsed, dict):
                return self._validate_params(parsed, parameters)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try parsing as JSON list -> map to first parameter as list
        try:
            parsed = json.loads(tool_args)
            if isinstance(parsed, list):
                if props:
                    first_key = next(iter(props), None)
                    if first_key:
                        return {first_key: parsed}
        except (json.JSONDecodeError, ValueError):
            pass

        stripped = tool_args.strip()
        # Check for invalid JSON objects/arrays and give helpful errors
        if stripped.startswith("{") and stripped.endswith("}"):
            return f"参数解析失败，请检查 JSON 格式: {tool_args}"
        if stripped.startswith("[") and stripped.endswith("]"):
            return f"参数解析失败，请检查 JSON 格式: {tool_args}"

        # Simple string -> assign to first parameter with type coercion
        first_key = next(iter(props), None)
        if first_key:
            prop_schema = props.get(first_key, {})
            prop_type = prop_schema.get("type", "")
            value = tool_args
            # Type coercion for string fallback
            if prop_type == "int":
                try:
                    value = int(tool_args)
                except ValueError:
                    pass
            elif prop_type == "float":
                try:
                    value = float(tool_args)
                except ValueError:
                    pass
            elif prop_type == "bool":
                value = tool_args.lower() in ("true", "yes", "1")
            return {first_key: value}

        return {}

    @staticmethod
    def _validate_params(parsed: dict, parameters: dict) -> dict:
        """Validate and filter parsed parameters against the tool's parameter schema.

        Only passes keys that exist in the tool's 'properties' definition,
        preventing TypeError from unexpected keyword arguments.
        """
        allowed_keys = set(parameters.get("properties", {}).keys())
        if not allowed_keys:
            return parsed
        # Keep only parameters that are defined in the tool schema
        filtered = {k: v for k, v in parsed.items() if k in allowed_keys}
        return filtered

    async def _search_plugin_tools_handler(
        self,
        event: AstrMessageEvent,
        keywords: str,
    ) -> str:
        catalog = self._build_plugin_catalog()

        keywords_normalized = keywords.lower()
        results = []
        for info in catalog:
            for tool in info["tools"]:
                if (keywords_normalized in tool["name"].lower() or
                    keywords_normalized in tool["description"].lower()):
                    results.append((info, tool))

        if not results:
            return f"未找到匹配关键词 '{keywords}' 的工具。\n\n请使用 `list_plugins` 查看所有提供 LLM 工具的插件。"

        lines = [f"\U0001f50d 找到 {len(results)} 个匹配的工具：\n"]
        for info, tool in results:
            lines.append(f"  **{info['name']}** / `{tool['name']}`")
            lines.append(f"    {tool['description']}")
            lines.append("")

        return "\n".join(lines)

    # ---- IM command handlers ----

    @filter.command("list_plugins")
    async def cmd_list_plugins(self, event: AstrMessageEvent):
        """列出所有提供 LLM 工具的插件"""
        output = await self._list_plugins_handler(event)
        yield event.plain_result(output).use_t2i(False)

    @filter.command("search_plugins")
    async def cmd_search_plugins(
        self, event: AstrMessageEvent, keywords: str = ""
    ):
        """搜索提供 LLM 工具的插件工具"""
        if not keywords.strip():
            yield event.plain_result("请提供搜索关键词，例如：/search_plugins 翻译").use_t2i(False)
            return

        output = await self._search_plugin_tools_handler(event, keywords)
        yield event.plain_result(output).use_t2i(False)

    # ---- Cache invalidation hooks ----

    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata) -> None:
        """插件加载后自动清除缓存"""
        self._invalidate_cache()
        logger.debug(f"Plugin {metadata.name} loaded, cleared discovery cache")

    @filter.on_plugin_unloaded()
    async def _on_plugin_unloaded(self, metadata) -> None:
        """插件卸载后自动清除缓存"""
        self._invalidate_cache()
        logger.debug(f"Plugin {metadata.name} unloaded, cleared discovery cache")
