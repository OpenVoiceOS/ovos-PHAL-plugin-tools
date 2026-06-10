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
from typing import Dict, Optional, Type

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
    and keeps an in-memory registry keyed by ``tool_name``.  Tool names must be
    unique across toolboxes; if two toolboxes register the same name the later
    one wins and a warning is logged.
    """

    def __init__(self, bus=None, config=None):
        super().__init__(bus, "ovos-phal-plugin-tools", config)
        self._toolboxes: Dict[str, ToolBox] = {}
        # registry: tool_name → (toolbox_id, ToolBox)
        self._tool_registry: Dict[str, ToolBox] = {}
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
        """Discover and instantiate all installed ToolBox plugins."""
        plugins: Dict[str, Type[ToolBox]] = find_toolbox_plugins()
        LOG.info(f"[ovos-phal-plugin-tools] found {len(plugins)} toolbox plugin(s): {list(plugins.keys())}")
        for ep_name, cls in plugins.items():
            try:
                tb = cls(toolbox_id=ep_name, bus=self.bus)
                self._toolboxes[ep_name] = tb
                for tool_name in tb.tools:
                    if tool_name in self._tool_registry:
                        LOG.warning(
                            f"[ovos-phal-plugin-tools] tool name collision: '{tool_name}' "
                            f"already registered from toolbox '{self._tool_registry[tool_name].toolbox_id}', "
                            f"overwriting with toolbox '{ep_name}'"
                        )
                    self._tool_registry[tool_name] = tb
                LOG.debug(f"[ovos-phal-plugin-tools] loaded toolbox '{ep_name}' with tools: {list(tb.tools.keys())}")
            except Exception as e:
                LOG.exception(f"[ovos-phal-plugin-tools] failed to load toolbox '{ep_name}': {e}")

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
        tools = []
        for tb in self._toolboxes.values():
            for entry in tb.tool_json_list:
                tools.append({**entry, "toolbox_id": tb.toolbox_id})
        self.bus.emit(message.response({"tools": tools}))

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
        tb: Optional[ToolBox] = self._tool_registry.get(name)
        if tb is None:
            self.bus.emit(message.response({"error": f"Unknown tool: '{name}'"}))
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

        tb: Optional[ToolBox] = self._tool_registry.get(name)
        if tb is None:
            self.bus.emit(message.response({"name": name, "error": f"Unknown tool: '{name}'"}))
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

        Response payload::

            {"loaded": ["math_tools", ...], "total_tools": 3}
        """
        self._toolboxes.clear()
        self._tool_registry.clear()
        self._load_toolboxes()
        total_tools = sum(len(tb.tools) for tb in self._toolboxes.values())
        self.bus.emit(message.response({
            "loaded": list(self._toolboxes.keys()),
            "total_tools": total_tools,
        }))
