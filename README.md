# pw-triage-bot

Triage and housekeeping for a patchwork project: works out which section (a
maintainer's `core`, or an upstream) owns each incoming patch and series, writes
the state its owner names, and runs the owner's hook. It also recognises a
series a later posting replaced, handing the patchwork delegate of the old one
to the new one when it has none, and archives what has gone quiet. Everything
is read and written over patchwork's REST API, cached locally in sqlite.

## Known limitations

Patchwork served no `series` for patches from before 2018, and served cover
letters as patches of their own before April 2025. The tool assumes every patch
belongs to a series and every series patch is a real patch, and trips on
either. On a project with history that old, archive everything from before
those dates first — e.g. `archive --force` on a large enough DAYS — before the
first `sync` with triage.

## Quick start

```
export PW_TRIAGE_BOT_TOKEN=...
# create/edit patchwork.project, paths, upstream
pw-triage-bot --config myproject.yaml sync
```

`sync` fills the store on its first run and triages what it finds; run it again
on a timer afterward, e.g. from cron. `-n`/`--dry-run` reports what a run would
do without writing anything; like `--config` and `--store`, it goes before the
command. Runs on the same store, `pw-check` ones included, wait for each
other, through `<store>.lock`.

## Commands

- **`sync`** — reads whatever has newly arrived on patchwork into the local
  store, then triages every series left holding an undecided patch.
- **`triage SERIES_ID`** — triages one series by id, reading it into the store
  first if it isn't held yet. Skips a series already fully decided about.
- **`archive DAYS`** — archives every patch whose whole series has been quiet
  for at least DAYS, and drops it from the store.
- **`check-db`** — exits 0 if this version can use the store, 1 if the store is
  missing or of another schema version (delete it and `sync` again), 2 if it
  cannot be read.

Run `pw-triage-bot COMMAND --help` for each command's options (`--force`,
`--no-triage`), and `pw-triage-bot --help` for the ones the whole run takes
(`--dry-run`, `--no-hook`, ...).

## pw-check

```
echo '{"msgid": "<MSGID>", "series": true, "checks": [{"context": "my-ci", "state": "fail"}]}' | pw-check
```

`pw-check` reports CI results to patchwork as checks, against the mail the CI
ran on. It takes the same `--config`, `--store`, `--token` and `-n` as
`pw-triage-bot`, reads one request, a JSON document, on stdin, and answers one
on stdout. It only reads the store, and needs one `sync` has filled.

The request:

- `msgid` (with or without `<>`) is looked up in the store. Without `series`,
  it is a patch's, and that patch gets the checks. With `"series": true`, it
  is the series' cover letter or any of its patches, and every patch of the
  series gets each check, its context prefixed with `series-`.
- `checks` lists the checks to upload. Each has a `state`, one of `pending`,
  `success`, `warning` or `fail`, and a `context` (letters, digits, `-` and
  `_`) telling it from other testing systems'; `description` and `url` are
  optional. An empty list uploads nothing, and `msgid` is then not looked up.

The whole request is checked before anything is uploaded. The answer lists
the checks `uploaded`, as `{"patch", "context", "state"}`, and on failure
holds the `error` that stopped the rest; the run then exits 1.

`samples/pw-check.socket` serves it on a socket only its user may
reach, one `pw-check` per connection: a client writes its request, shuts its
side down, and reads the answer.

## Configuration

```yaml
patchwork:
  url: https://patchwork.kernel.org
  project: linux-clk

core:
  paths: [drivers/clk/, include/linux/clk*.h]
  state: under-review
  hook: fire-some-checks.sh

foreign:                   # owns no paths: it is what is left
  state: not-applicable

upstream:
  default:                 # backs every upstream that leaves a setting out
    state: awaiting-upstream
    hook: forward-to.sh
  foo:
    paths: [drivers/clk/foo/]

transitions:               # every state the tool may write, and from where
  under-review:      [new]
  awaiting-upstream: [new]
  not-applicable:    [new]
  superseded: [new, under-review, awaiting-upstream, not-applicable]
```

- **`patchwork`**: `url` and `project` (the link name, `linux-clk` in
  `patchwork.kernel.org/project/linux-clk/`) locate the instance. The token
  authenticating writes is kept out of the file: it is read from the `--token`
  file, or `$PW_TRIAGE_BOT_TOKEN` when there is none.
- **`core`, `foreign`, `upstream.<name>`**: each names the `paths` it owns
  (gitignore patterns; `foreign` and `upstream.default` own none), the `state`
  a triage writes on its patches, and an optional `hook` run once its series is
  triaged, passed the series document (plus `handler`/`owners`) via
  `$PW_TRIAGE_BOT_JSON`. A path belongs to whichever upstream's patterns cover
  it, or to `core` otherwise; a series touching more than one section, or
  touching core paths no upstream claims, belongs to `core`. `upstream.default`
  supplies `state`/`hook` for an upstream that leaves them unset.
- **`transitions`**: the only states the tool may write, and per state, the
  states it may be written from. A state left out as a target — `accepted`,
  say — is never written over, whatever a section's `state` asks for. A
  `superseded` target is required.
