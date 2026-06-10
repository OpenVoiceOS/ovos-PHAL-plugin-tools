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
"""End-to-end tests for OVOSToolsPHALPlugin.

Simulates a real plugin instance on a FakeBus, injects a dummy ToolBox via a
synthetic entry-point, and exercises the full list → get → invoke round-trip.
"""
import unittest
from typing import List
from unittest.mock import patch

from ovos_bus_client import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import (
    AgentTool,
    ToolArguments,
    ToolBox,
    ToolOutput,
)


# ---------------------------------------------------------------------------
# Dummy toolbox used as the synthetic entry-point target
# ---------------------------------------------------------------------------

class MultiplyArgs(ToolArguments):
    x: float = Field(..., description="Multiplicand")
    y: float = Field(..., description="Multiplier")


class MultiplyOutput(ToolOutput):
    product: float = Field(..., description="x * y")


class E2EToolBox(ToolBox):
    """Minimal toolbox for end-to-end tests."""

    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="multiply",
                description="Multiply two floats.",
                argument_schema=MultiplyArgs,
                output_schema=MultiplyOutput,
                tool_call=lambda args: MultiplyOutput(product=args.x * args.y),
            ),
            AgentTool(
                name="boom",
                description="Always raises RuntimeError.",
                argument_schema=MultiplyArgs,
                output_schema=MultiplyOutput,
                tool_call=lambda args: (_ for _ in ()).throw(RuntimeError("boom!")),
            ),
        ]


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------

class TestE2ERoundTrip(unittest.TestCase):
    """Full round-trip: plugin instance on FakeBus, synthetic toolbox injected."""

    def setUp(self):
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin

        self.bus = FakeBus()
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"e2e_tools": E2EToolBox}):
            self.plugin = OVOSToolsPHALPlugin(bus=self.bus)

    # -- helpers ---

    def _roundtrip(self, request_event: str, payload: dict) -> dict:
        responses = []
        self.bus.on(f"{request_event}.response", lambda m: responses.append(m))
        self.bus.emit(Message(request_event, payload))
        self.assertEqual(len(responses), 1, f"Expected exactly 1 response for {request_event}")
        return responses[0].data

    # -- list -----------------------------------------------------------------

    def test_list_returns_all_tools(self):
        data = self._roundtrip("ovos.tools.list", {})
        names = [t["name"] for t in data["tools"]]
        self.assertIn("multiply", names)
        self.assertIn("boom", names)

    def test_list_tools_have_required_fields(self):
        data = self._roundtrip("ovos.tools.list", {})
        for tool in data["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("argument_schema", tool)
            self.assertIn("output_schema", tool)
            self.assertIn("toolbox_id", tool)

    # -- get ------------------------------------------------------------------

    def test_get_known_tool(self):
        data = self._roundtrip("ovos.tools.get", {"name": "multiply"})
        self.assertNotIn("error", data)
        self.assertEqual(data["name"], "multiply")
        self.assertEqual(data["toolbox_id"], "e2e_tools")
        # schema must contain the declared fields
        props = data["argument_schema"].get("properties", {})
        self.assertIn("x", props)
        self.assertIn("y", props)

    def test_get_unknown_tool_returns_error(self):
        data = self._roundtrip("ovos.tools.get", {"name": "not_a_tool"})
        self.assertIn("error", data)

    # -- invoke ---------------------------------------------------------------

    def test_invoke_multiply_success(self):
        data = self._roundtrip("ovos.tools.invoke",
                               {"name": "multiply", "args": {"x": 6.0, "y": 7.0}})
        self.assertNotIn("error", data)
        self.assertEqual(data["name"], "multiply")
        self.assertAlmostEqual(data["result"]["product"], 42.0)

    def test_invoke_tool_that_raises(self):
        data = self._roundtrip("ovos.tools.invoke",
                               {"name": "boom", "args": {"x": 1.0, "y": 1.0}})
        self.assertIn("error", data)
        self.assertEqual(data["name"], "boom")

    def test_invoke_bad_args_returns_error(self):
        data = self._roundtrip("ovos.tools.invoke",
                               {"name": "multiply", "args": {"x": "not_a_float", "y": 2.0}})
        self.assertIn("error", data)

    def test_invoke_unknown_tool_returns_error(self):
        data = self._roundtrip("ovos.tools.invoke",
                               {"name": "ghost", "args": {}})
        self.assertIn("error", data)
        self.assertEqual(data["name"], "ghost")

    # -- reload ---------------------------------------------------------------

    def test_reload_round_trip(self):
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"e2e_tools": E2EToolBox}):
            data = self._roundtrip("ovos.tools.reload", {})
        self.assertIn("e2e_tools", data["loaded"])
        self.assertEqual(data["total_tools"], 2)


if __name__ == "__main__":
    unittest.main()
