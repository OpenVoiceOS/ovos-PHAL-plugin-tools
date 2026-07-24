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
"""ovoscope end-to-end tests for OVOSToolsPHALPlugin.

Uses MiniPHAL with plugin_factories so the plugin is always wired to the
harness FakeBus.  A synthetic ToolBox is injected via unittest.mock.patch
to avoid requiring any real toolbox entry-points at test time.

Flows asserted
--------------
list round-trip
    ovos.tools.list → ovos.tools.list.response
    response payload has ``tools`` list; each entry has name, description,
    argument_schema, output_schema, toolbox_id

get round-trip (known tool)
    ovos.tools.get {"name": "add"} → ovos.tools.get.response
    response has name, toolbox_id; argument_schema contains expected fields

get round-trip (unknown tool)
    ovos.tools.get {"name": "ghost"} → ovos.tools.get.response
    response has "error" key

invoke round-trip (success)
    ovos.tools.invoke {"name": "add", "args": {"a": 3, "b": 4}}
    → ovos.tools.invoke.response {"name": "add", "result": {"result": 7}}

invoke round-trip (tool raises)
    ovos.tools.invoke {"name": "bomb", "args": {…}}
    → ovos.tools.invoke.response {"name": "bomb", "error": "…"}

invoke missing name
    ovos.tools.invoke {} → ovos.tools.invoke.response {"error": "…"}

reload round-trip
    ovos.tools.reload → ovos.tools.reload.response
    response has "loaded" list and "total_tools" count

PHALTest declarative helper
    PHALTest.execute() with trigger=ovos.tools.list verifies
    ovos.tools.list.response appears and no error response appears
"""
from __future__ import annotations

import unittest
from typing import List
from unittest.mock import patch

from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus
from pydantic import Field

from ovos_plugin_manager.templates.agent_tools import (
    AgentTool,
    ToolArguments,
    ToolBox,
    ToolOutput,
)
from ovoscope.phal import MiniPHAL, PHALTest

# ---------------------------------------------------------------------------
# Synthetic toolbox shared across all tests
# ---------------------------------------------------------------------------

PLUGIN_ID = "ovos-phal-plugin-tools"


class AddArgs(ToolArguments):
    a: int = Field(..., description="First operand")
    b: int = Field(..., description="Second operand")


class AddOutput(ToolOutput):
    result: int = Field(..., description="Sum of a and b")


class BombArgs(ToolArguments):
    x: int = Field(default=0)


class BombOutput(ToolOutput):
    pass


def _add_logic(args: AddArgs) -> AddOutput:
    return AddOutput(result=args.a + args.b)


def _bomb_logic(args: BombArgs) -> BombOutput:
    raise RuntimeError("intentional explosion")


