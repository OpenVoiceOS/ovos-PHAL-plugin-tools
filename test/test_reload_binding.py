"""ovos.tools.reload keeps one set of bus handlers per toolbox (OVOS-TOOLS-1 §4.1).

A reload must not leave earlier toolbox instances subscribed: each one answers
the toolbox topics and runs the tool again, so a call made after N reloads ran
N + 1 times.
"""
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from ovos_bus_client import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import AgentTool, ToolArguments, ToolBox, ToolOutput

CALLS: List[int] = []


class CountArgs(ToolArguments):
    n: int = Field(..., description="A number")


class CountOutput(ToolOutput):
    n: int


def _count(args: CountArgs) -> CountOutput:
    CALLS.append(args.n)
    return CountOutput(n=args.n)


class CountToolBox(ToolBox):
    def __init__(self, config: Optional[Dict[str, Any]] = None, bus=None):
        super().__init__(toolbox_id="count_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [AgentTool(name="count", description="Record a number.",
                          argument_schema=CountArgs, output_schema=CountOutput, tool_call=_count)]


def _plugin(plugins):
    from ovos_phal_plugin_tools import OVOSToolsPHALPlugin
    bus = FakeBus()
    with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value=plugins):
        plugin = OVOSToolsPHALPlugin(bus=bus, config={})
    return plugin, bus


def _reload(bus, plugins):
    with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value=plugins):
        bus.emit(Message("ovos.tools.reload"))


def _answers(bus, topic, data):
    got = []
    bus.on(topic + ".response", got.append)
    bus.emit(Message(topic, data))
    bus.remove(topic + ".response", got.append)
    return got


class TestReloadBinding(unittest.TestCase):
    def setUp(self):
        CALLS.clear()

    def test_toolbox_call_answers_once_and_runs_once_after_reloads(self):
        plugin, bus = _plugin({"count_tools": CountToolBox})
        _reload(bus, {"count_tools": CountToolBox})
        _reload(bus, {"count_tools": CountToolBox})

        got = _answers(bus, "ovos.persona.tools.count_tools.call", {"name": "count", "kwargs": {"n": 7}})

        self.assertEqual([m.data["result"] for m in got], [{"n": 7}])
        self.assertEqual(CALLS, [7])

    def test_discover_answers_once_per_toolbox_after_reloads(self):
        plugin, bus = _plugin({"count_tools": CountToolBox})
        _reload(bus, {"count_tools": CountToolBox})

        got = _answers(bus, "ovos.persona.tools.discover", {})

        self.assertEqual([m.data["toolbox_id"] for m in got], ["count_tools"])

    def test_invoke_runs_the_tool_once_after_reloads(self):
        plugin, bus = _plugin({"count_tools": CountToolBox})
        _reload(bus, {"count_tools": CountToolBox})

        got = _answers(bus, "ovos.tools.invoke", {"name": "count", "args": {"n": 3}})

        self.assertEqual([m.data["result"] for m in got], [{"n": 3}])
        self.assertEqual(CALLS, [3])

    def test_removed_toolbox_stops_answering(self):
        plugin, bus = _plugin({"count_tools": CountToolBox})
        _reload(bus, {})

        self.assertEqual(_answers(bus, "ovos.persona.tools.count_tools.call",
                                  {"name": "count", "kwargs": {"n": 1}}), [])
        self.assertEqual(_answers(bus, "ovos.persona.tools.discover", {}), [])
        self.assertEqual(_answers(bus, "ovos.persona.tools.call",
                                  {"toolbox_id": "count_tools", "name": "count", "kwargs": {"n": 1}}), [])
        self.assertEqual(CALLS, [])
