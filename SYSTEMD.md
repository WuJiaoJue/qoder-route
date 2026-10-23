# Running QoderRoute as a systemd user service

**Why this exists.** DSH's `qoder-route` provider points at `http://127.0.0.1:8010/v1`,
which is a *local* proxy (this repo), not a remote API. Before this setup the proxy was
started by hand with `backend/start.sh` (`nohup … &`). It therefore died on every reboot,
and **all** Qoder (Route) models failed in DSH with:

```
重试延迟：10000 毫秒    失败原因：Connection error.
```

`Connection error.` is literally `ECONNREFUSED` on 8010 — the proxy was simply not there.
`retryPolicy.maxRetries: 20` in the DSH provider config is why it retried so insistently.

## Installed units

| File | Purpose |
|---|---|
| `~/.config/systemd/user/qoder-route.service` | The proxy (`uvicorn app.main:app` on `0.0.0.0:8010`) |
| `~/.config/systemd/user/qoder-route.service.d/watchdog.conf` | `OnFailure=qoder-route.service` — self-healing |
| `~/.config/systemd/user/qoder-route-health.service` | One-shot probe of `/api/health` |
| `~/.config/systemd/user/qoder-route-health.timer` | Runs the probe every 60s, 45s after boot |

`Linger=yes` was already enabled for this user, which is **required** — without it the
user manager (and these units) would only run while a login session is open.

## Design decisions worth knowing

**`KillMode=process`, not the default `control-group`.**
`signer_service._spawn_signer()` deliberately detaches the Node signer sidecar with
`start_new_session=True` so it "survives this Uvicorn generation" (the app's own words).
The default `KillMode=control-group` would kill that detached signer on every stop;
`KillMode=process` stops only Uvicorn and lets the next generation *adopt* the healthy
signer. This is verified: killing Uvicorn (20889 → 21623) left the signer running.

**`PATH` includes nvm's node.**
`_spawn_signer()` runs `["node", …]` via a bare `PATH` lookup, and the HTTP health check
reports `qoder_cli_version_source: "npm"`. If node were missing from the unit's `PATH` the
signer could never start and the proxy would come up permanently `degraded`.

**The watchdog probes `/api/health`, not just the port.**
`/api/health` returns **200** normally but **503 + `status: "degraded"`** when the signer is
unhealthy. A plain TCP/port check would report "up" while every real completion fails, so
the probe uses `curl -sf` (which fails on 503) to catch that case.

**`Restart=always` + `StartLimitIntervalSec=0`.**
The app is self-healing; systemd must never rate-limit it into a permanent dead state.

**No `ExecStartPre` port check.** The app's own `flock`-based singleton guard already
refuses to overlap backends — duplicating that in systemd would fight it.

## Operations

```bash
qrstatus.sh              # one-glance health (see below)
qrstatus.sh --probe      # + a real chat completion (costs a little quota)

systemctl --user status qoder-route.service
systemctl --user restart qoder-route.service
journalctl --user -u qoder-route.service -f      # live logs (replaces data/server.log)
systemctl --user list-timers qoder-route-health.timer
```

`qrstatus.sh` is a convenience wrapper at the repo root (untracked). It checks all four
layers — unit state, boot-enablement, proxy health, signer, and the watchdog timer — and
accepts `--probe` to send a real completion.

## Interaction with the repo's own scripts

`backend/start.sh` / `restart.sh` still work and remain the documented local path for
development. **Do not run `start.sh` while the service is active** — it will detect the
port as busy and exit 1 (its own safety check), which is the intended behaviour.

To go back to manual management:

```bash
systemctl --user disable --now qoder-route.service qoder-route-health.timer
```

The signer is a host-level singleton keyed by `signer/.start.lock`, so switching between
manual and systemd management does not produce duplicate signers — but if you stop the
service, stop the detached signer too (`kill "$(cat backend/signer/signer.pid)"`),
otherwise it lingers on 8123.

## Scope note

Per `AGENTS.md`, this is a **personal localhost operator tool**. These units preserve the
project's existing listen policy (`0.0.0.0:8010` for LAN panel access, signer pinned to
`127.0.0.1:8123`) and add **no auth, TLS, or internet-facing hardening** — that is out of
scope by design.
