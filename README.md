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
command.

## Commands

- **`sync`** — reads whatever has newly arrived on patchwork into the local
  store, then triages every series left holding an undecided patch.
- **`triage SERIES_ID`** — triages one series by id, reading it into the store
  first if it isn't held yet. Skips a series already fully decided about.
- **`archive DAYS`** — archives every patch whose whole series has been quiet
  for at least DAYS, and drops it from the store.

Run `pw-triage-bot COMMAND --help` for each command's options (`--force`,
`--no-triage`), and `pw-triage-bot --help` for the ones the whole run takes
(`--dry-run`, `--no-hook`, ...).

## pw-check

```
pw-check --series --state fail --context my-ci --description "rebuild" MSGID
```

`pw-check` reports a CI result to patchwork as a check, against the mail the CI
ran on. It takes the same `--config`, `--store`, `--token` and `-n` as
`pw-triage-bot`, and looks MSGID (with or without `<>`) up. An MSGID the store
does not know fails the run.

- Without `--series`, MSGID is a patch's, and that patch gets the check.
- With `--series`, MSGID is the series' cover letter or any of its patches, and
  every patch of the series gets the check, its description prefixed with
  `series`.
- `--state` is one of `pending`, `success`, `warning` or `fail`; `--context`
  (letters, digits, `-` and `_`) tells the check from other testing systems';
  `--description` and `--url` are optional.

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
</content>
