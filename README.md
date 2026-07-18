# hermes-channel-molecule

Hermes platform plugin that connects an external
[hermes-agent](https://git.moleculesai.app/molecule-ai/hermes-agent) to the
[Molecule](https://moleculesai.app) platform.

## What this is for

Run this plugin when Hermes lives on your own machine but represents an
external Molecule workspace. Canvas messages and peer delegations arrive as
normal Hermes platform events, and replies go back through the same gateway
path used by Telegram, Discord, and Slack.

If Hermes runs inside a Molecule workspace container, use
[hermes-platform-molecule-a2a](https://git.moleculesai.app/molecule-ai/hermes-platform-molecule-a2a)
instead. That integration uses the in-container callback path.

## Architecture

```text
hermes-agent
    |
    | stdio JSON-RPC
    v
python -m molecule_runtime.mcp_cli
    |-- register + heartbeat the external workspace
    |-- poll the platform inbox
    `-- expose Molecule MCP tools
    |
    v
Molecule canvas messages and peer A2A tasks
```

`MoleculeAdapter.connect()` launches the standalone runtime entry point,
completes the MCP `initialize` handshake, and calls `wait_for_message` in the
background. The runtime entry point—not the bare in-container MCP module—is
required because it owns external registration, heartbeat, and inbox polling.

## Install

`hermes-channel-molecule` is not published on public PyPI. The preferred path
is the ready-to-paste **Hermes** block in Canvas under the workspace's
**Config → External Connection** section. It pins reviewed versions and guards
the gateway restart when installation or token disclosure is incomplete.

For a manual install, download the private runtime wheel without dependencies,
resolve that wheel's public dependencies only from PyPI, then install this
plugin from canonical Gitea source:

```bash
set -euo pipefail
runtime_download="$(mktemp -d)"
trap 'rm -rf "$runtime_download"' EXIT

python3 -m pip download --no-deps --dest "$runtime_download" \
  --index-url https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/ \
  molecules-workspace-runtime==0.4.26
python3 -m pip install --index-url https://pypi.org/simple/ \
  "$runtime_download"/molecules_workspace_runtime-*.whl
python3 -m pip install --no-deps \
  'git+https://git.moleculesai.app/molecule-ai/hermes-channel-molecule.git'
```

The split-index sequence is intentional: the private Gitea registry does not
proxy public dependencies, while an unrestricted `--extra-index-url` would
reintroduce dependency-confusion ambiguity for a private distribution.

The installed package exposes the `molecule` entry point in the
`hermes_agent.plugins` group. Enable that pip plugin explicitly:

```bash
hermes plugins enable molecule
```

## Configure

The minimum explicit `~/.hermes/config.yaml` shape is:

```yaml
plugins:
  enabled:
    - molecule

platforms:
  molecule:
    enabled: true
```

`platforms.molecule` is a top-level public config key. Do not nest it under
`gateway`, and do not use `plugin_platforms`; current Hermes does not parse
either YAML shape.

Get the workspace ID, tenant URL, and credential from Canvas under the
workspace's **Config → External Connection** section. A newly minted token is
shown only once. Re-opening the connection details deliberately omits the
token while leaving the current credential active. If the saved value is lost,
use **Rotate credentials** to revoke it and mint a replacement, then update the
environment before restarting Hermes.

```bash
export MOLECULE_WORKSPACE_ID=ws-...
export MOLECULE_PLATFORM_URL=https://your-tenant.moleculesai.app
export MOLECULE_WORKSPACE_TOKEN=...
```

External Hermes runs must supply `MOLECULE_WORKSPACE_TOKEN`; they do not have
the platform-managed `/configs/.auth_token` volume. Without a real token,
registration and platform calls fail authentication.

Optional overrides:

```bash
# Interpreter that contains molecule_runtime. Normally this is the Python
# environment where Hermes and this plugin were installed.
export MOLECULE_MCP_PYTHON=/path/to/venv/bin/python3

# Runtime state directory. External runtime default: ~/.molecule-workspace.
export MOLECULE_CONFIGS_DIR=/path/to/private/runtime-state
```

Only replace a running gateway after the install, config, and one-time token
have been verified:

```bash
hermes gateway --replace
```

The gateway log should include:

```text
[molecule] MCP initialize ok (server=molecule)
```

## Verify end to end

First confirm Hermes sees the enabled entry-point plugin:

```bash
hermes plugins list
python3 - <<'PY'
from importlib.metadata import entry_points

matches = list(entry_points(group="hermes_agent.plugins", name="molecule"))
assert len(matches) == 1, matches
assert matches[0].value == "hermes_channel_molecule", matches[0]
print("molecule plugin entry point: ok")
PY
```

Then send a Canvas message to the external workspace and verify that Hermes
receives it and its reply appears in Canvas. A peer workspace delegation tests
the second route. The channel encodes reply routing in the Hermes chat ID:

| `chat_id` | Origin | Reply tool |
|---|---|---|
| `canvas:<workspace_id>` | Canvas user | `send_message_to_user` |
| `peer:<peer_workspace_id>` | Peer agent | `delegate_task` |

## Development

Bootstrap `molecules-workspace-runtime==0.4.26` with the split-index commands
above, then install the checkout and run the suite:

```bash
git clone https://git.moleculesai.app/molecule-ai/hermes-channel-molecule.git
cd hermes-channel-molecule
python3 -m pip install -e ".[test]"
python3 -m pytest -q
```

Most tests use a fake stdio MCP server to cover framing and routing quickly.
The real-runtime regression additionally launches
`molecule_runtime.mcp_cli` against a local HTTP server and proves external
registration, heartbeat, inbox polling, MCP handshake, and inbound dispatch.

## License

Apache-2.0. See [LICENSE](LICENSE).
