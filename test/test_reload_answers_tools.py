"""ovos.tools.reload answers the refreshed tool list (OVOS-TOOLS-1 §5).

§5 table: `ovos.tools.reload` | none read | `tools`: the list after every
toolbox refreshed.
"""
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from ovos_bus_client import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import AgentTool, ToolArguments, ToolBox, ToolOutput


class NArgs(ToolArguments):
    n: int = Field(..., description="A number")


class NOut(ToolOutput):
    n: int


class EchoToolBox(ToolBox):
    def __init__(self, config: Optional[Dict[str, Any]] = None, bus=None):
        super().__init__(toolbox_id="echo_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [AgentTool(name="echo", description="Echo a number.",
                          argument_schema=NArgs, output_schema=NOut, tool_call=lambda a: NOut(n=a.n))]


def _ask(bus, topic):
    got = []
    bus.on(topic + ".response", got.append)
    bus.emit(Message(topic))
    return got[0].data


class TestReloadAnswersTools(unittest.TestCase):
    def test_reload_answers_the_same_tools_as_list(self):
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin
        bus = FakeBus()
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value={"echo_tools": EchoToolBox}):
            OVOSToolsPHALPlugin(bus=bus, config={})
            reloaded = _ask(bus, "ovos.tools.reload")

        self.assertEqual([(t["toolbox_id"], t["name"]) for t in reloaded["tools"]], [("echo_tools", "echo")])
        self.assertEqual(reloaded["tools"], _ask(bus, "ovos.tools.list")["tools"])
        self.assertEqual(reloaded["loaded"], ["echo_tools"])
        self.assertEqual(reloaded["total_tools"], 1)
