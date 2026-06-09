"""Plugin Toolifier.

Expose selected AstrBot command plugins as LLM-callable tools. The bridge keeps
the original command filters in the call path so natural-language invocation
does not silently bypass plugin permissions.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import shlex
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import sp
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata, star_registry
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.core.star.star_handler import star_handlers_registry

PLUGIN_MODULE_PATH = "data.plugins.astrbot_plugin_toolifier.main"
PLUGIN_NAME = "astrbot_plugin_toolifier"
META_TOOL_NAMES = {
    "list_plugin_commands",
    "search_plugin_commands",
    "call_plugin_command",
    "list_plugins",
    "search_plugin_tools",
    "call_plugin",
}
META_TOOL_REQUIRED = {
    "search_plugin_commands": ["keywords"],
    "call_plugin_command": ["plugin_name", "command_name"],
    "search_plugin_tools": ["keywords"],
    "call_plugin": ["plugin_name", "tool_name"],
}


@dataclass(frozen=True)
class CommandParameter:
    name: str
    schema_type: str
    required: bool
    description: str
    raw_spec: Any

    def as_func_arg(self) -> dict[str, Any]:
        schema = {
            "type": self.schema_type,
            "name": self.name,
            "description": self.description,
        }
        if self.schema_type == "array":
            schema["items"] = {"type": "string"}
        return schema


@dataclass
class CommandEntry:
    plugin_name: str
    plugin_desc: str
    plugin_author: str | None
    plugin_version: str | None
    module_path: str
    handler: StarHandlerMetadata
    command_filter: CommandFilter
    command_name: str
    aliases: list[str]
    command_id: str
    tool_name: str
    description: str
    parameters: list[CommandParameter] = field(default_factory=list)
    is_admin: bool = False
    reserved: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.plugin_name}:{self.command_name}"


class Main(Star):
    """Discover command plugins and expose them to the Agent safely."""

    author = "Chen"
    name = PLUGIN_NAME
    desc = "Expose selected AstrBot command plugins as LLM-callable tools."

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self._registered_command_tools: set[str] = set()

    async def initialize(self) -> None:
        if self._registration_mode() in {"meta", "both"}:
            self._register_meta_tools()
        else:
            self._remove_meta_tools()
        await self._sync_command_tools()
        logger.info("Plugin Toolifier initialized.")

    async def terminate(self) -> None:
        self._remove_registered_command_tools()
        self._remove_meta_tools()

    # ---- LLM tool registration ----

    def _register_meta_tools(self) -> None:
        from astrbot.core.provider.register import llm_tools

        inactivated_tools = set(
            sp.get(
                "inactivated_llm_tools",
                [],
                scope="global",
                scope_id="global",
            ),
        )
        tool_specs = [
            (
                "list_plugin_commands",
                [],
                (
                    "List AstrBot plugin commands that are currently safe for the "
                    "Agent to call. Use this before choosing a plugin command."
                ),
                self._list_plugin_commands_handler,
            ),
            (
                "search_plugin_commands",
                [
                    {
                        "type": "string",
                        "name": "keywords",
                        "description": "Natural-language search keywords.",
                    },
                ],
                "Search callable AstrBot plugin commands by name and description.",
                self._search_plugin_commands_handler,
            ),
            (
                "call_plugin_command",
                [
                    {
                        "type": "string",
                        "name": "plugin_name",
                        "description": "Plugin name from list_plugin_commands.",
                    },
                    {
                        "type": "string",
                        "name": "command_name",
                        "description": (
                            "Command name, command id, alias, or generated tool name "
                            "from list_plugin_commands."
                        ),
                    },
                    {
                        "type": "string",
                        "name": "command_args",
                        "description": (
                            "Arguments as JSON object/list or plain text. Example: "
                            "{\"city\":\"Beijing\"}."
                        ),
                    },
                ],
                "Call a safe AstrBot plugin command and return its captured output.",
                self._call_plugin_command_handler,
            ),
            # Backward-compatible names for the original plugin contract.
            (
                "list_plugins",
                [],
                "Alias of list_plugin_commands.",
                self._list_plugin_commands_handler,
            ),
            (
                "search_plugin_tools",
                [
                    {
                        "type": "string",
                        "name": "keywords",
                        "description": "Natural-language search keywords.",
                    },
                ],
                "Alias of search_plugin_commands.",
                self._search_plugin_commands_handler,
            ),
            (
                "call_plugin",
                [
                    {
                        "type": "string",
                        "name": "plugin_name",
                        "description": "Plugin name from list_plugins.",
                    },
                    {
                        "type": "string",
                        "name": "tool_name",
                        "description": "Command/tool name from list_plugins.",
                    },
                    {
                        "type": "string",
                        "name": "tool_args",
                        "description": "Arguments as JSON object/list or plain text.",
                    },
                ],
                "Alias of call_plugin_command.",
                self._call_plugin_alias_handler,
            ),
        ]

        for name, args, desc, handler in tool_specs:
            llm_tools.add_func(name, args, desc, handler)
            func_tool = llm_tools.func_list[-1]
            func_tool.handler_module_path = PLUGIN_MODULE_PATH
            func_tool.active = name not in inactivated_tools
            if required_params := META_TOOL_REQUIRED.get(name):
                func_tool.parameters["required"] = required_params

    @staticmethod
    def _remove_tools_by_name(tool_names: set[str]) -> None:
        if not tool_names:
            return

        from astrbot.core.provider.register import llm_tools

        llm_tools.func_list = [
            tool
            for tool in llm_tools.func_list
            if not (
                tool.name in tool_names
                and tool.handler_module_path == PLUGIN_MODULE_PATH
            )
        ]

    def _remove_meta_tools(self) -> None:
        self._remove_tools_by_name(META_TOOL_NAMES)

    async def _sync_command_tools(self) -> None:
        mode = self._registration_mode()

        if mode == "meta":
            self._remove_registered_command_tools()
            return

        from astrbot.core.provider.register import llm_tools

        inactivated_tools = set(
            sp.get(
                "inactivated_llm_tools",
                [],
                scope="global",
                scope_id="global",
            ),
        )
        entries = self._build_command_catalog()
        next_tools: set[str] = set()
        for entry in entries:
            if not self._command_allowed_by_static_policy(entry):
                continue

            next_tools.add(entry.tool_name)
            llm_tools.add_func(
                entry.tool_name,
                [param.as_func_arg() for param in entry.parameters],
                (
                    f"Call AstrBot command /{entry.command_name} from plugin "
                    f"{entry.plugin_name}. {entry.description}"
                ),
                self._make_command_tool_handler(entry.command_id),
            )
            func_tool = llm_tools.func_list[-1]
            func_tool.handler_module_path = PLUGIN_MODULE_PATH
            func_tool.active = entry.tool_name not in inactivated_tools
            if required_params := [
                param.name for param in entry.parameters if param.required
            ]:
                func_tool.parameters["required"] = required_params

        for old_name in self._registered_command_tools - next_tools:
            self._remove_tools_by_name({old_name})
        self._registered_command_tools = next_tools

    def _remove_registered_command_tools(self) -> None:
        if not self._registered_command_tools:
            return

        self._remove_tools_by_name(self._registered_command_tools)
        self._registered_command_tools.clear()

    def _make_command_tool_handler(
        self,
        command_id: str,
    ) -> Callable[..., Awaitable[str]]:
        async def _handler(event: AstrMessageEvent, **kwargs: Any) -> str:
            return await self._call_command_by_id(event, command_id, kwargs)

        return _handler

    # ---- Catalog ----

    def _build_command_catalog(
        self,
        event: AstrMessageEvent | None = None,
        disabled_plugins: set[str] | None = None,
    ) -> list[CommandEntry]:
        entries: list[CommandEntry] = []
        metadata_by_prefix = self._metadata_by_prefix()
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent,
            plugins_name=event.plugins_name if event else None,
        )

        for handler in handlers:
            if not handler.enabled:
                continue
            metadata = self._find_metadata(
                handler.handler_module_path,
                metadata_by_prefix,
            )
            if not metadata or not metadata.name or not metadata.module_path:
                continue
            if not metadata.activated:
                continue
            if disabled_plugins and metadata.name in disabled_plugins:
                continue

            command_filters = [
                filter_
                for filter_ in handler.event_filters
                if isinstance(filter_, CommandFilter)
            ]
            if not command_filters:
                continue

            for command_filter in command_filters:
                command_names = command_filter.get_complete_command_names()
                if not command_names:
                    continue

                command_name = command_names[0]
                aliases = command_names[1:]
                parameters = self._build_parameters(command_filter)
                command_id = self._command_id(
                    metadata.module_path,
                    handler,
                    command_name,
                )
                entries.append(
                    CommandEntry(
                        plugin_name=metadata.name,
                        plugin_desc=metadata.desc or "",
                        plugin_author=metadata.author,
                        plugin_version=metadata.version,
                        module_path=metadata.module_path,
                        handler=handler,
                        command_filter=command_filter,
                        command_name=command_name,
                        aliases=aliases,
                        command_id=command_id,
                        tool_name=self._tool_name(
                            metadata.name,
                            command_name,
                            command_id,
                        ),
                        description=handler.desc or f"Execute /{command_name}",
                        parameters=parameters,
                        is_admin=self._is_admin_command(handler),
                        reserved=metadata.reserved,
                    )
                )

        return sorted(entries, key=lambda item: (item.plugin_name, item.command_name))

    async def _build_command_catalog_for_event(
        self,
        event: AstrMessageEvent,
    ) -> list[CommandEntry]:
        return self._build_command_catalog(
            event,
            await self._disabled_plugins_for_event(event),
        )

    @staticmethod
    def _metadata_by_prefix() -> dict[str, StarMetadata]:
        return {
            metadata.module_path: metadata
            for metadata in star_registry
            if metadata.module_path
        }

    @staticmethod
    def _find_metadata(
        module_path: str,
        metadata_by_prefix: dict[str, StarMetadata],
    ) -> StarMetadata | None:
        if module_path in metadata_by_prefix:
            return metadata_by_prefix[module_path]

        best_prefix = ""
        best_metadata = None
        for prefix, metadata in metadata_by_prefix.items():
            if module_path.startswith(prefix + ".") and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_metadata = metadata
        return best_metadata

    @staticmethod
    def _command_id(
        module_path: str,
        handler: StarHandlerMetadata,
        command_name: str,
    ) -> str:
        raw = f"{module_path}:{handler.handler_full_name}:{command_name}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _tool_name(plugin_name: str, command_name: str, command_id: str) -> str:
        raw_name = f"{plugin_name}_{command_name}".lower()
        safe_name = re.sub(r"[^a-z0-9_]+", "_", raw_name).strip("_")
        if not safe_name:
            safe_name = "command"
        return f"plugin_cmd_{safe_name}_{command_id}"

    @staticmethod
    def _is_admin_command(handler: StarHandlerMetadata) -> bool:
        for filter_ in handler.event_filters:
            if (
                isinstance(filter_, PermissionTypeFilter)
                and filter_.permission_type == PermissionType.ADMIN
            ):
                return True
        return False

    def _build_parameters(
        self,
        command_filter: CommandFilter,
    ) -> list[CommandParameter]:
        parameters = []
        for name, raw_spec in command_filter.handler_params.items():
            schema_type, required = self._schema_type_and_required(raw_spec)
            parameters.append(
                CommandParameter(
                    name=name,
                    schema_type=schema_type,
                    required=required,
                    description=(
                        f"Parameter `{name}` for /{command_filter.command_name}."
                    ),
                    raw_spec=raw_spec,
                )
            )
        return parameters

    @staticmethod
    def _schema_type_and_required(raw_spec: Any) -> tuple[str, bool]:
        if raw_spec is GreedyStr:
            return "string", False
        if isinstance(raw_spec, type):
            return _python_type_to_schema_type(raw_spec), True
        if (
            isinstance(raw_spec, types.UnionType)
            or typing.get_origin(raw_spec) is typing.Union
        ):
            non_none = [
                item for item in typing.get_args(raw_spec) if item is not type(None)
            ]
            if len(non_none) == 1 and isinstance(non_none[0], type):
                return _python_type_to_schema_type(non_none[0]), True
            return "string", True
        return _python_value_to_schema_type(raw_spec), False

    # ---- Safety policy ----

    def _command_allowed_by_static_policy(self, entry: CommandEntry) -> bool:
        if entry.plugin_name == PLUGIN_NAME:
            return False
        if entry.reserved and not self._setting("expose_builtin_commands", False):
            return False
        if entry.is_admin and not self._setting("allow_admin_commands", False):
            return False

        allow_plugins = self._setting_set("allow_plugins")
        deny_plugins = self._setting_set("deny_plugins")
        allow_commands = self._setting_set("allow_commands")
        deny_commands = self._setting_set("deny_commands")

        if entry.plugin_name in deny_plugins:
            return False
        if allow_plugins and entry.plugin_name not in allow_plugins:
            return False

        command_keys = self._command_policy_keys(entry)
        if deny_commands and command_keys & deny_commands:
            return False
        if allow_commands and not (command_keys & allow_commands):
            return False
        return True

    def _command_policy_keys(self, entry: CommandEntry) -> set[str]:
        return {
            entry.command_name,
            entry.command_id,
            entry.tool_name,
            f"{entry.plugin_name}:{entry.command_name}",
            f"{entry.plugin_name}.{entry.command_name}",
        }

    async def _validate_runtime_filters(
        self,
        event: AstrMessageEvent,
        entry: CommandEntry,
        command_args: dict[str, Any],
    ) -> tuple[bool, str, AstrMessageEvent, dict[str, Any]]:
        fake_event = self._create_command_event(event, entry, command_args)
        config = self.context.get_config(fake_event.unified_msg_origin)
        parsed_params: dict[str, Any] = {}

        for handler_filter in entry.handler.event_filters:
            try:
                if isinstance(handler_filter, CommandFilter):
                    if not handler_filter.filter(fake_event, config):
                        return False, "命令过滤器未通过。", fake_event, {}
                    parsed_params = dict(
                        fake_event.get_extra("parsed_params", {}) or {},
                    )
                    continue
                if isinstance(handler_filter, CommandGroupFilter):
                    if not handler_filter.filter(fake_event, config):
                        return (
                            False,
                            f"命令 /{entry.command_name} 的指令组条件未满足。",
                            fake_event,
                            {},
                        )
                    continue
                if isinstance(handler_filter, PermissionTypeFilter):
                    if not handler_filter.filter(fake_event, config):
                        return (
                            False,
                            f"当前用户无权调用 /{entry.command_name}。",
                            fake_event,
                            {},
                        )
                    continue
                if not handler_filter.filter(fake_event, config):
                    return (
                        False,
                        f"命令 /{entry.command_name} 的运行条件未满足。",
                        fake_event,
                        {},
                    )
            except Exception as exc:
                return False, f"命令过滤器校验失败: {exc}", fake_event, {}

        fake_event._extras.pop("parsed_params", None)
        return True, "", fake_event, parsed_params

    # ---- Meta tool handlers ----

    async def _list_plugin_commands_handler(self, event: AstrMessageEvent) -> str:
        entries = [
            entry
            for entry in await self._build_command_catalog_for_event(event)
            if self._command_allowed_by_static_policy(entry)
        ]
        if not entries:
            return "当前没有可供 LLM 安全调用的插件命令。"

        lines = [f"已发现 {len(entries)} 个可调用插件命令："]
        current_plugin = None
        for entry in entries:
            if entry.plugin_name != current_plugin:
                current_plugin = entry.plugin_name
                lines.append("")
                lines.append(f"- 插件 `{entry.plugin_name}`: {entry.plugin_desc}")
            lines.append(
                f"  - /{entry.command_name} "
                f"(command_id={entry.command_id}, tool={entry.tool_name})"
            )
            if entry.description:
                lines.append(f"    描述: {entry.description}")
            if entry.aliases:
                lines.append(f"    别名: {', '.join(entry.aliases)}")
            if entry.parameters:
                params = ", ".join(
                    f"{param.name}:{param.schema_type}"
                    + ("*" if param.required else "")
                    for param in entry.parameters
                )
                lines.append(f"    参数: {params}")
        return "\n".join(lines)

    async def _search_plugin_commands_handler(
        self,
        event: AstrMessageEvent,
        keywords: str,
    ) -> str:
        keywords = keywords.strip().lower()
        if not keywords:
            return "请提供搜索关键词。"

        entries = [
            entry
            for entry in await self._build_command_catalog_for_event(event)
            if self._command_allowed_by_static_policy(entry)
        ]
        terms = [term for term in re.split(r"\s+", keywords) if term]
        matched = [
            entry
            for entry in entries
            if self._entry_matches(entry, terms)
        ]

        if not matched:
            return f"未找到匹配 `{keywords}` 的可调用插件命令。"

        lines = [f"找到 {len(matched)} 个匹配命令："]
        for entry in matched:
            lines.append(
                f"- `{entry.plugin_name}` / `/{entry.command_name}` "
                f"(command_id={entry.command_id}, tool={entry.tool_name})"
            )
            lines.append(f"  {entry.description}")
        return "\n".join(lines)

    async def _call_plugin_command_handler(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        command_name: str,
        command_args: str = "",
    ) -> str:
        entry = await self._find_command_entry(event, plugin_name, command_name)
        if not entry:
            return (
                f"未找到插件 `{plugin_name}` 中的命令 `{command_name}`。"
                "请先调用 list_plugin_commands 或 search_plugin_commands。"
            )
        args = self._parse_command_args(entry, command_args)
        if isinstance(args, str):
            return args
        return await self._call_entry(event, entry, args)

    async def _call_plugin_alias_handler(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        tool_name: str,
        tool_args: str = "",
    ) -> str:
        return await self._call_plugin_command_handler(
            event,
            plugin_name,
            tool_name,
            tool_args,
        )

    async def _call_command_by_id(
        self,
        event: AstrMessageEvent,
        command_id: str,
        kwargs: dict[str, Any],
    ) -> str:
        entry = await self._find_command_entry(event, "", command_id)
        if not entry:
            return f"命令工具 `{command_id}` 已不可用，请重新查询插件命令列表。"
        return await self._call_entry(event, entry, kwargs)

    async def _call_entry(
        self,
        event: AstrMessageEvent,
        entry: CommandEntry,
        command_args: dict[str, Any],
    ) -> str:
        if not self._command_allowed_by_static_policy(entry):
            return f"命令 /{entry.command_name} 未被当前 Toolifier 策略允许调用。"

        ok, reason, fake_event, parsed_args = await self._validate_runtime_filters(
            event,
            entry,
            command_args,
        )
        if not ok:
            return reason

        captured: list[MessageChain | MessageEventResult | str] = []
        self._patch_event_senders(fake_event, captured)

        try:
            await self._invoke_handler(fake_event, entry, parsed_args, captured)
        except Exception as exc:
            logger.exception(
                "Plugin Toolifier command call failed: %s",
                entry.display_name,
            )
            return f"调用命令 /{entry.command_name} 失败: {exc}"

        if result := fake_event.get_result():
            captured.append(result)

        text = self._captured_to_text(captured)
        if text:
            return text
        return f"命令 /{entry.command_name} 执行完成，但没有产生可返回内容。"

    async def _invoke_handler(
        self,
        fake_event: AstrMessageEvent,
        entry: CommandEntry,
        kwargs: dict[str, Any],
        captured: list[MessageChain | MessageEventResult | str],
    ) -> None:
        timeout = self._timeout_seconds()
        ready = entry.handler.handler(fake_event, **kwargs)

        if inspect.isasyncgen(ready):
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            ready.__anext__(),
                            timeout=timeout,
                        )
                    except StopAsyncIteration:
                        break
                    self._capture_handler_result(chunk, captured)
            finally:
                await ready.aclose()
            return

        if inspect.isawaitable(ready):
            result = await asyncio.wait_for(ready, timeout=timeout)
            self._capture_handler_result(result, captured)
            return

        self._capture_handler_result(ready, captured)

    @staticmethod
    def _capture_handler_result(
        result: Any,
        captured: list[MessageChain | MessageEventResult | str],
    ) -> None:
        if result is None:
            return
        if isinstance(result, ProviderRequest):
            captured.append("命令返回了 LLM 请求，Toolifier 不会执行嵌套 LLM 请求。")
            return
        if isinstance(result, MessageChain | MessageEventResult | str):
            captured.append(result)
            return
        captured.append(str(result))

    async def _find_command_entry(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        command_name: str,
    ) -> CommandEntry | None:
        normalized_plugin = plugin_name.strip().lower()
        normalized_command = command_name.strip().lower()
        for entry in await self._build_command_catalog_for_event(event):
            if normalized_plugin and entry.plugin_name.lower() != normalized_plugin:
                continue
            names = {
                entry.command_name.lower(),
                entry.command_id.lower(),
                entry.tool_name.lower(),
                *(alias.lower() for alias in entry.aliases),
            }
            if normalized_command in names:
                return entry
        return None

    @staticmethod
    def _entry_matches(entry: CommandEntry, terms: list[str]) -> bool:
        haystack = " ".join(
            [
                entry.plugin_name,
                entry.plugin_desc,
                entry.command_name,
                entry.description,
                " ".join(entry.aliases),
                " ".join(param.name for param in entry.parameters),
            ]
        ).lower()
        return all(term in haystack for term in terms)

    # ---- Argument handling ----

    def _parse_command_args(
        self,
        entry: CommandEntry,
        raw_args: str,
    ) -> dict[str, Any] | str:
        raw_args = (raw_args or "").strip()
        if not entry.parameters:
            return {}
        if not raw_args:
            missing = [param.name for param in entry.parameters if param.required]
            if missing:
                return f"缺少必要参数: {', '.join(missing)}"
            return {}

        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            return self._coerce_mapping(entry, parsed)
        if isinstance(parsed, list):
            return self._coerce_sequence(entry, parsed)
        if parsed is not None:
            return self._coerce_sequence(entry, [parsed])

        try:
            parts = shlex.split(raw_args)
        except ValueError:
            parts = raw_args.split()
        return self._coerce_sequence(entry, parts)

    def _coerce_mapping(
        self,
        entry: CommandEntry,
        values: dict[str, Any],
    ) -> dict[str, Any] | str:
        result = {}
        for param in entry.parameters:
            if param.name not in values:
                if param.required:
                    return f"缺少必要参数: {param.name}"
                continue
            coerced = self._coerce_value(values[param.name], param)
            if isinstance(coerced, str) and coerced.startswith("__error__:"):
                return coerced.removeprefix("__error__:")
            result[param.name] = coerced
        return result

    def _coerce_sequence(
        self,
        entry: CommandEntry,
        values: list[Any],
    ) -> dict[str, Any] | str:
        result = {}
        params = entry.parameters
        for index, param in enumerate(params):
            if param.raw_spec is GreedyStr:
                value = " ".join(str(item) for item in values[index:])
            elif index < len(values):
                value = values[index]
            elif param.required:
                return f"缺少必要参数: {param.name}"
            else:
                continue

            coerced = self._coerce_value(value, param)
            if isinstance(coerced, str) and coerced.startswith("__error__:"):
                return coerced.removeprefix("__error__:")
            result[param.name] = coerced
        return result

    @staticmethod
    def _coerce_value(value: Any, param: CommandParameter) -> Any:
        target_type = _target_python_type(param.raw_spec)
        if target_type is None or isinstance(value, target_type):
            return value
        try:
            if target_type is bool:
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "yes", "1", "on"}:
                        return True
                    if lowered in {"false", "no", "0", "off"}:
                        return False
                    return f"__error__:参数 `{param.name}` 类型错误，应为 bool。"
                return bool(value)
            return target_type(value)
        except (TypeError, ValueError):
            return f"__error__:参数 `{param.name}` 类型错误，应为 {target_type.__name__}。"

    # ---- Event capture ----

    def _create_command_event(
        self,
        original_event: AstrMessageEvent,
        entry: CommandEntry,
        command_args: dict[str, Any],
    ) -> AstrMessageEvent:
        fake_event = copy.copy(original_event)
        fake_event.message_obj = copy.copy(original_event.message_obj)
        message_str = self._command_message_text(entry, command_args)
        fake_event.message_str = message_str
        fake_event.message_obj.message_str = message_str
        fake_event.message_obj.message = [Plain(message_str)]
        fake_event._result = None
        fake_event._extras = dict(original_event.get_extra(default={}) or {})
        fake_event._force_stopped = False
        fake_event._has_send_oper = False
        fake_event.call_llm = True
        fake_event.is_wake = True
        fake_event.is_at_or_wake_command = True

        # Keep the original platform/session/sender semantics, but avoid mutating
        # the source event's message object.
        if isinstance(fake_event.message_obj, AstrBotMessage):
            fake_event.message_obj.session_id = original_event.session_id
        return fake_event

    @staticmethod
    def _command_message_text(entry: CommandEntry, command_args: dict[str, Any]) -> str:
        parts = [entry.command_name]
        for param in entry.parameters:
            if param.name not in command_args:
                continue
            value = command_args[param.name]
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            elif isinstance(value, dict):
                parts.append(json.dumps(value, ensure_ascii=False))
            else:
                parts.append(str(value))
        return " ".join(parts)

    @staticmethod
    def _patch_event_senders(
        fake_event: AstrMessageEvent,
        captured: list[MessageChain | MessageEventResult | str],
    ) -> None:
        async def _capture_send(message: MessageChain | None) -> None:
            if message is not None:
                captured.append(message)
            fake_event._has_send_oper = True

        async def _capture_streaming(generator, use_fallback: bool = False) -> None:
            async for chain in generator:
                if chain is not None:
                    captured.append(chain)
            fake_event._has_send_oper = True

        async def _noop() -> None:
            return None

        fake_event.send = _capture_send  # type: ignore[method-assign]
        fake_event.send_streaming = _capture_streaming  # type: ignore[method-assign]
        fake_event.send_typing = _noop  # type: ignore[method-assign]
        fake_event.stop_typing = _noop  # type: ignore[method-assign]

    def _captured_to_text(
        self,
        captured: list[MessageChain | MessageEventResult | str],
    ) -> str:
        parts = []
        for item in captured:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            text = self._message_chain_to_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _message_chain_to_text(message: MessageChain | MessageEventResult) -> str:
        parts = []
        for component in message.chain or []:
            if isinstance(component, Plain):
                parts.append(component.text)
            elif isinstance(component, Image):
                parts.append("[Image]")
            else:
                comp_type = getattr(component, "type", component.__class__.__name__)
                parts.append(f"[{comp_type}]")
        return "\n".join(part for part in parts if str(part).strip())

    # ---- Commands for humans ----

    @filter.command("list_plugins")
    async def cmd_list_plugins(self, event: AstrMessageEvent):
        """List plugin commands exposed to LLM."""
        output = await self._list_plugin_commands_handler(event)
        yield event.plain_result(output).use_t2i(False)

    @filter.command("search_plugins")
    async def cmd_search_plugins(
        self,
        event: AstrMessageEvent,
        keywords: str = "",
    ):
        """Search plugin commands exposed to LLM."""
        if not keywords.strip():
            yield event.plain_result(
                "请提供搜索关键词，例如：/search_plugins 翻译",
            ).use_t2i(False)
            return
        output = await self._search_plugin_commands_handler(event, keywords)
        yield event.plain_result(output).use_t2i(False)

    # ---- Cache/sync hooks ----

    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata) -> None:
        await self._sync_command_tools()
        logger.debug("Plugin %s loaded, synced Toolifier command tools", metadata.name)

    @filter.on_plugin_unloaded()
    async def _on_plugin_unloaded(self, metadata) -> None:
        await self._sync_command_tools()
        logger.debug(
            "Plugin %s unloaded, synced Toolifier command tools",
            metadata.name,
        )

    # ---- Config helpers ----

    def _setting(self, key: str, default: Any) -> Any:
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return getter(key, default)
        return default

    def _registration_mode(self) -> str:
        mode = self._setting("registration_mode", "meta")
        if mode in {"meta", "per_command", "both"}:
            return mode
        return "meta"

    def _setting_set(self, key: str) -> set[str]:
        value = self._setting(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    async def _disabled_plugins_for_event(
        self,
        event: AstrMessageEvent,
    ) -> set[str]:
        session_plugin_config = await sp.get_async(
            scope="umo",
            scope_id=event.unified_msg_origin,
            key="session_plugin_config",
            default={},
        )
        session_config = session_plugin_config.get(event.unified_msg_origin, {})
        disabled_plugins = session_config.get("disabled_plugins", [])
        return {str(plugin_name) for plugin_name in disabled_plugins}

    def _timeout_seconds(self) -> float:
        raw = self._setting("tool_timeout_seconds", 30)
        try:
            return max(1.0, min(float(raw), 300.0))
        except (TypeError, ValueError):
            return 30.0


def _python_type_to_schema_type(py_type: type) -> str:
    if py_type is inspect.Parameter.empty:
        return "string"
    if py_type is bool:
        return "boolean"
    if py_type in {int, float}:
        return "number"
    if py_type in {list, tuple, set}:
        return "array"
    if py_type is dict:
        return "object"
    return "string"


def _python_value_to_schema_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list | tuple | set):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _target_python_type(raw_spec: Any) -> type | None:
    if raw_spec is GreedyStr:
        return str
    if raw_spec is inspect.Parameter.empty:
        return str
    if isinstance(raw_spec, type):
        return raw_spec
    if (
        isinstance(raw_spec, types.UnionType)
        or typing.get_origin(raw_spec) is typing.Union
    ):
        non_none = [
            item for item in typing.get_args(raw_spec) if item is not type(None)
        ]
        if len(non_none) == 1 and isinstance(non_none[0], type):
            return non_none[0]
        return str
    if raw_spec is None:
        return None
    return type(raw_spec)
