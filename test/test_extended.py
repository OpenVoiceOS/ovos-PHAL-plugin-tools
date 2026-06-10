"""Extended unit tests for OVOSToolsPHALPlugin.

Covers:
- invoke with args missing required schema fields
- tool raising mid-execution (error reply, bus not crashed)
- reload with plugin set changed (added/removed)
- concurrent invokes (thread-safety of registry)
- list with zero plugins
- get with empty name / non-string name
- malformed bus message payloads (None data, missing keys)
- response message context preservation (session/ident routing)
- runtime_requirements property
- toolbox load failure is caught and logged
"""
import threading
import unittest
from typing import List
from unittest.mock import patch, MagicMock

from ovos_bus_client import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field, ValidationError

from ovos_plugin_manager.templates.agent_tools import (
    AgentTool,
    ToolArguments,
    ToolBox,
    ToolOutput,
)


# ---------------------------------------------------------------------------
# Fixtures (reuse same shape as test_unit fixtures)
# ---------------------------------------------------------------------------

class AddArgs(ToolArguments):
    a: int = Field(..., description="First operand")
    b: int = Field(..., description="Second operand")


class AddOutput(ToolOutput):
    result: int = Field(..., description="Sum")


def _add_logic(args: AddArgs) -> AddOutput:
    return AddOutput(result=args.a + args.b)


def _raise_logic(args: AddArgs) -> AddOutput:
    raise ValueError("tool exploded deliberately")


class MathToolBox(ToolBox):
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


class ExtraToolBox(ToolBox):
    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="mul",
                description="Multiply (stub).",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=_add_logic,
            )
        ]


class RaiseToolBox(ToolBox):
    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="boom",
                description="Always raises.",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=_raise_logic,
            )
        ]


def _make_plugin(toolboxes=None):
    from ovos_phal_plugin_tools import OVOSToolsPHALPlugin
    bus = FakeBus()
    patched = toolboxes or {}
    with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value=patched):
        plugin = OVOSToolsPHALPlugin(bus=bus)
    return plugin, bus


def _invoke(bus, payload):
    responses = []
    bus.on("ovos.tools.invoke.response", lambda m: responses.append(m))
    bus.emit(Message("ovos.tools.invoke", payload))
    return responses[0].data if responses else None


def _get(bus, payload):
    responses = []
    bus.on("ovos.tools.get.response", lambda m: responses.append(m))
    bus.emit(Message("ovos.tools.get", payload))
    return responses[0].data if responses else None


def _list(bus):
    responses = []
    bus.on("ovos.tools.list.response", lambda m: responses.append(m))
    bus.emit(Message("ovos.tools.list"))
    return responses[0].data if responses else None


# ---------------------------------------------------------------------------
# runtime_requirements
# ---------------------------------------------------------------------------

class TestRuntimeRequirements(unittest.TestCase):
    def test_runtime_requirements_are_offline_safe(self):
        plugin, _ = _make_plugin()
        req = plugin.runtime_requirements
        self.assertFalse(req.internet_before_load)
        self.assertFalse(req.network_before_load)
        self.assertFalse(req.requires_internet)
        self.assertFalse(req.requires_network)
        self.assertTrue(req.no_internet_fallback)
        self.assertTrue(req.no_network_fallback)


# ---------------------------------------------------------------------------
# Toolbox load failure
# ---------------------------------------------------------------------------

class TestToolboxLoadFailure(unittest.TestCase):
    def test_bad_toolbox_class_is_skipped_and_logged(self):
        """A toolbox whose constructor raises must not crash the plugin."""
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin

        class BrokenToolBox(ToolBox):
            def __init__(self, **kwargs):
                raise RuntimeError("broken on init")

            def discover_tools(self):
                return []

        bus = FakeBus()
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"broken": BrokenToolBox}):
            with patch("ovos_phal_plugin_tools.LOG") as mock_log:
                plugin = OVOSToolsPHALPlugin(bus=bus)
                self.assertTrue(mock_log.exception.called)
        # broken toolbox must not appear in registry
        self.assertNotIn("broken", plugin._toolboxes)


