# What Buzz offers an agent

Research for [quadseven/loom#1031](https://github.com/quadseven/loom/issues/1031). Checked 2026-10-19 against Buzz 0.5.28: the installed binaries' `--help` output and the source at tag [`desktop-v0.5.28`](https://github.com/block/buzz/tree/desktop-v0.5.28). Links below point at that tag. Nothing was sent to the self-hosted relay; only `--help` was run.

## Summary

- `buzz-dev-mcp` is a coding-tools MCP server (shell, file read, edit, image, todo). It has no Buzz messaging tools. Buzz access goes through the `buzz` CLI, which the server puts on the shell's PATH.
- Claude Code can load it over stdio, but it duplicates built-in tools. Claude Code gets the same Buzz surface by calling `buzz` from Bash.
- Wake-on-mention is `buzz-acp`: it holds one WebSocket to the relay, subscribes per channel with a `#p` filter on the agent's pubkey, and prompts an ACP agent (Claude via `claude-agent-acp`) for each mention.
- Limits: the CLI refuses content over 64 KiB; the relay rejects content over 256 KB and frames over 512 KiB. Rate limits are per pubkey: 300 HTTP calls/min, 10 WebSocket frames/s, 60 or 120 persisted events/min over WebSocket.
- Reconnect in `buzz-acp` never gives up: backoff 1, 2, 4, 8, 16, 32 s, then 60 s, with jitter, and replay from the last seen timestamp minus 5 s. A 502 on the upgrade takes this path.

## 1. buzz-dev-mcp

Tools, from [`crates/buzz-dev-mcp/src/lib.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-dev-mcp/src/lib.rs) (L40-125):

| Tool | Purpose |
|---|---|
| `shell` | One process per call, bash by default (`BUZZ_SHELL` overrides). Output tail-truncated to about 8 KB, with the full output saved to an artifact file. Default timeout 120 s, maximum 1200 s. `rg`, `tree` and `buzz` are on PATH. |
| `read_file` | Numbered lines, with `offset` and `limit` (default 2000). |
| `str_replace` | Exact find-and-replace that returns a unified diff. |
| `view_image` | File, URL or data URL, returned as an MCP image block (longest edge 1568 px). |
| `todo` | Session checklist that survives compaction. |
| `_Stop`, `_PostCompact` | Internal hooks for `buzz-agent`'s lifecycle. |

- Transport: stdio only (`rmcp` with `transport-io`; `serve(stdio())` in `lib.rs` L138-185). Running `buzz-dev-mcp --help` prints `Error: ConnectionClosed("initialize request")`, which means the binary waits for an MCP initialize message on stdin. It takes no flags.
- Multicall: started as `rg`, `tree` or `buzz`, the binary runs that tool instead. [`shim.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-dev-mcp/src/shim.rs) symlinks those three names into a private temp dir at the front of PATH.
- Configuration: the working directory is the server's cwd. The server reads only `BUZZ_SHELL`. `BUZZ_RELAY_URL` and `BUZZ_PRIVATE_KEY` are inherited by the shell's `buzz` and add a "Buzz relay configured" hint to the server instructions ([`shell.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-dev-mcp/src/shell.rs) L75-90). No key is read for MCP itself.
- Claude Code: the shape `claude mcp add buzz-dev -- /Applications/Buzz.app/Contents/MacOS/buzz-dev-mcp` fits a stdio server. No live handshake was run. The tools duplicate Bash, Read and Edit, and putting the private key in MCP config would write a secret to a file. Calling the `buzz` CLI from Bash is the simpler route. Buzz Desktop passes `buzz-dev-mcp` to agents as `mcp_command` (`desktop/src-tauri/src/managed_agents/agent_events.rs` L180).

## 2. Mentions, DMs, threads, search, wake-up

CLI surface (from `buzz --help` and subcommand help; source [`crates/buzz-cli`](https://github.com/block/buzz/tree/desktop-v0.5.28/crates/buzz-cli)):

- Mention: `buzz messages send --channel <uuid> --content "@Name ..." --mention <hex|npub>` (repeatable). A mention is a `p` tag on the signed kind:9 event, and the success JSON's `mention_pubkeys` confirms delivery. Without `--mention`, `@Name` resolves against channel members, and the send fails on an unresolved name ([`base_prompt.md`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-acp/src/base_prompt.md) L43-49).
- DMs: `buzz dms open --pubkey <hex>` (1 to 8 people), `dms list`, `dms add-member`, `dms hide`. A DM is a channel with type `dm`, and messages go to it with `messages send --channel`.
- Threads: `messages send --reply-to <event-id>` starts or continues a thread. `messages thread --channel --event` (or `--link buzz://message?...`) returns the whole thread, with `--limit` and `--depth-limit`.
- Read: `messages get --channel --since/--before --limit --kinds`.
- Search: `messages search --query --author --since --limit`. This is Postgres full-text search (`to_tsvector('simple', content)`) scoped to the community, with an access check on each hit ([`crates/buzz-search/src/lib.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-search/src/lib.rs)).
- Polling without a daemon: `buzz feed get --types mentions,needs_action --since <ts>`.

Wake on mention ([`crates/buzz-acp/README.md`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-acp/README.md), `buzz-acp --help`):

- `--subscribe mentions` (the default) sends one REQ per member channel with `#h=<channel>`, `#p=<agent pubkey>`, and kinds 9, 40003, 46010 and 40007 ([`relay.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-acp/src/relay.rs) L3340-3385; [`config.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-acp/src/config.rs) L1370-1440). `all` drops the `#p` filter. `config` reads per-channel rules from `buzz-acp.toml` (`kinds`, `require_mention`). `--no-mention-filter` turns off the `#p` requirement.
- DMs use the same channel subscription, so a DM wakes the agent under `mentions` only when the message p-tags it. Whether Desktop auto-tags agents in a DM was not confirmed. `--no-mention-filter` or a `require_mention = false` rule removes the doubt.
- When the agent is added to a channel, a membership notification subscribes it automatically. At startup, the harness replays mentions that arrived while it was down.
- `--respond-to` is the author gate, applied to every inbound event (mentions, DMs, thread replies):
  - `owner-only` (the default): forwards only the owner's events, so nothing comes through until the owner resolves. Set the owner with `--agent-owner`.
  - `allowlist`: `--respond-to-allowlist` pubkeys plus the owner.
  - `anyone`: no author filtering.
  - `nobody`: heartbeat prompts only.
- The owner can send `!cancel`, `!rotate` and `!shutdown` as exact kind:9 bodies that p-tag the agent. These are checked before the gate.
- Mid-turn mentions: `--multiple-event-handling steer` is the default. The other modes are `queue`, `interrupt` and `owner-interrupt`.
- Sessions: `--session-policy channel` is the default. `thread` gives each thread its own session.
- Context: `--context-message-limit` (default 12, maximum 100) prepends recent thread or DM messages.

## 3. Size, rate and reconnect

Size:
- CLI: content up to 65,536 bytes, diffs up to 61,440 ([`buzz-cli/src/validate.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-cli/src/validate.rs) L4-7).
- Relay: event `content` up to 256 KB ([`handlers/ingest.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-relay/src/handlers/ingest.rs) L2466). WebSocket frames default to 512 KiB, set by `BUZZ_MAX_FRAME_BYTES` ([`config.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-relay/src/config.rs) L14, L743). NIP-11 advertises `max_message_length`.

Rate (per pubkey, fixed windows, shared through Redis; defaults in [`buzz-auth/src/rate_limit.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-auth/src/rate_limit.rs) L117-140, overridable by `BUZZ_RATE_LIMIT_*` on the relay):
- HTTP (`POST /events`, `/query`, `/count`, which is the CLI's path): 300 calls/min for every pubkey ([`api/bridge.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-relay/src/api/bridge.rs) L29-45). Agent API tiers exist in config but are not read at this tag.
- WebSocket EVENT, REQ and COUNT: 10 frames/s, counted as 50 per 5 s window. Persisted EVENTs over WebSocket are also limited to 60/min, or 120/min when the auth context has an `agent_owner_pubkey` (a NIP-OA auth tag, `BUZZ_AUTH_TAG`). Ephemeral kinds skip the per-minute quota ([`rejection.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-relay/src/rejection.rs) L59-100). Rejections read `rate-limited: quota exceeded; retry in Ns`.
- The relay also fails closed: if the Redis counter is unavailable, it rejects the request (`admission.rs`).

Reconnect:
- `buzz` CLI: up to 3 attempts with full jitter (0 to 0.5 s, then 0 to 1.5 s) on connect or timeout errors and on 502/503/504. A 429 waits for the relay's `retry in Ns` hint, capped at 30 s. A non-idempotent send whose response is lost returns `DeliveryUnknown` and is not retried ([`client.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-cli/src/client.rs) L122-135, L633-665). Exit code 2 means a relay or network error, and the error JSON includes `retryable`.
- `buzz-acp`:
  - Keepalive: pings every 30 s, and a missing pong after 10 s counts as a dead connection.
  - Startup: up to 5 retries.
  - After a drop: no attempt limit. Backoff steps through 1, 2, 4, 8, 16, 32 s, then 60 s, with jitter of plus or minus 20%. DNS failures retry every 2 s. The backoff step resets after 60 s of stable connection.
  - Resubscribe: one REQ every 125 ms, so the relay frame limit is not hit.
  - Replay: from the last `created_at` seen in each channel minus 5 s, with duplicate events dropped by ID (12,000 IDs kept).
  - Source: [`relay.rs`](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-acp/src/relay.rs) L47-107, L3210-3320.
  - The 0.5.x 502 reconnects in the logs are handled this way. The README says delivery is not guaranteed if relay history is gone.

## 4. buzz-agent, buzz-acp, and an always-on Claude agent

- `buzz-agent` ([README](https://github.com/block/buzz/blob/desktop-v0.5.28/crates/buzz-agent/README.md)) is Buzz's own small ACP agent: it reads JSON-RPC on stdin, calls an LLM API without streaming, and runs tools over MCP (normally `buzz-dev-mcp`). It is configured only through environment variables, and `BUZZ_AGENT_PROVIDER` is required (`anthropic`, `openai`, `openrouter`, `databricks`); with it unset, the binary exits at startup, as `--help` shows. `BUZZ_AGENT_REQUIRE_REPLY=1` re-prompts the model (at most twice) when a turn ends without a `buzz messages send` or `reactions add`. It is not Claude Code.
- `buzz-acp` is the always-on harness. It connects with NIP-42 using the agent's key, finds the channels it belongs to, queues events per channel, and sends each batch to N ACP subprocesses (`--agents`, 1 to 32, all sharing one identity) as `session/prompt`. The agent replies by running the `buzz` CLI. `buzz-acp run --task <file|->` runs one local task and exits.
- Claude: `npm install -g @agentclientprotocol/claude-agent-acp`, then `BUZZ_ACP_AGENT_COMMAND=claude-agent-acp`, `ANTHROPIC_API_KEY`, `BUZZ_RELAY_URL`, `BUZZ_PRIVATE_KEY`, and run `buzz-acp` (README "Running with Claude Code", L88). Optional: `BUZZ_ACP_MCP_COMMAND=buzz-dev-mcp`.
- Settings that matter for an unattended agent:
  - `--permission-mode` defaults to `bypass-permissions`.
  - Set `--respond-to` and `--agent-owner` explicitly, since the default `owner-only` drops everything until an owner resolves.
  - `--idle-timeout` (README default 620 s) and `--max-turn-duration` (7200 s) cap a turn.
  - `--heartbeat-interval` (0 or at least 10 s) prompts the agent while idle.
  - `--lazy-pool` with `--idle-pool-sleep` starts and stops worker processes on demand.
  - `--exit-after-inactivity 0` (the default) keeps the harness running.
  - `buzz-acp auth-methods` and `buzz-acp authenticate` exist. Whether `claude-agent-acp` accepts a subscription login instead of an API key was not checked.
- The harness also sets up Git signing and credentials for the relay's `/git`, puts the agent online, and shows typing indicators. `--no-presence` and `--no-typing` turn the last two off.

## Open points

- DM wake without an explicit p-tag (section 2).
- A live `tools/list` from `buzz-dev-mcp` under Claude Code (section 1).
- Which rate tier the self-hosted relay assigns to an agent key without a NIP-OA auth tag (likely the 60/min human tier).
