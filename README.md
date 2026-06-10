# ovos-PHAL-plugin-tools

A PHAL (Platform / Hardware Abstraction Layer) service provider that exposes all
installed OPM **ToolBox** plugins (`opm.agents.toolbox` entry-point group) as a
unified bus API.

Any skill, agent, or external client connected to the OVOS messagebus can:

- enumerate every available tool and its JSON Schema
- fetch a single tool's full schema
- invoke a tool by name with keyword arguments

---

## Bus API reference

All events follow the request → response pattern. The response event name is the
request event name suffixed with `.response`.

### `ovos.tools.list`

Enumerate all tools registered across every loaded toolbox.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| *(no payload fields)* | | | |

**Response: `ovos.tools.list.response`**

| Field | Type | Description |
|-------|------|-------------|
| `tools` | `list[ToolEntry]` | All discovered tools |

**ToolEntry fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Unique tool name (snake_case) |
| `description` | `str` | Natural-language description |
| `argument_schema` | `dict` | JSON Schema for call arguments |
| `output_schema` | `dict` | JSON Schema for the return value |
| `toolbox_id` | `str` | Originating toolbox entry-point name |

**Example response payload:**

```json
{
  "tools": [
    {
      "name": "add",
      "description": "Add two integers.",
      "argument_schema": {
        "title": "AddArgs",
        "type": "object",
        "properties": {
          "a": {"title": "A", "type": "integer"},
          "b": {"title": "B", "type": "integer"}
        },
        "required": ["a", "b"]
      },
      "output_schema": {
        "title": "AddOutput",
        "type": "object",
        "properties": {
          "result": {"title": "Result", "type": "integer"}
        },
        "required": ["result"]
      },
      "toolbox_id": "math_tools"
    }
  ]
}
```

---

### `ovos.tools.get`

Retrieve the full schema of a single named tool.

**Request payload:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `str` | yes | The snake_case tool name |

**Response: `ovos.tools.get.response`**

Success:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Tool name |
| `description` | `str` | Natural-language description |
| `argument_schema` | `dict` | JSON Schema for call arguments |
| `output_schema` | `dict` | JSON Schema for return value |
| `toolbox_id` | `str` | Originating toolbox |

Error (unknown tool or missing `name`):

| Field | Type | Description |
|-------|------|-------------|
| `error` | `str` | Human-readable error message |

**Example:**

```json
// request
{"name": "add"}

// response (success)
{
  "name": "add",
  "description": "Add two integers.",
  "argument_schema": { ... },
  "output_schema":   { ... },
  "toolbox_id": "math_tools"
}

// response (error)
{"error": "Unknown tool: 'nonexistent'"}
```

---

### `ovos.tools.invoke`

Execute a tool synchronously and receive its result.

**Request payload:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `str` | yes | The snake_case tool name |
| `args` | `dict` | yes | Keyword arguments matching the tool's `argument_schema` |

**Response: `ovos.tools.invoke.response`**

Success:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Tool name (echoed) |
| `result` | `dict` | Tool output, matching `output_schema` |

Error:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Tool name (echoed) |
| `error` | `str` | `ExceptionType: message` |

**Example:**

```json
// request
{"name": "add", "args": {"a": 3, "b": 4}}

// response (success)
{"name": "add", "result": {"result": 7}}

// response (error — bad args)
{"name": "add", "error": "ValueError: Tool input validation failed for 'add' ..."}

// response (error — unknown tool)
{"name": "nope", "error": "Unknown tool: 'nope'"}
```

---

### `ovos.tools.reload`

Hot-reload the toolbox registry without restarting OVOS.  Useful after
installing new toolbox plugins at runtime.

**Request payload:** *(none)*

**Response: `ovos.tools.reload.response`**

| Field | Type | Description |
|-------|------|-------------|
| `loaded` | `list[str]` | Toolbox entry-point names now loaded |
| `total_tools` | `int` | Total number of tools across all toolboxes |

---

## Third-party usage example

```python
from ovos_bus_client import MessageBusClient, Message

bus = MessageBusClient()
bus.run_in_thread()

# --- list all tools ---
response = bus.wait_for_response(Message("ovos.tools.list"))
for tool in response.data["tools"]:
    print(tool["name"], "—", tool["description"])

# --- get schema for one tool ---
response = bus.wait_for_response(
    Message("ovos.tools.get", {"name": "add"})
)
print(response.data)

# --- invoke a tool ---
response = bus.wait_for_response(
    Message("ovos.tools.invoke", {"name": "add", "args": {"a": 10, "b": 32}})
)
if "error" in response.data:
    print("Error:", response.data["error"])
else:
    print("Result:", response.data["result"])
```

---

## Installation

```bash
pip install ovos-PHAL-plugin-tools
```

Enable in your OVOS config (`mycroft.conf`):

```json
{
  "PHAL": {
    "ovos-phal-plugin-tools": {
      "enabled": true
    }
  }
}
```

Install one or more toolbox plugins (entry-point group `opm.agents.toolbox`) and
the service will discover them automatically at startup.  Call
`ovos.tools.reload` if you install plugins while OVOS is already running.

---

## Writing a ToolBox plugin

Implement `ovos_plugin_manager.templates.agent_tools.ToolBox` and register under
the `opm.agents.toolbox` entry-point group in your package's `pyproject.toml`:

```toml
[project.entry-points."opm.agents.toolbox"]
my-math-tools = "my_package.toolbox:MyMathToolBox"
```

```python
from typing import List
from pydantic import Field
from ovos_plugin_manager.templates.agent_tools import (
    AgentTool, ToolArguments, ToolOutput, ToolBox,
)


class AddArgs(ToolArguments):
    a: int = Field(..., description="First operand")
    b: int = Field(..., description="Second operand")


class AddOutput(ToolOutput):
    result: int = Field(..., description="Sum")


class MyMathToolBox(ToolBox):
    def discover_tools(self) -> List[AgentTool]:
        return [
            AgentTool(
                name="add",
                description="Add two integers.",
                argument_schema=AddArgs,
                output_schema=AddOutput,
                tool_call=lambda args: AddOutput(result=args.a + args.b),
            )
        ]
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