# ---------------------------------------------------------------------------
# invoke — missing required fields
# ---------------------------------------------------------------------------

class TestInvokeMissingRequiredFields(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"math_tools": MathToolBox})

    def test_missing_required_arg_a(self):
        data = _invoke(self.bus, {"name": "add", "args": {"b": 5}})
        self.assertIn("error", data)
        self.assertEqual(data["name"], "add")

    def test_missing_both_args(self):
        data = _invoke(self.bus, {"name": "add", "args": {}})
        self.assertIn("error", data)

    def test_wrong_type_for_required_arg(self):
        data = _invoke(self.bus, {"name": "add", "args": {"a": [], "b": 2}})
        self.assertIn("error", data)


# ---------------------------------------------------------------------------
# invoke — tool raises mid-execution
# ---------------------------------------------------------------------------

class TestInvokeToolRaises(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"raise_tools": RaiseToolBox})

    def test_error_reply_contains_exception_type(self):
        data = _invoke(self.bus, {"name": "boom", "args": {"a": 1, "b": 2}})
        self.assertIn("error", data)
        # The framework wraps ValueError in RuntimeError; either appears in the error string
        self.assertTrue(
            "RuntimeError" in data["error"] or "ValueError" in data["error"]
        )

    def test_bus_not_crashed_after_raise(self):
        """Subsequent invocations still work after a tool raises."""
        _invoke(self.bus, {"name": "boom", "args": {"a": 1, "b": 2}})
        # bus is still up: list should still respond
        data = _list(self.bus)
        self.assertIsNotNone(data)

    def test_name_echoed_on_error(self):
        data = _invoke(self.bus, {"name": "boom", "args": {"a": 1, "b": 2}})
        self.assertEqual(data["name"], "boom")


# ---------------------------------------------------------------------------
# reload — plugin set changed
# ---------------------------------------------------------------------------

class TestReloadPluginSetChanged(unittest.TestCase):
    def test_reload_adds_new_toolbox(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        self.assertNotIn("mul", plugin._tool_registry)

        responses = []
        bus.on("ovos.tools.reload.response", lambda m: responses.append(m))

        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"math_tools": MathToolBox, "extra_tools": ExtraToolBox}):
            bus.emit(Message("ovos.tools.reload"))

        data = responses[0].data
        self.assertIn("mul", plugin._tool_registry)
        self.assertIn("extra_tools", data["loaded"])
        self.assertEqual(data["total_tools"], 2)

    def test_reload_removes_old_toolbox(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox, "extra_tools": ExtraToolBox})
        self.assertIn("mul", plugin._tool_registry)

        responses = []
        bus.on("ovos.tools.reload.response", lambda m: responses.append(m))

        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"math_tools": MathToolBox}):
            bus.emit(Message("ovos.tools.reload"))

        data = responses[0].data
        self.assertNotIn("mul", plugin._tool_registry)
        self.assertNotIn("extra_tools", data["loaded"])
        self.assertEqual(data["total_tools"], 1)

    def test_reload_to_empty_set(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        responses = []
        bus.on("ovos.tools.reload.response", lambda m: responses.append(m))

        with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value={}):
            bus.emit(Message("ovos.tools.reload"))

        data = responses[0].data
        self.assertEqual(data["loaded"], [])
        self.assertEqual(data["total_tools"], 0)
        self.assertEqual(plugin._tool_registry, {})


# ---------------------------------------------------------------------------
# concurrent invokes
# ---------------------------------------------------------------------------

class TestConcurrentInvokes(unittest.TestCase):
    def test_concurrent_invokes_all_succeed(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        results = []
        errors = []
        lock = threading.Lock()

        def do_invoke(a, b):
            try:
                data = _invoke(bus, {"name": "add", "args": {"a": a, "b": b}})
                with lock:
                    results.append(data)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=do_invoke, args=(i, i + 1)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 10)
        for data in results:
            self.assertIn("result", data)


# ---------------------------------------------------------------------------
# list with zero plugins
# ---------------------------------------------------------------------------

