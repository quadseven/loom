# Evidence endpoint

The evidence service is an optional launchd sibling on the lane machine. It
serves only GET requests and must bind to an address assigned to the machine's
tailnet interface. At startup it verifies the address against `tailscale ip
-4` and refuses wildcard, loopback, or non-tailnet addresses. The Tailscale
CLI must be available in launchd's `PATH`, or `LOOM_TAILSCALE_BIN` must name it.

Copy `lanes/evidence/config.example.json` to a private file outside the
checkout, restrict that file to the service account, and populate its `logs`
and `queries` maps. Do not commit the populated file. Each map key is a name
used by the HTTP endpoint.

A log entry has a `command` array. Each array item is one argument; the
service never invokes a shell. The command must contain `{since_hours}` as a
standalone argument, replaced with the validated `since` value. The caller's
`match` value filters the command's output as a fixed substring. Configure
only commands that read logs.

A log entry may instead set `file` to a station capture path. The service
reads timestamped lines from that file, applies the requested `since` window,
then filters by the fixed `match` substring. Configure `LOOM_LOG_CAPTURE`
on the station as a JSON list of objects with `source`, `namespace`, and
`selector` strings; the station writes captures under `$LOOM_HOME/logs/capture`.
`LOOM_LOG_CAPTURE_RETENTION_HOURS` controls retention and defaults to 48.

A query entry has `database`, `sql`, and `params` fields. `sql` must be one
SELECT statement using named SQLite parameters. Each declared parameter type
is `str`, `int`, `float`, or `bool`; all declared values must be present in a
request. The database is opened in read-only mode. The service runs with the
minimum filesystem permissions needed to read the configured sources.

Install with `lanes/evidence/install.sh` after setting these environment
variables:

- `LOOM_EVIDENCE_CONFIG`: private JSON config file path.
- `LOOM_EVIDENCE_TOKEN_SSM`: SSM SecureString parameter name for the bearer token.
- `LOOM_EVIDENCE_BIND_HOST`: the machine's tailnet interface address.
- `LOOM_EVIDENCE_PORT`: listen port (defaults to 7341).
- `LOOM_AWS_REGION`: region containing the token parameter.
- `LOOM_TAILSCALE_BIN`: Tailscale CLI path (defaults to `tailscale`).

The service reads the token from SSM at startup. Rotate it in SSM and restart
the launchd agent to update the in-memory token. Every request is logged with
its evidence name, request parameters, and response size. Responses contain
at most 200 lines or rows and 64 KiB; unsafe lines or rows are omitted and
counted in `dropped`.

Requests use `/evidence/logs?source=NAME&match=TEXT&since=HOURS` or
`/evidence/query?name=NAME&PARAM=VALUE`. Every call requires
`Authorization: Bearer <token>`.
