"""A tool name two toolboxes own is answered with an error (OVOS-TOOLS-1 §5).

"A tool service resolves `name` across every toolbox it loaded; when two
toolboxes own the same name the service MUST answer with an error rather than
pick one, since the flat namespace cannot address the pair of §3."
"""
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from ovos_bus_client import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import AgentTool, ToolArguments, ToolBox, ToolOutput

CALLS: List[str] = []


class TextArgs(ToolArguments):
    text: str = Field(..., description="Text")


class CountOutput(ToolOutput):
    words: int


def _toolbox(toolbox_id: str, offset: int):
    def _count(args: TextArgs) -> CountOutput:
        CALLS.append(toolbox_id)
        return CountOutput(words=len(args.text.split()) + offset)

    class _Box(ToolBox):
        def __init__(self, config: Optional[Dict[str, Any]] = None, bus=None):
            super().__init__(toolbox_id=toolbox_id, config=config, bus=bus)

        def discover_tools(self) -> List[AgentTool]:
            return [AgentTool(name="word_count", description="Count words.",
                              argument_schema=TextArgs, output_schema=CountOutput, tool_call=_count)]
    return _Box


PLUGINS = {"my-text-tools": _toolbox("my-text-tools", 0),
           "other-text-tools": _toolbox("other-text-tools", 1000)}


def _ask(bus, topic, data):
    got = []
    bus.on(topic + ".response", got.append)
    bus.emit(Message(topic, data))
    return [m.data for m in got]


class TestDuplicateToolName(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin
        self.bus = FakeBus()
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins", return_value=PLUGINS):
            self.plugin = OVOSToolsPHALPlugin(bus=self.bus, config={})

    def test_invoke_answers_an_error_and_runs_nothing(self):
        answers = _ask(self.bus, "ovos.tools.invoke", {"name": "word_count", "args": {"text": "a b c"}})
        self.assertEqual(answers, [{
            "name": "word_count",
            "error": "Ambiguous tool: 'word_count' is owned by toolboxes 'my-text-tools', 'other-text-tools'",
        }])
        self.assertEqual(CALLS, [])

    def test_get_answers_an_error(self):
        answers = _ask(self.bus, "ovos.tools.get", {"name": "word_count"})
        self.assertEqual(answers, [{
            "error": "Ambiguous tool: 'word_count' is owned by toolboxes 'my-text-tools', 'other-text-tools'",
        }])

    def test_list_still_shows_both_tools_with_their_toolbox(self):
        tools = _ask(self.bus, "ovos.tools.list", {})[0]["tools"]
        self.assertEqual(sorted((t["toolbox_id"], t["name"]) for t in tools),
                         [("my-text-tools", "word_count"), ("other-text-tools", "word_count")])

    def test_each_toolbox_still_answers_its_own_call(self):
        mine = _ask(self.bus, "ovos.persona.tools.my-text-tools.call",
                    {"name": "word_count", "kwargs": {"text": "a b c"}})
        other = _ask(self.bus, "ovos.persona.tools.other-text-tools.call",
                     {"name": "word_count", "kwargs": {"text": "a b c"}})
        self.assertEqual([a["result"] for a in mine], [{"words": 3}])
        self.assertEqual([a["result"] for a in other], [{"words": 1003}])