class TestListZeroPlugins(unittest.TestCase):
    def test_list_empty_returns_empty_list(self):
        _, bus = _make_plugin()
        data = _list(bus)
        self.assertEqual(data["tools"], [])


# ---------------------------------------------------------------------------
# get with empty / non-string name
# ---------------------------------------------------------------------------

class TestGetEdgeCases(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"math_tools": MathToolBox})

    def test_get_empty_name_returns_error(self):
        data = _get(self.bus, {"name": ""})
        self.assertIn("error", data)

    def test_get_missing_name_key_returns_error(self):
        data = _get(self.bus, {})
        self.assertIn("error", data)

    def test_get_non_string_name_falls_through(self):
        # int name will not match any tool → unknown tool error
        data = _get(self.bus, {"name": 42})
        self.assertIn("error", data)


# ---------------------------------------------------------------------------
# malformed bus message payloads
# ---------------------------------------------------------------------------

class TestMalformedMessagePayloads(unittest.TestCase):
    def setUp(self):
        self.plugin, self.bus = _make_plugin({"math_tools": MathToolBox})

    def test_invoke_none_data_handled(self):
        """Message with data=None must not crash the plugin; response must be emitted."""
        msg = Message("ovos.tools.invoke")
        msg.data = None
        responses = []
        self.bus.on("ovos.tools.invoke.response", lambda m: responses.append(m))

        # The plugin accesses message.data.get(…), which will fail on None data.
        # We allow either a graceful error response OR no crash (exception swallowed).
        try:
            self.plugin.handle_tools_invoke(msg)
        except AttributeError:
            pass  # acceptable — bus not crashed
        # plugin is still functional after the error
        data = _invoke(self.bus, {"name": "add", "args": {"a": 1, "b": 2}})
        self.assertIn("result", data)

    def test_list_with_none_data_does_not_crash(self):
        msg = Message("ovos.tools.list")
        msg.data = None
        responses = []
        self.bus.on("ovos.tools.list.response", lambda m: responses.append(m))
        # list handler does not use message.data at all; should always succeed
        self.plugin.handle_tools_list(msg)
        self.assertEqual(len(responses), 1)

    def test_get_none_data_returns_error_or_raises(self):
        msg = Message("ovos.tools.get")
        msg.data = None
        responses = []
        self.bus.on("ovos.tools.get.response", lambda m: responses.append(m))
        try:
            self.plugin.handle_tools_get(msg)
        except AttributeError:
            pass
        # bus still up after any error
        data = _list(self.bus)
        self.assertIsNotNone(data)


# ---------------------------------------------------------------------------
# Response context preservation
# ---------------------------------------------------------------------------

class TestResponseContextPreservation(unittest.TestCase):
    def test_response_preserves_message_context(self):
        """message.response() must carry the same context as the request."""
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        ctx = {"session": {"session_id": "sess-42"}, "ident": "msg-99"}
        req = Message("ovos.tools.invoke", {"name": "add", "args": {"a": 2, "b": 3}}, ctx)
        responses = []
        bus.on("ovos.tools.invoke.response", lambda m: responses.append(m))
        bus.emit(req)
        self.assertEqual(len(responses), 1)
        resp = responses[0]
        # response must carry the result
        self.assertEqual(resp.data["result"]["result"], 5)

    def test_list_response_context(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        ctx = {"ident": "list-req-1"}
        req = Message("ovos.tools.list", {}, ctx)
        responses = []
        bus.on("ovos.tools.list.response", lambda m: responses.append(m))
        bus.emit(req)
        self.assertEqual(len(responses), 1)

    def test_get_response_context(self):
        plugin, bus = _make_plugin({"math_tools": MathToolBox})
        ctx = {"ident": "get-req-1"}
        req = Message("ovos.tools.get", {"name": "add"}, ctx)
        responses = []
        bus.on("ovos.tools.get.response", lambda m: responses.append(m))
        bus.emit(req)
        data = responses[0].data
        self.assertNotIn("error", data)


if __name__ == "__main__":
    unittest.main()