class SyntheticToolBox(ToolBox):
    """Minimal toolbox with two tools: add (succeeds) and bomb (always raises).

    Shaped like a real plugin: only accepts (config, bus); toolbox_id is
    supplied internally, never by the caller.
    """

    def __init__(self, config=None, bus=None):
        super().__init__(toolbox_id="synthetic_tools", config=config, bus=bus)

    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="add",
                description="Add two integers.",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=_add_logic,
            ),
            AgentTool(
                name="bomb",
                description="Always raises.",
                argument_schema=BombArgs,
                output_schema=BombOutput,
                tool_call=_bomb_logic,
            ),
        ]


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def _make_plugin_factory():
    """Return a factory that builds OVOSToolsPHALPlugin with patched toolbox discovery."""

    def factory(bus: FakeBus):
        from ovos_phal_plugin_tools import OVOSToolsPHALPlugin
        with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                   return_value={"synthetic_tools": SyntheticToolBox}):
            return OVOSToolsPHALPlugin(bus=bus)

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request_response(phal: MiniPHAL, event: str, payload: dict,
                       timeout: float = 3.0) -> Message:
    """Emit *event* and wait for *event*.response, return the response Message."""
    response_type = f"{event}.response"
    phal.emit(Message(event, payload), wait=0.05)
    return phal.assert_emitted(response_type, timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOVOSToolsPHALPluginBusAPI(unittest.TestCase):
    """True ovoscope bus-level tests for OVOSToolsPHALPlugin.

    Each test spins up a fresh MiniPHAL context with the plugin constructed
    on the harness FakeBus via plugin_factories.
    """

    # -- list -----------------------------------------------------------------

    def test_list_emits_response(self):
        """ovos.tools.list triggers ovos.tools.list.response."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.list", {})
            self.assertIn("tools", resp.data)

    def test_list_response_contains_all_tools(self):
        """Every tool registered in the toolbox appears in the list response."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.list", {})
            names = [t["name"] for t in resp.data["tools"]]
            self.assertIn("add", names)
            self.assertIn("bomb", names)

    def test_list_response_tool_schema_fields(self):
        """Each tool entry in the list response has required schema fields."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.list", {})
            for tool in resp.data["tools"]:
                for key in ("name", "description", "argument_schema", "output_schema", "toolbox_id"):
                    self.assertIn(key, tool, f"Missing key {key!r} in tool entry {tool!r}")

    def test_list_response_toolbox_id_matches(self):
        """toolbox_id in list response matches the synthetic toolbox name."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.list", {})
            for tool in resp.data["tools"]:
                self.assertEqual(tool["toolbox_id"], "synthetic_tools")

    # -- get ------------------------------------------------------------------

    def test_get_known_tool_returns_schema(self):
        """ovos.tools.get for a known tool returns its schema."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.get", {"name": "add"})
            self.assertNotIn("error", resp.data)
            self.assertEqual(resp.data["name"], "add")
            self.assertEqual(resp.data["toolbox_id"], "synthetic_tools")
            props = resp.data.get("argument_schema", {}).get("properties", {})
            self.assertIn("a", props)
            self.assertIn("b", props)

    def test_get_unknown_tool_returns_error(self):
        """ovos.tools.get for an unknown tool returns an error payload."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.get", {"name": "ghost"})
            self.assertIn("error", resp.data)
            self.assertIn("ghost", resp.data["error"])

    def test_get_missing_name_returns_error(self):
        """ovos.tools.get without 'name' field returns an error payload."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.get", {})
            self.assertIn("error", resp.data)

    # -- invoke ---------------------------------------------------------------

    def test_invoke_success_result_payload(self):
        """ovos.tools.invoke for add(3,4) returns result=7 in response."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(
                phal, "ovos.tools.invoke",
                {"name": "add", "args": {"a": 3, "b": 4}},
            )
            self.assertNotIn("error", resp.data)
            self.assertEqual(resp.data["name"], "add")
            self.assertEqual(resp.data["result"]["result"], 7)

    def test_invoke_tool_that_raises_returns_error(self):
        """ovos.tools.invoke for a tool that always raises returns error payload."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(
                phal, "ovos.tools.invoke",
                {"name": "bomb", "args": {"x": 0}},
            )
            self.assertIn("error", resp.data)
            self.assertEqual(resp.data["name"], "bomb")

    def test_invoke_unknown_tool_returns_error(self):
        """ovos.tools.invoke for an unknown tool returns error payload."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(
                phal, "ovos.tools.invoke",
                {"name": "nonexistent", "args": {}},
            )
            self.assertIn("error", resp.data)
            self.assertEqual(resp.data["name"], "nonexistent")

    def test_invoke_missing_name_returns_error(self):
        """ovos.tools.invoke without 'name' returns error with empty name."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(phal, "ovos.tools.invoke", {})
            self.assertIn("error", resp.data)
            self.assertEqual(resp.data.get("name", ""), "")

    def test_invoke_bad_args_returns_error(self):
        """ovos.tools.invoke with wrong arg types returns an error payload."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            resp = _request_response(
                phal, "ovos.tools.invoke",
                {"name": "add", "args": {"a": "not_an_int", "b": 4}},
            )
            self.assertIn("error", resp.data)

    def test_invoke_does_not_emit_get_response(self):
        """ovos.tools.invoke MUST NOT emit ovos.tools.get.response."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            phal.emit(
                Message("ovos.tools.invoke", {"name": "add", "args": {"a": 1, "b": 1}}),
                wait=0.15,
            )
            phal.assert_not_emitted("ovos.tools.get.response", wait=0.0)

    # -- reload ---------------------------------------------------------------

    def test_reload_response_payload(self):
        """ovos.tools.reload re-loads toolboxes and responds with counts."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                       return_value={"synthetic_tools": SyntheticToolBox}):
                resp = _request_response(phal, "ovos.tools.reload", {})
            self.assertIn("loaded", resp.data)
            self.assertIn("total_tools", resp.data)
            self.assertIsInstance(resp.data["loaded"], list)
            self.assertIsInstance(resp.data["total_tools"], int)

    def test_reload_does_not_emit_invoke_response(self):
        """ovos.tools.reload MUST NOT emit ovos.tools.invoke.response."""
        with MiniPHAL(
            plugin_ids=[PLUGIN_ID],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
        ) as phal:
            with patch("ovos_phal_plugin_tools.find_toolbox_plugins",
                       return_value={"synthetic_tools": SyntheticToolBox}):
                phal.emit(Message("ovos.tools.reload", {}), wait=0.15)
            phal.assert_not_emitted("ovos.tools.invoke.response", wait=0.0)

    # -- PHALTest declarative -------------------------------------------------

    def test_phal_test_list_declarative(self):
        """PHALTest.execute() verifies list response appears."""
        captured = PHALTest(
            plugin_ids=[PLUGIN_ID],
            trigger_message=Message("ovos.tools.list", {}),
            expected_types=["ovos.tools.list.response"],
            forbidden_types=["ovos.tools.invoke.response"],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
            timeout=3.0,
        ).execute()
        response_msgs = [m for m in captured if m.msg_type == "ovos.tools.list.response"]
        self.assertEqual(len(response_msgs), 1)
        self.assertIn("tools", response_msgs[0].data)

    def test_phal_test_invoke_declarative(self):
        """PHALTest.execute() verifies invoke response appears and contains result."""
        captured = PHALTest(
            plugin_ids=[PLUGIN_ID],
            trigger_message=Message("ovos.tools.invoke", {"name": "add", "args": {"a": 10, "b": 5}}),
            expected_types=["ovos.tools.invoke.response"],
            forbidden_types=["ovos.tools.list.response"],
            plugin_factories={PLUGIN_ID: _make_plugin_factory()},
            timeout=3.0,
        ).execute()
        resp = next(m for m in captured if m.msg_type == "ovos.tools.invoke.response")
        self.assertEqual(resp.data["result"]["result"], 15)


if __name__ == "__main__":
    unittest.main()
