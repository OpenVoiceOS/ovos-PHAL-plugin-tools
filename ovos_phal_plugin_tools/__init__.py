# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Type

from ovos_plugin_manager.phal import PHALPlugin
from ovos_plugin_manager.persona import find_toolbox_plugins
from ovos_plugin_manager.templates.agent_tools import ToolBox
from ovos_utils.log import LOG
from ovos_bus_client import Message
from ovos_utils.process_utils import RuntimeRequirements


class OVOSToolsPHALPlugin(PHALPlugin):
    """PHAL plugin that exposes installed OPM ToolBox plugins over the messagebus.

    Third-party skills, agents, or external clients can:
      - list all tools across all loaded toolboxes
      - retrieve the full schema of a single tool
      - invoke a tool by name with keyword arguments

    The plugin loads all ``opm.agents.toolbox`` entry-point plugins at startup
    and keeps an in-memory registry keyed by ``tool_name``.  The service
    namespace is flat, so a name two toolboxes both own cannot be resolved:
    ``ovos.tools.get`` and ``ovos.tools.invoke`` answer an error for it
    (OVOS-TOOLS-1 §5), and a warning is logged at load.
    """

    def __init__(self, bus: Any = None, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(bus, "ovos-phal-plugin-tools", config)
        self._toolboxes: Dict[str, ToolBox] = {}
        # registry: tool_name → every ToolBox that owns a tool of that name
        self._tool_registry: Dict[str, List[ToolBox]] = {}
        self._load_toolboxes()
        self._register_bus_handlers()

    # ------------------------------------------------------------------
    # RuntimeRequirements
    # ------------------------------------------------------------------

    @property
    def runtime_requirements(self):
        return RuntimeRequirements(
            internet_before_load=False,
            network_before_load=False,
            requires_internet=False,
            requires_network=False,
            no_internet_fallback=True,
            no_network_fallback=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_toolboxes(self) -> None:
        """Discover installed ToolBox plugins and build the tool registry.

        A toolbox already loaded is kept and its tools refreshed. Building it
        again would bind a second set of its bus handlers, so every toolbox
        topic would be answered, and every call run, once more per reload.
        A toolbox whose plugin is gone is unbound from the bus and dropped.
        """
        plugins: Dict[str, Type[ToolBox]] = find_toolbox_plugins()
        LOG.info(f"[ovos-phal-plugin-tools] found {len(plugins)} toolbox plugin(s): {list(plugins.keys())}")
        for ep_name in set(self._toolboxes) - set(plugins):
            self._unbind(self._toolboxes.pop(ep_name))
        self._tool_registry.clear()
        for ep_name, cls in plugins.items():
            try:
                tb = self._toolboxes.get(ep_name)
                if tb is None:
                    tb = cls(config=self.config.get(ep_name, {}), bus=self.bus)
                    self._toolboxes[ep_name] = tb
                else:
                    tb.refresh_tools()
                for tool_name in tb.tools:
                    owners = self._tool_registry.setdefault(tool_name, [])
                    if owners:
                        LOG.warning(
                            f"[ovos-phal-plugin-tools] tool name collision: '{tool_name}' "
                            f"is owned by toolboxes {[o.toolbox_id for o in owners] + [ep_name]}; "
                            f"ovos.tools.get and ovos.tools.invoke answer an error for it"
                        )
                    owners.append(tb)
                LOG.debug(f"[ovos-phal-plugin-tools] loaded toolbox '{ep_name}' with tools: {list(tb.tools.keys())}")
            except Exception as e:
                LOG.exception(f"[ovos-phal-plugin-tools] failed to load toolbox '{ep_name}' ({cls}): {e}")

    def _unbind(self, tb: ToolBox) -> None:
        """Remove the bus handlers ``ToolBox.bind()`` registered for *tb*.

        Removing a handler that was never registered is a no-op, so this holds
        for ovos-plugin-manager versions with and without the static
        ``ovos.persona.tools.call`` topic (OVOS-TOOLS-1 §4.2).
        """
        self.bus.remove("ovos.persona.tools.discover", tb.handle_discover)
        self.bus.remove("ovos.persona.tools.call", tb.handle_call)
        self.bus.remove(f"ovos.persona.tools.{tb.toolbox_id}.call", tb.handle_call)

    def _tool_definitions(self) -> List[Dict[str, Any]]:
        """Every loaded tool as an OVOS-TOOLS-1 §3 definition."""
        return [{**entry, "toolbox_id": tb.toolbox_id}
                for tb in self._toolboxes.values()
                for entry in tb.tool_json_list]

    def _resolve(self, name: str) -> Tuple[Optional[ToolBox], Optional[str]]:
        """Return the one toolbox that owns *name*, or the error to answer."""
        owners = self._tool_registry.get(name, [])
        if not owners:
            return None, f"Unknown tool: '{name}'"
        if len(owners) > 1:
            ids = ", ".join(f"'{tb.toolbox_id}'" for tb in owners)
            return None, f"Ambiguous tool: '{name}' is owned by toolboxes {ids}"
        return owners[0], None

    def _register_bus_handlers(self) -> None:
        self.bus.on("ovos.tools.list", self.handle_tools_list)
        self.bus.on("ovos.tools.get", self.handle_tools_get)
        self.bus.on("ovos.tools.invoke", self.handle_tools_invoke)
        self.bus.on("ovos.tools.reload", self.handle_tools_reload)

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------

    def handle_tools_list(self, message: Message) -> None:
        """Reply to ``ovos.tools.list`` with all available tools.

        Response event: ``ovos.tools.list.response``

        Response payload::

            {
                "tools": [
                    {
                        "name": "add",
                        "description": "Add two integers.",
                        "argument_schema": { ... },   // JSON Schema
                        "output_schema":   { ... },   // JSON Schema
                        "toolbox_id": "math_tools"
                    },
                    ...
                ]
            }
        """
        self.bus.emit(message.response({"tools": self._tool_definitions()}))

    def handle_tools_get(self, message: Message) -> None:
        """Reply to ``ovos.tools.get`` with a single tool's schema.

        Request payload::

            {"name": "add"}

        Response event: ``ovos.tools.get.response``

        Response payload (success)::

            {
                "name": "add",
                "description": "Add two integers.",
                "argument_schema": { ... },
                "output_schema":   { ... },
                "toolbox_id": "math_tools"
            }

        Response payload (error)::

            {"error": "Unknown tool: 'nonexistent'"}
        """
        name: str = message.data.get("name", "")
        if not name:
            self.bus.emit(message.response({"error": "Missing required field: 'name'"}))
            return
        tb, error = self._resolve(name)
        if error:
            self.bus.emit(message.response({"error": error}))
            return
        tool = tb.get_tool(name)
        if tool is None:
            self.bus.emit(message.response({"error": f"Unknown tool: '{name}'"}))
            return
        self.bus.emit(message.response({
            "name": tool.name,
            "description": tool.description,
            "argument_schema": tool.argument_schema.model_json_schema(),
            "output_schema": tool.output_schema.model_json_schema(),
            "toolbox_id": tb.toolbox_id,
        }))

    def handle_tools_invoke(self, message: Message) -> None:
        """Execute a tool and reply with its result.

        Request payload::

            {"name": "add", "args": {"a": 3, "b": 4}}

        Response event: ``ovos.tools.invoke.response``

        Response payload (success)::

            {"name": "add", "result": {"result": 7}}

        Response payload (error)::

            {"name": "add", "error": "ValueError: ..."}
        """
        name: str = message.data.get("name", "")
        args: dict = message.data.get("args", {})

        if not name:
            self.bus.emit(message.response({"name": "", "error": "Missing required field: 'name'"}))
            return

        tb, error = self._resolve(name)
        if error:
            self.bus.emit(message.response({"name": name, "error": error}))
            return

        try:
            result = tb.call_tool(name, args)
            self.bus.emit(message.response({
                "name": name,
                "result": result.model_dump(mode="json"),
            }))
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            LOG.exception(f"[ovos-phal-plugin-tools] tool '{name}' raised: {error}")
            self.bus.emit(message.response({"name": name, "error": error}))

    def handle_tools_reload(self, message: Message) -> None:
        """Reload the toolbox registry at runtime.

        Useful when new toolbox plugins have been installed while OVOS is running.

        Response event: ``ovos.tools.reload.response``

        Response payload (``tools`` as in ``ovos.tools.list``, OVOS-TOOLS-1 §5)::

            {"tools": [...], "loaded": ["math_tools", ...], "total_tools": 3}
        """
        self._load_toolboxes()
        tools = self._tool_definitions()
        self.bus.emit(message.response({
            "tools": tools,
            "loaded": list(self._toolboxes.keys()),
            "total_tools": len(tools),
        }))
