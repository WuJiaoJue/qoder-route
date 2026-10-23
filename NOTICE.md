# Fork Notice

This repository is a **private-use fork** of <https://github.com/FieDie/qoder-route>.
It carries changes that were developed against a live local deployment.

## License status — read this before reusing anything

The upstream project **ships no license file** (`LICENSE`, `COPYING`, an SPDX
header, or a `license` field in any manifest). Its README has a section titled
"License & Notice", but that section is a *disclaimer*, not a grant:

> This is an unofficial third-party project. It is not affiliated with,
> endorsed by, or associated with Qoder or its operators. Use at your own risk.
> This software redistributes authenticated user credentials (PAT tokens);
> ensure compliance with applicable terms of service and handle credentials
> securely.

Absent an explicit grant, copyright in the upstream code remains **all rights
reserved** by its author. Consequences for this fork:

- **No license is offered here.** Nothing in this repository — upstream code or
  the fork's own changes — may be copied, redistributed, sublicensed, or used
  commercially on the strength of this README.
- The fork's author **cannot** add a license to the upstream portion; only the
  original copyright holder can. Please do not open a PR that "just adds a
  LICENSE file" on the assumption that fixes it.
- Forking on GitHub is permitted by GitHub's Terms of Service because the
  upstream repository is public. That platform permission covers *viewing and
  forking on GitHub*; it is not a copyright license for use elsewhere.
- If you want to build on this code, ask **FieDie** first.

## Terms of service of Qoder

This software is a proxy that signs requests to Qoder's private endpoints using
Personal Access Tokens, and rotates across several accounts. That behaviour may
conflict with Qoder's terms of service. The upstream README warns about this;
the warning applies in full to this fork, and the fork does not reduce it.

Operate it only against accounts you own and are permitted to automate.

## Credentials

No PAT, token, cookie, or API key is committed to this repository, and none
ever has been in the fork's history. Credentials live only in the local SQLite
database under `data/`, which is gitignored.

Account names appearing in `docs/` are **neutralised placeholders**
(`paid-A`, `free-acct`, …) rather than real identities, so the design notes do
not describe anyone's account inventory.

## What this fork adds

See the two commits on top of `origin/main`:

| Area | Change |
|---|---|
| Error semantics | A transient upstream model-queue (10605) returns `429` with a sanitised message instead of a passthrough `403`, so clients retry rather than aborting as an authentication failure |
| Account control | Per-account `enabled` switch and `reserve_credits`, editable via `PATCH /api/accounts/{id}` and the panel |
| Routing | Selectable strategy: `fill_first` / `spread` / `least_used` |
| Model catalog | Optional auto-sync from Qoder's live model list, merged over the static baseline |
| Dashboard | Traffic chart window selector (1h / 6h / 24h), aggregated from the persisted request table so it survives a restart |

Design rationale for each is in [`docs/`](docs/).
