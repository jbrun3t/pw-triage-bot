# pw-triage-bot reference

Start with the [deployment guide](../README.md).

## Configuration

See the [Linux clock project example](../samples/linux-clk.yaml). The
configuration and token file default to `pw-triage-bot.yaml` and
`pw-triage-bot.token` under `~/.config/pw-triage-bot/`. If there is no token
file, the bot uses `PW_TRIAGE_BOT_TOKEN`. The database defaults to
`~/.local/state/pw-triage-bot/pw-triage-bot.db`. Use `--config`, `--token`,
and `--store` to select other paths.

- `patchwork.url` is the HTTPS base URL. `patchwork.project` is the name in
  the project URL, such as `linux-clk` in `/project/linux-clk/`.
- `core.paths` selects the files the project maintains. Paths are gitignore
  patterns. Files outside the project are `foreign`. `upstream.<name>.paths`
  assigns parts of `core` to an upstream. A series touching core-only paths
  or more than one upstream is handled by `core`. `upstream.default`
  supplies any `state` or `hook` the upstream leaves unset.
- Each section's `state` sets its target Patchwork state. Its optional `hook`
  runs when the series is triaged and receives the path to a JSON series
  document in `PW_TRIAGE_BOT_JSON`. The document includes `handler` and
  `owners`.
- `transitions` lists the only target states the bot may write, with the
  current states each target may replace. For example, a target
  `under-review: [new]` permits replacing `new` but not `accepted`.
  A `superseded` target is required.

## Commands

| Command | Use |
| --- | --- |
| `sync` | Read new patches and triage series with undecided patches. Add `--no-triage` to populate the store only. |
| `triage SERIES_ID` | Triage one series by its numeric Patchwork ID. Use `--force` to revisit an already triaged series. |
| `archive DAYS` | Archive patches whose series has been inactive for at least `DAYS` and remove them from the local store. `--force` skips the activity check against Patchwork. |
| `check-db` | Exit `0` if the store is compatible, `1` if it is missing or has another schema version, and `2` if it cannot be read. |

Put global options such as `--config`, `--store`, `--token`, `--dry-run`
(`-n`), and `--no-hook` before the command. Run `pw-triage-bot --help` or
`pw-triage-bot COMMAND --help` for details. Commands using the same store,
including `pw-check`, coordinate through its lock file.

To build the scheduler image locally from the repository root:

```sh
podman build -f samples/Containerfile -t localhost/pw-triage-bot .
```

## `pw-check`

`pw-check` takes the same configuration, store, token and dry-run options.
It reads one JSON request from standard input and writes one JSON reply.

```sh
echo '{"msgid":"YOUR_MESSAGE_ID","series":true,"checks":[{"context":"my-ci","state":"fail"}]}' |
    pw-check
```

- `msgid` accepts a Message-ID with or without angle brackets. Without
  `series`, it identifies a patch. With `"series": true`, it can be the
  cover letter or any patch in the series. Each patch receives the check
  with `series-` prefixed to its context.
- Each entry in `checks` needs a `context` using letters, digits, `-` or `_`,
  and a `state`: `pending`, `success`, `warning`, or `fail`. `description`
  and `url` are optional. An empty `checks` list makes no uploads and
  doesn't look up the message.

The request is validated before any uploads. The reply contains `uploaded`,
an array of check records with `patch`, `context`, and `state`. If an error
stops the request, the reply includes `error` and the command exits `1`;
check `uploaded` to see what was posted before the error.

## Older Patchwork history

The bot cannot read patches without a series or cover letters stored as
patches. If the project has older records in either form, review and archive
them before enabling triage.
