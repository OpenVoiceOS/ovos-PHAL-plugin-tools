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
"""Unit tests for OVOSToolsPHALPlugin bus handlers.

These tests bypass plugin loading and inject a minimal in-process registry
so the handler logic can be tested without any installed toolbox plugins.
"""
import unittest
from typing import Any, Dict, List, Optional, Union
from unittest.mock import patch

from ovos_bus_client import Message
from ovos_bus_client.client import MessageBusClient
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import (
    AgentTool,
    ToolArguments,
    ToolBox,
    ToolOutput,
)


# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

class AddArgs(ToolArguments):
    a: int = Field(..., description="First operand")
    b: int = Field(..., description="Second operand")


class AddOutput(ToolOutput):
    result: int = Field(..., description="Sum of a and b")


def _add_logic(args: AddArgs) -> AddOutput:
    return AddOutput(result=args.a + args.b)


def _fail_logic(args: AddArgs) -> AddOutput:
    raise RuntimeError("intentional failure")


class MathToolBox(ToolBox):
    """Mirrors the real-world plugin contract: subclasses supply their own
    ``toolbox_id`` to ``super().__init__()`` and only expose ``(config, bus)``
    in their own constructor - callers must never pass ``toolbox_id``."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 bus: Optional[Union[MessageBusClient, FakeBus]] = None):
        self.received_config = config
        super().__init__(toolbox_id="math_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="add",
                description="Add two integers.",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=_add_logic,
            )
        ]


class FailToolBox(ToolBox):
    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 bus: Optional[Union[MessageBusClient, FakeBus]] = None):
        super().__init__(toolbox_id="fail_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="fail",
                description="Always raises.",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=_fail_logic,
            )
        ]


# ---------------------------------------------------------------------------
# Helper: build a plugin instance with an injected registry
# ---------------------------------------------------------------------------

def _make_plugin(toolboxes=None, config=None):
    """Return an OVOSToolsPHALPlugin wired to a FakeBus with injected toolboxes."""
    from ovos_phal_plugin_tools import OVOSToolsPHALPlugin

    bus = FakeBus()
    # Patch find_toolbox_plugins so no real installed plugins are touched
    patched_plugins = toolboxes or {}
    with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value=patched_plugins):
        plugin = OVOSToolsPHALPlugin(bus=bus, config=config)
    return plugin, bus


# ---------------------------------------------------------------------------
# Regression: ToolBox plugins take (config, bus), NOT toolbox_id
# ---------------------------------------------------------------------------
#
# Real ToolBox plugins (e.g. ovos-agentic-loop's clock/math/web/filesystem/shell
# tools, ovos-ddg-plugin, ovos-wikipedia-plugin) supply their own `toolbox_id`
# to `super().__init__()` and only expose `(config=None, bus=None)` in their
# own `__init__`. Calling `cls(toolbox_id=ep_name, bus=self.bus)` therefore
# raises `TypeError: __init__() got an unexpected keyword argument
# 'toolbox_id'` for every real toolbox plugin. Because the loader wraps this
# in a per-toolbox try/except, the failure was previously silent: zero
# toolboxes loaded and only a debug-level log line said why. This test would
# have caught that silent zero-toolbox load.

class RealWorldToolBox(ToolBox):
    """Shaped exactly like a real installed plugin: constructor only accepts
    ``config`` and ``bus``, never ``toolbox_id``."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 bus: Optional[Union[MessageBusClient, FakeBus]] = None):
        self.received_config = config
        super().__init__(toolbox_id="real_world_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="ping",
                description="Return pong.",
                argument_schema=ToolArguments,
                output_schema=ToolOutput,
                tool_call=lambda args: ToolOutput(),
            )
        ]


class TestRealWorldToolBoxContract(unittest.TestCase):
    def test_toolbox_instantiates_without_toolbox_id_kwarg(self):
        """The loader must not pass toolbox_id= to the plugin constructor."""
        plugin, _ = _make_plugin(
            {"real_world_tools": RealWorldToolBox},
            config={"real_world_tools": {"greeting": "hi"}},
        )
        # A silent zero-toolbox load is exactly the bug: assert non-empty.
        self.assertNotEqual(plugin._toolboxes, {})
        self.assertIn("real_world_tools", plugin._toolboxes)
        self.assertIn("ping", plugin._tool_registry)

    def test_toolbox_receives_its_own_config(self):
        """Per-toolbox config must be sourced from self.config, not ignored."""
        plugin, _ = _make_plugin(
            {"real_world_tools": RealWorldToolBox},
            config={"real_world_tools": {"greeting": "hi"}},
        )
        tb = plugin._toolboxes["real_world_tools"]
        self.assertEqual(tb.received_config, {"greeting": "hi"})

    def test_toolbox_defaults_to_empty_config(self):
        """Toolboxes with no matching config key still load, with {}."""
        plugin, _ = _make_plugin({"real_world_tools": RealWorldToolBox})
        tb = plugin._toolboxes["real_world_tools"]
        self.assertEqual(tb.received_config, {})


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestRegistry(unittest.TestCase):
    def test_empty_registry(self):
        plugin, _ = _make_plugin()
        self.assertEqual(plugin._toolboxes, {})
        self.assertEqual(plugin._tool_registry, {})

    def test_registry_populated(self):
        plugin, _ = _make_plugin({"math_tools": MathToolBox})
        self.assertIn("math_tools", plugin._toolboxes)
        self.assertIn("add", plugin._tool_registry)

    def test_collision_logged(self):
        """Two toolboxes with the same tool name → warning, last wins."""
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin

        class DupToolBox(ToolBox):
            def __init__(self, config=None, bus=None):
                super().__init__(toolbox_id="dup_tools", config=config, bus=bus)

            def discover_tools(self):
                return [
                    AgentTool(
                        name="add",
                        description="duplicate",
                        argument_schema=AddArgs,
                        output_schema=AddOutput,
                        tool_call=_add_logic,
                    )
                ]

        bus = FakeBus()
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"math_tools": MathToolBox, "dup_tools": DupToolBox}):
            with patch("ovos_phal_plugin_tools.LOG") as mock_log:
                plugin = OVOSToolsPHALPlugin(bus=bus)
                # warning must have been emitted at least once
                self.assertTrue(mock_log.warning.called)
        # last-loaded toolbox wins
        self.assertEqual(plugin._tool_registry["add"].toolbox_id, "dup_tools")


