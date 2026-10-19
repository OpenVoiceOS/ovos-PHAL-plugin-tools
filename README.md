# ovos-PHAL-plugin-tools

This plugin gives every program on an OVOS device the same set of tools. It
loads each installed OVOS tool plugin (a `ToolBox`, entry-point group
`opm.agents.toolbox`) and answers four messagebus requests: list the tools, get
one tool's schema, run a tool, and reload the set.

A skill, an agent, a GUI or a remote client can then find and run a tool with
one bus message. It does not need to import the plugin or know which package
the tool came from.

## What you can do

- List every tool on the device, with the JSON Schema of its arguments and of
  its result.
- Run a tool by name and get its result back as JSON.
- Install another tool plugin and load it without a restart.
- Reach each toolbox directly on the bus topics of the agent tools contract,
  [OVOS-TOOLS-1](https://github.com/OpenVoiceOS/architecture/blob/dev/tools-1.md).
  The plugin is the "tool service" of that specification.

The tools come from other packages. For example,
[ovos-agentic-loop](https://github.com/OpenVoiceOS/ovos-agentic-loop) installs
clock, math, file, shell and web-search toolboxes, and many OVOS skills ship a
toolbox (weather, Wikipedia, WordNet and others). The table of known toolboxes
is in the
[ovos-plugin-manager agent docs](https://github.com/OpenVoiceOS/ovos-plugin-manager/blob/dev/docs/agents.md).

## User stories

### Give every program on my device the same tools

You run OVOS on a device and want a skill, an agent and a script to share one
set of tools.

1. Install the plugin and at least one toolbox. Here the toolboxes come from
   ovos-agentic-loop:

   ```bash
   pip install ovos-PHAL-plugin-tools ovos-agentic-loop
   ```

2. Enable the plugin in `mycroft.conf`:

   ```json
   {
     "PHAL": {
       "ovos-phal-plugin-tools": {"enabled": true}
     }
   }
   ```

3. Restart the PHAL service (`ovos_PHAL`). Its log names every toolbox it
   found:

   ```text
   [ovos-phal-plugin-tools] found 6 toolbox plugin(s): ['ovos-clock-tools', 'ovos-filesystem-tools', 'ovos-math-tools', 'ovos-shell-tools', 'ovos-skill-md-toolbox', 'ovos-web-search-tools']
   ```

The tools are then on the bus. Read the security notes under
[Deployment](#deployment) before you enable toolboxes that write files or run
commands.

### Call a tool from my own program

You write a script or a skill and want it to run a tool on the device.

```python
from ovos_bus_client import MessageBusClient, Message

bus = MessageBusClient()   # the device bus, 127.0.0.1:8181 by default
bus.run_in_thread()
bus.connected_event.wait()

reply = bus.wait_for_response(
    Message("ovos.tools.invoke",
            {"name": "evaluate_expression", "args": {"expression": "17*23"}}))
print(reply.data)
```

The reply on a device with ovos-agentic-loop installed:

```json
{"name": "evaluate_expression", "result": {"result": 391.0, "expression": "17*23", "error": null}}
```

When the call fails, the reply carries `error` in place of `result`:

```json
{"name": "evaluate_expression", "error": "ValueError: Tool input validation failed for 'evaluate_expression' in ToolBox 'ovos-math-tools'"}
```

Send `ovos.tools.list` first to see every tool name and its argument schema.

### Add a tool while OVOS runs

You installed another tool plugin and do not want to restart OVOS.

1. Install the plugin with a regular install, for example
   `pip install my-text-tools`.
2. Send `ovos.tools.reload`. The answer lists every tool after the reload
   (trimmed here to the added toolbox):

   ```json
   {"tools": [{"name": "word_count", "description": "Count the words in a piece of text.",
               "argument_schema": {"...": "..."}, "output_schema": {"...": "..."},
               "toolbox_id": "my-text-tools"}],
    "loaded": ["ovos-clock-tools", "ovos-filesystem-tools", "ovos-math-tools",
               "ovos-shell-tools", "ovos-skill-md-toolbox", "ovos-web-search-tools",
               "my-text-tools"],
    "total_tools": 13}
   ```

3. Call the added tool:

   ```json
   {"name": "word_count", "result": {"words": 7}}
   ```

A plugin installed in editable mode (`pip install -e`) is not importable by the
running process. Restart PHAL for those.

To write a tool plugin, follow the
[ovos-plugin-manager tool plugin guide](https://github.com/OpenVoiceOS/ovos-plugin-manager/blob/dev/docs/api/agent-tools.md).

### Two toolboxes use the same tool name

The tool service has one flat list of names. When two toolboxes both own a
tool called `word_count`, `ovos.tools.get` and `ovos.tools.invoke` refuse to
pick one and run nothing:

```json
{"name": "word_count", "error": "Ambiguous tool: 'word_count' is owned by toolboxes 'my-text-tools', 'other-text-tools'"}
```

`ovos.tools.list` still shows both tools, each with its `toolbox_id`. To run
one of them, address its toolbox directly:

```python
bus.wait_for_response(
    Message("ovos.persona.tools.call",
            {"toolbox_id": "my-text-tools", "name": "word_count",
             "kwargs": {"text": "a b c"}}))
```

`ovos.persona.tools.call` needs ovos-plugin-manager with OVOS-TOOLS-1 support.
Older versions answer only the per-toolbox topic
`ovos.persona.tools.my-text-tools.call`, with the same payload minus
`toolbox_id`.

## Bus API reference

All requests follow the request and response pattern. The response topic is the
request topic followed by `.response`.

### `ovos.tools.list`

List every tool of every loaded toolbox. The request carries no fields.

**Response: `ovos.tools.list.response`**

| Field | Type | Description |
|-------|------|-------------|
| `tools` | `list[ToolEntry]` | Every loaded tool |

**ToolEntry fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Tool name |
| `description` | `str` | What the tool does, written for a model |
| `argument_schema` | `dict` | JSON Schema of the call arguments |
| `output_schema` | `dict` | JSON Schema of the result |
| `toolbox_id` | `str` | The toolbox that owns the tool |

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

### `ovos.tools.get`

Get the schema of one tool.

**Request payload:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `str` | yes | Tool name |

**Response: `ovos.tools.get.response`**

On success, one ToolEntry (see `ovos.tools.list`). On failure, one field:

| Field | Type | Description |
|-------|------|-------------|
| `error` | `str` | `Missing required field: 'name'`, `Unknown tool: '<name>'` or `Ambiguous tool: '<name>' is owned by toolboxes ...` |

### `ovos.tools.invoke`

Run a tool and get its result.

**Request payload:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `str` | yes | Tool name |
| `args` | `dict` | no | Arguments that match the tool's `argument_schema`. Absent means `{}`. |

**Response: `ovos.tools.invoke.response`**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | The tool name, echoed |
| `result` | `dict` | The tool output, valid against `output_schema` (success only) |
| `error` | `str` | Why the call failed (failure only) |

The `error` is one of the three `ovos.tools.get` errors, or, when the tool ran
and failed, the exception class name, `: ` and the message.

### `ovos.tools.reload`

Load toolbox plugins installed since the last load, refresh the tools of every
loaded toolbox, and drop toolboxes whose plugin is gone. The request carries no
fields.

**Response: `ovos.tools.reload.response`**

| Field | Type | Description |
|-------|------|-------------|
| `tools` | `list[ToolEntry]` | Every tool after the reload, as in `ovos.tools.list` |
| `loaded` | `list[str]` | The toolbox ids loaded after the reload |
| `total_tools` | `int` | The number of tools |

### Toolbox topics

Each loaded toolbox also answers the toolbox topics of OVOS-TOOLS-1 §4 itself:
`ovos.persona.tools.discover` (one answer per toolbox) and
`ovos.persona.tools.call` (addressed by `data.toolbox_id`). A reload keeps one
set of these handlers per toolbox.

## Deployment

### Install and enable

```bash
pip install ovos-PHAL-plugin-tools
```

```json
{
  "PHAL": {
    "ovos-phal-plugin-tools": {"enabled": true}
  }
}
```

### Configure a toolbox

Each toolbox reads its own section, nested under the plugin's section and keyed
by the toolbox id:

```json
{
  "PHAL": {
    "ovos-phal-plugin-tools": {
      "enabled": true,
      "ovos-filesystem-tools": {
        "allow_write": false,
        "root_path": "/home/ovos/shared"
      }
    }
  }
}
```

With this config, a `write_file` call answers
`"Write access is disabled (allow_write=False)."` and a `read_file` call for
`/etc/hostname` answers `"Access denied: path '/etc/hostname' is outside the
sandbox root."`.

A toolbox reads its config when it loads. A change to its config needs a PHAL
restart.

### Security

The plugin loads **every** installed toolbox and answers **every** client on
the messagebus. The bus has no authentication, so any program that can reach
it can run any loaded tool.

- Keep the messagebus bound to `127.0.0.1`, which is the OVOS default.
- `ovos-filesystem-tools` from ovos-agentic-loop can write files by default
  (`allow_write` is `true`). Its sandbox is `root_path`, and the default is the
  working directory of the PHAL process. Set `allow_write` to `false`, or set
  `root_path` to a directory made for it, as in the example above.
- `ovos-shell-tools` refuses every command until you set `allow_shell` to
  `true`. Combine it with `allowed_commands` if you turn it on.
- Do not install a toolbox you do not want every bus client to use.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Unknown tool: '<name>'` for a tool you just installed | Send `ovos.tools.reload`. If the plugin is an editable install, restart PHAL. |
| The PHAL log shows `Failed to load plugin entry point ... No module named ...` | The plugin's package is not importable by the PHAL process. Install it in the same Python environment as OVOS. |
| The PHAL log shows `failed to load toolbox '<id>'` with a `TypeError` | The toolbox constructor does not accept `config` and `bus` keyword arguments. Hosts build every toolbox as `cls(config=..., bus=...)`. |
| `Ambiguous tool: ...` | Two toolboxes own that name. Address one with `ovos.persona.tools.call` and its `toolbox_id`, or remove one plugin. |
| `ovos-mcp-toolbox` or `ovos-utcp-toolbox` lists no tools | These toolboxes need a server address in their config section. See [ovos-tool-adapters](https://github.com/OpenVoiceOS/ovos-tool-adapters). |

## Related projects

- [ovos-plugin-manager](https://github.com/OpenVoiceOS/ovos-plugin-manager)
  defines the `ToolBox` template and the `opm.agents.toolbox` plugin group.
- [ovos-agentic-loop](https://github.com/OpenVoiceOS/ovos-agentic-loop) runs an
  LLM agent loop that uses toolboxes, and ships several toolboxes.
- [ovos-tool-adapters](https://github.com/OpenVoiceOS/ovos-tool-adapters) turns
  an MCP server or a UTCP manual into a toolbox.
- [ovos-bus-client](https://github.com/OpenVoiceOS/ovos-bus-client) is the
  messagebus client used to send `ovos.tools.*` requests.
- [ovos-PHAL](https://github.com/OpenVoiceOS/ovos-PHAL) is the PHAL framework
  that loads this plugin.

## Credits

Developed by [TigreGótico](https://tigregotico.pt) for
[OpenVoiceOS](https://openvoiceos.org).

[![NGI0 Commons Fund](./ngi.png)](https://nlnet.nl/project/OpenVoiceOS)

This project was funded through the [NGI0 Commons Fund](https://nlnet.nl/commonsfund),
a fund established by [NLnet](https://nlnet.nl) with financial support from the
European Commission's [Next Generation Internet](https://ngi.eu) programme, under
the aegis of [DG Communications Networks, Content and Technology](https://commission.europa.eu/about-european-commission/departments-and-executive-agencies/communications-networks-content-and-technology_en)
under grant agreement No [101135429](https://cordis.europa.eu/project/id/101135429).

## License

Apache 2.0. See [LICENSE](LICENSE).
