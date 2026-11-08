# Streaming and monitoring deployment

`streaming.py` implements a bounded, read-only Kite WebSocket quote feed. `deployment.py` renders Windows Task Scheduler or systemd user-service files for this feed or the existing monitor. Rendering does not register, enable or start services. Neither runner calls live execution or delivers webhook messages.

## Kite streaming

Install the optional dependency into the user's chosen Python environment:

```text
python -m pip install -r requirements-streaming.txt
python scripts/streaming.py run --workdir trader --master trader/stream-instruments.json --duration 3600 --retries 3
python scripts/streaming.py status --workdir trader
python scripts/streaming.py stop --workdir trader
python scripts/streaming.py clear-stop --workdir trader
```

`stream-instruments.json` contains `{"instruments": [...]}` with 1–500 selected, same-day normalized **Kite tradable instruments**. Use actual master tokens and supported NSE/BSE/NFO/BFO contracts. Currency, commodities and index packets are not supported by this runner; the daily index collector remains separate. Prices for supported packets are converted from paise to rupees. Expired or foreign-broker instruments block startup.

The user provides `KITE_API_KEY` and `KITE_ACCESS_TOKEN` in the local process environment. This code does not create or renew sessions. Credentials are never written to the quote cache, status or generated service files. The network client uses TLS, disables redirects and requires a successful WebSocket upgrade. Connection/subscription errors suppress provider details.

The runner subscribes in `full` mode. Complete binary packets provide both last-trade and exchange timestamps, decoded directly from epochs into IST. Short packets, unknown tokens, malformed lengths, future timestamps and contradictory clocks fail. Earlier exchange updates cannot overwrite later ones. Heartbeats and order postbacks never generate fills or synthetic candles.

Quotes are atomically stored in `state/kite-stream.json`. A process lock prevents duplicate writers. Every disconnection and final stop clears cached prices. Read consumers require a connected state, a process heartbeat within five seconds, unchanged instrument identity and fresh trade/exchange/receipt timestamps. A fresh heartbeat alone does not make an illiquid or old quote usable. A crashed process becomes stale after five seconds; status does not claim to verify process liveness.

Duration is bounded to 1–43,200 seconds, with 0–5 reconnect attempts and capped backoff. Connection establishment may take up to ten seconds and close up to one second beyond a time/stop boundary. Reconnection only resubscribes to market data. It does not retry orders. Session rollover stops the runner so tomorrow's master is not inferred from today's tokens. Stop latches persist until `clear-stop` obtains the writer lock.

To select the stream for analysis and the watcher, set `"quote_feed": "kite_stream"` in the local config. The selected `instrument.json` must exactly match a streamed master record. Missing/stopped/stale streams block this route; there is no silent REST fallback. Omit that setting to retain the existing REST/file route. Live execution still refreshes its own authoritative broker evidence. Completed candles, calendar, restrictions, account reconciliation and event evidence need independent refresh; a quote stream does not establish them. `--broker-poll` continues refreshing the watcher's history/REST inputs, with the selected stream quote applied afterward.

Transport testing used websocket-client **1.9.2** with a real local WebSocket handshake, masked subscription messages, heartbeat and close, using synthetic credentials. This is not authenticated Kite acceptance. Other brokers' streaming transports remain unimplemented.

## Deployment files

Prepare a JSON spec with `schema: "readonly_deployment_v1"`, `target: "windows"` or `"systemd"`, a lowercase `name`, absolute `python`, `skill_dir` and `workdir` paths, and `component: "monitor"` or `"stream"`.

Monitor specs additionally require integer `cycles` (1–8640), `interval` (5–60 seconds), `max_failures` (1–20), and boolean `broker_poll`. Stream specs require an absolute `master` path, `duration` (1–43,200 seconds) and `retries` (0–5). Example monitor settings:

```json
{
  "schema": "readonly_deployment_v1",
  "target": "windows",
  "name": "india-monitor",
  "python": "C:\\Python312\\python.exe",
  "skill_dir": "D:\\Skills\\india-day-trader",
  "workdir": "D:\\Trading\\trader",
  "windows_user": "YOUR-PC\\YOUR-USER",
  "component": "monitor",
  "cycles": 12,
  "interval": 5,
  "max_failures": 3,
  "broker_poll": true
}
```

These are illustrative paths and a short bounded run, not a configured service. Replace them with the user's actual environment. Rendering accepts target-platform paths without claiming the files or user exist. Review those before registration.

```text
python scripts/deployment.py render --spec trader/deployment.json --out deployment-new
python scripts/deployment.py verify --out deployment-new
```

The new directory contains a task XML or `.service` file, `operator-actions.json` with exact argument arrays for registration/start/stop/removal, and a SHA-256 manifest. `verify` checks integrity only, not operating-system acceptance. Existing directories are never overwritten. Deployment accepts only the monitor or stream component; it cannot generate an execution or notification service.

Windows uses an explicit `windows_user`, an interactive logon trigger, least privilege, and `IgnoreNew` for duplicate launches. Registration does not include a force-overwrite flag. Run the recorded registration command from the generated directory after review; starting the task is a separate action. The native Windows Task Scheduler successfully parsed a generated definition during development; no task was registered or started. The task's hidden setting hides it in the scheduler UI and is not a guarantee that the chosen Python executable suppresses its console window.

systemd output is a **user** service with `Restart=no`, a restrictive creation mask and an `ExecStop` that sets the appropriate stop latch. Follow the recorded copy/reload/enable/start commands individually. Startup is tied to the configured user manager; boot-time operation without login needs separate host configuration. An optional absolute `environment_file` path references an existing private environment file without embedding its contents. Windows tasks require credentials to be available in their actual user/task environment; an interactive terminal's temporary variables are not automatically guaranteed there. No credentials are collected by the renderer.

Both targets preserve bounded completion and stop latches; neither automatically restarts after failure. Linux activation and Windows registration/runtime behavior remain acceptance work. Updating credentials, replacing a stale master, refreshing sourced context and explicitly clearing a stop remain operator responsibilities. These services monitor data; they do not protect or flatten positions.

Contracts: [Kite WebSockets](https://kite.trade/docs/connect/v3/websocket/), [websocket-client interface](https://websocket-client.readthedocs.io/en/latest/core.html), [Microsoft Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema), [systemd user units](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html).