# ---------------------------------------------------------------------------
# ovos.tools.list
# ---------------------------------------------------------------------------

class TestHandleToolsList(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"math_tools": MathToolBox})

    def test_response_contains_tools(self):
        responses = []
        self.bus.on("ovos.tools.list.response", lambda m: responses.append(m))
        self.bus.emit(Message("ovos.tools.list"))
        self.assertEqual(len(responses), 1)
        tools = responses[0].data["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "add")
        self.assertIn("argument_schema", tools[0])
        self.assertIn("output_schema", tools[0])
        self.assertEqual(tools[0]["toolbox_id"], "math_tools")

    def test_response_empty_when_no_toolboxes(self):
        plugin, bus = _make_plugin()
        responses = []
        bus.on("ovos.tools.list.response", lambda m: responses.append(m))
        bus.emit(Message("ovos.tools.list"))
        self.assertEqual(responses[0].data["tools"], [])


# ---------------------------------------------------------------------------
# ovos.tools.get
# ---------------------------------------------------------------------------

class TestHandleToolsGet(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"math_tools": MathToolBox})

    def _get(self, payload):
        responses = []
        self.bus.on("ovos.tools.get.response", lambda m: responses.append(m))
        self.bus.emit(Message("ovos.tools.get", payload))
        return responses[0].data

    def test_get_known_tool(self):
        data = self._get({"name": "add"})
        self.assertEqual(data["name"], "add")
        self.assertIn("argument_schema", data)
        self.assertIn("output_schema", data)
        self.assertEqual(data["toolbox_id"], "math_tools")
        self.assertNotIn("error", data)

    def test_get_unknown_tool(self):
        data = self._get({"name": "nope"})
        self.assertIn("error", data)
        self.assertIn("nope", data["error"])

    def test_get_missing_name(self):
        data = self._get({})
        self.assertIn("error", data)
        self.assertIn("name", data["error"])


# ---------------------------------------------------------------------------
# ovos.tools.invoke
# ---------------------------------------------------------------------------

class TestHandleToolsInvoke(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin(
            {"math_tools": MathToolBox, "fail_tools": FailToolBox}
        )

    def _invoke(self, payload):
        responses = []
        self.bus.on("ovos.tools.invoke.response", lambda m: responses.append(m))
        self.bus.emit(Message("ovos.tools.invoke", payload))
        return responses[0].data

    def test_invoke_success(self):
        data = self._invoke({"name": "add", "args": {"a": 3, "b": 4}})
        self.assertEqual(data["name"], "add")
        self.assertEqual(data["result"]["result"], 7)
        self.assertNotIn("error", data)

    def test_invoke_unknown_tool(self):
        data = self._invoke({"name": "nope", "args": {}})
        self.assertEqual(data["name"], "nope")
        self.assertIn("error", data)
        self.assertIn("nope", data["error"])

    def test_invoke_missing_name(self):
        data = self._invoke({"args": {"a": 1, "b": 2}})
        self.assertEqual(data["name"], "")
        self.assertIn("error", data)

    def test_invoke_bad_args(self):
        data = self._invoke({"name": "add", "args": {"a": "not_int", "b": 2}})
        self.assertIn("error", data)

    def test_invoke_tool_raises(self):
        data = self._invoke({"name": "fail", "args": {"a": 1, "b": 2}})
        self.assertIn("error", data)
        self.assertIn("fail", data["name"])

    def test_name_echoed_on_all_responses(self):
        for name, args in [
            ("add", {"a": 1, "b": 2}),
            ("nope", {}),
            ("fail", {"a": 1, "b": 2}),
        ]:
            data = self._invoke({"name": name, "args": args})
            self.assertEqual(data["name"], name)


# ---------------------------------------------------------------------------
# ovos.tools.reload
# ---------------------------------------------------------------------------

class TestHandleToolsReload(unittest.TestCase):
    def test_reload_repopulates_registry(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        # manually clear to simulate stale state
        plugin._toolboxes.clear()
        plugin._tool_registry.clear()

        responses = []
        bus.on("ovos.tools.reload.response", lambda m: responses.append(m))

        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"math_tools": MathToolBox}):
            bus.emit(Message("ovos.tools.reload"))

        self.assertEqual(len(responses), 1)
        data = responses[0].data
        self.assertIn("math_tools", data["loaded"])
        self.assertEqual(data["total_tools"], 1)
        self.assertIn("add", plugin._tool_registry)


if __name__ == "__main__":
    unittest.main()
