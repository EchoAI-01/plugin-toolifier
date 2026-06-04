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
import time
import uuid

from astrbot.core.star import star_map as _star_map, star_registry
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.message.components import Plain


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

        Primary data source: star_handlers_registry (OnCallingFuncToolEvent handlers).
        Each @llm_tool decorated function creates a handler with a fixed
        handler_module_path that survives functools.partial wrapping.

        Parameters are obtained from llm_tools.func_list (registered at decoration
        time with full schema), then matched by tool name.

        Uses event-driven cache: cache is rebuilt only when stale (plugin loaded,
        unloaded, tool activated/deactivated, or plugin turned on/off).
        """
        if self._catalog_cache is not None:
            return self._catalog_cache

        from astrbot.core.provider.register import llm_tools
        from astrbot.core.star.register.star_handler import EventType
        from astrbot.core.star.star_handler import star_handlers_registry

        catalog: dict[str, dict[str, Any]] = {}

        # Build a mapping from module_path prefix to plugin metadata.
        # This handles both main module (e.g. "data.plugins.xxx.main")
        # and sub-modules (e.g. "data.plugins.xxx.tools.search") since
        # star_map only has the main module key.
        prefix_map: dict[str, Any] = {}
        for metadata in star_registry:
            mp = metadata.module_path
            if not mp:
                continue
            prefix_map[mp] = metadata

        # 1. Collect LLM Tools from star_handlers_registry.
        for handler_md in star_handlers_registry:
            if handler_md.event_type != EventType.OnCallingFuncToolEvent:
                continue

            mp = handler_md.handler_module_path
            if not mp:
                continue

            # Find the owning plugin via prefix match.
            # The handler_module_path might be a sub-module like
            # "data.plugins.xxx.tools.search", while star_map only has
            # "data.plugins.xxx.main". We need to find the best matching
            # prefix: longest matching prefix first, then fall back to
            # checking if mp starts with any known prefix.
            meta: Any = None
            # First try exact match
            if mp in prefix_map:
                meta = prefix_map[mp]
            else:
                # Try prefix match: find the longest known prefix that mp starts with
                best_match = ""
                for known_mp, known_meta in prefix_map.items():
                    if mp.startswith(known_mp + ".") and len(known_mp) > len(best_match):
                        best_match = known_mp
                        meta = known_meta
                    elif mp == known_mp:
                        meta = known_meta
                        break

            if not meta or not meta.name:
                continue

            # Skip if the owning plugin is inactive
            if not meta.activated and not getattr(meta, "reserved", False):
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
                    "_module_path": meta.module_path,  # for call_plugin precise matching
                    "tools": [],
                    "commands": [],
                }

            tool_name = handler_md.handler_name

            # Check if we already added this tool (skip duplicates)
            if any(t["name"] == tool_name for t in catalog[name_key]["tools"]):
                continue

            # Get full parameters from func_list (registered at decoration time)
            params: dict = {}
            for ft in llm_tools.func_list:
                if ft.name == tool_name:
                    params = ft.parameters or {}
                    break

            catalog[name_key]["tools"].append({
                "name": tool_name,
                "description": handler_md.desc or "",
                "parameters": params,
            })

        # 2. Collect command info from handler registry
        for handler_md in star_handlers_registry:
            mp = handler_md.handler_module_path
            if not mp:
                continue

            # Same prefix matching for commands
            meta: Any = None
            if mp in prefix_map:
                meta = prefix_map[mp]
            else:
                best_match = ""
                for known_mp, known_meta in prefix_map.items():
                    if mp.startswith(known_mp + ".") and len(known_mp) > len(best_match):
                        best_match = known_mp
                        meta = known_meta

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

        # 4. Discover @filter.command handlers and expose them as LLM tools.
        #    This bridges the gap: most AstrBot plugins register @filter.command
        #    (not @llm_tool), so they are invisible to the Agent. Here we wrap
        #    each command handler into a catalog entry that Agent can call via
        #    call_plugin with tool_name "cmd_<command_name>".
        for handler_md in star_handlers_registry:
            # Only process AdapterMessageEvent handlers (i.e. command/event handlers)
            if handler_md.event_type != EventType.AdapterMessageEvent:
                continue

            mp = handler_md.handler_module_path
            if not mp:
                continue

            # Prefix match for sub-module handlers
            meta: Any = None
            if mp in prefix_map:
                meta = prefix_map[mp]
            else:
                best_match = ""
                for known_mp, known_meta in prefix_map.items():
                    if mp.startswith(known_mp + ".") and len(known_mp) > len(best_match):
                        best_match = known_mp
                        meta = known_meta

            if not meta or not meta.name:
                continue

            # Skip if the owning plugin is inactive (unless reserved)
            if not meta.activated and not getattr(meta, "reserved", False):
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
                    "_module_path": meta.module_path,
                    "tools": [],
                    "commands": [],
                }

            # Find CommandFilter in event filters
            for ef in handler_md.event_filters:
                if not hasattr(ef, "command_name"):
                    continue
                cmd_name = ef.command_name
                parent_names = getattr(ef, "parent_command_names", None) or []
                for pn in parent_names:
                    if pn:
                        cmd_name = f"{pn} {cmd_name}"

                # Build function args schema from handler_params
                func_args = _build_func_args_from_command(ef)

                # Tool name prefixed with "cmd_" to distinguish from @llm_tool
                tool_name = f"cmd_{cmd_name}"

                # Skip duplicate tools
                if any(t["name"] == tool_name for t in catalog[name_key]["tools"]):
                    continue

                catalog[name_key]["tools"].append({
                    "name": tool_name,
                    "description": handler_md.desc or f"Execute the command /{cmd_name}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            p["name"]: p for p in func_args
                        },
                        "required": [p["name"] for p in func_args if p.get("required", False)],
                    },
                    "_handler_md": handler_md,
                    "_cmd_filter": ef,
                    "_is_command": True,
                })

        # 5. Filter: only include plugins that have at least one tool
        result = []
        for info in catalog.values():
            if not info["activated"] and not info["reserved"]:
                continue
            if not info["tools"]:
                continue
            result.append(info)

        self._catalog_cache = result
        return result

# ---- Helper functions for command tool exposure ----

def _build_func_args_from_command(cmd_filter) -> list[dict]:
    """Convert command handler parameters to JSON Schema parameter list.
    
    Reads from CommandFilter.handler_params which maps param_name -> 
    type_or_default_value.
    """
    import types
    import typing
    
    args = []
    for param_name, type_or_default in cmd_filter.handler_params.items():
        param_info = {"name": param_name}
        
        if isinstance(type_or_default, type):
            # Type annotation (e.g. str, int)
            type_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
            param_info["type"] = type_map.get(type_or_default.__name__, "string")
            param_info["required"] = True
        elif isinstance(type_or_default, types.UnionType) or typing.get_origin(type_or_default) is typing.Union:
            # Optional[T] etc
            param_info["type"] = "string"
            param_info["required"] = False
        else:
            # Has default value
            param_info["type"] = "string"
            param_info["required"] = False
        
        param_info["description"] = f"Parameter: {param_name}"
        args.append(param_info)
    
    return args


def _extract_text(result) -> str:
    """Extract plain text from various handler result types."""
    if isinstance(result, str):
        return result
    if isinstance(result, MessageEventResult):
        parts = []
        for comp in (result.chain or []):
            if hasattr(comp, "text"):
                parts.append(comp.text)
            else:
                parts.append(str(comp))
        return "\n".join(parts)
    if hasattr(result, "message"):
        return str(result.message)
    return str(result)


def _create_fake_event(
    message_str: str,
    original_event: AstrMessageEvent,
) -> AstrMessageEvent:
    """Create a minimal AstrMessageEvent for command invocation.
    
    This simulates a user sending the command text directly to the bot,
    allowing command handlers to work without modification.
    """
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent
    
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = original_event.get_self_id()
    msg.session_id = original_event.session_id
    msg.message_id = f"toolifier_{uuid.uuid4().hex[:8]}"
    msg.sender = MessageMember(
        user_id=original_event.get_sender_id() or "toolifier_agent",
        nickname="Agent",
    )
    msg.message = [Plain(message_str)]
    msg.message_str = message_str
    msg.timestamp = int(time.time())
    
    fake_event = WebChatMessageEvent(
        message_str, msg, original_event.platform_meta, original_event.session_id
    )
    fake_event.is_wake = True
    fake_event.is_at_or_wake_command = True
    return fake_event


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
        from astrbot.core.star.register.star_handler import EventType
        from astrbot.core.star.star_handler import star_handlers_registry

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

        # Resolve the actual FunctionTool by matching both name AND module_path.
        # Use star_handlers_registry for precise matching (handler_module_path
        # is fixed at decoration time and not affected by functools.partial).
        # Supports prefix match for sub-module handlers.
        target_module_path: str | None = None
        for info in catalog:
            if info["name"] == plugin_info["name"]:
                target_module_path = info.get("_module_path")
                break

        target_ft = None
        if target_module_path:
            for handler_md in star_handlers_registry:
                if handler_md.event_type != EventType.OnCallingFuncToolEvent:
                    continue
                if handler_md.handler_name != tool_name:
                    continue
                mp = handler_md.handler_module_path
                if not mp:
                    continue
                if mp == target_module_path or mp.startswith(target_module_path + "."):
                    for ft in llm_tools.func_list:
                        if ft.name == tool_name and getattr(ft, "active", True):
                            target_ft = ft
                            break
                    break

        parsed_args = self._parse_args(tool_info["parameters"], tool_args)
        if isinstance(parsed_args, str):
            return parsed_args

        if target_ft is None:
            # Check if this is a command tool (cmd_ prefix)
            if tool_name.startswith("cmd_") and "_is_command" in tool_info:
                handler_md = tool_info.get("_handler_md")
                cmd_filter = tool_info.get("_cmd_filter")
                if handler_md and cmd_filter:
                    return await self._invoke_command_handler(
                        event, handler_md, cmd_filter, parsed_args,
                    )
            return f"工具 '{tool_name}' 所属的插件工具未注册。"

        try:
            return await self._invoke_func_tool(event, target_ft, parsed_args)
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

    async def _invoke_command_handler(
        self,
        event: AstrMessageEvent,
        handler_md: Any,
        cmd_filter: Any,
        parsed_args: dict,
    ) -> str:
        """Invoke a command handler by simulating a message event.
        
        Builds a fake message like "/command_name arg1 arg2", creates a
        minimal WebChatMessageEvent, and calls the handler directly.
        """
        # Build fake message string: /cmd arg1 arg2 ...
        cmd_parts = [cmd_filter.command_name]
        for k, v in parsed_args.items():
            if isinstance(v, list):
                cmd_parts.append(" ".join(str(x) for x in v))
            elif isinstance(v, dict):
                cmd_parts.append(json.dumps(v))
            else:
                cmd_parts.append(str(v))
        fake_message = " ".join(cmd_parts)
        
        # Create simulated event
        fake_event = _create_fake_event(fake_message, event)
        
        # Call the handler
        handler = handler_md.handler
        try:
            if inspect.isasyncgenfunction(handler):
                # Async generator (yields MessageEventResult)
                results = []
                gen = handler(fake_event, **parsed_args)
                try:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                gen.__anext__(), timeout=30.0,
                            )
                            results.append(_extract_text(chunk))
                        except asyncio.TimeoutError:
                            return f"命令 '{cmd_filter.command_name}' 执行超时。"
                        except StopAsyncIteration:
                            break
                finally:
                    if hasattr(gen, "aclose"):
                        gen.aclose()
                return "\n".join(results) if results else "命令执行完成。"
            else:
                # Regular async function
                result = await handler(fake_event, **parsed_args)
                if result is None:
                    return "命令执行完成。"
                return _extract_text(result)
        except Exception as e:
            logger.exception("命令执行失败: %s", cmd_filter.command_name)
            return f"命令 '{cmd_filter.command_name}' 执行失败: {e!s}"


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
