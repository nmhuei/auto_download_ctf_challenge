# Instance endpoint metadata

## Goal

Every downloaded challenge has a stable top-level `instance` field in
`metadata.json`. It stores the usable connection endpoint or command chosen
by the user or a lifecycle script.

## Metadata contract

New workspaces always include:

```json
"instance": ""
```

An empty string means no endpoint is known. Once available, it stores a
human-usable string such as `nc host.example 31337` or
`https://instance.example`.

`instance_info` remains the platform-derived lifecycle record and is not
repurposed. In particular, `instance` does not assert that the platform can
start, stop, or renew a service.

## Data flow

`WorkspaceBuilder` writes `instance: ""` for a new challenge. Pull/update
preserves an existing `instance` because it is local user/script state rather
than platform data. Platform start and status responses update the field when
they provide an entry; stopping clears it only when that entry was
platform-managed. Static endpoints may be entered manually in `metadata.json`
or by a local script and remain untouched by lifecycle operations.

No new command executes a stored value. It is data only.

## Compatibility and testing

Existing metadata without `instance` is treated as empty and gains the field
on its next pull/update. Tests cover generation, incremental preservation,
lifecycle synchronization, and platform-managed clearing.
