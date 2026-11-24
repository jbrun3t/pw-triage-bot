# pw-triage-bot

Automatically triage incoming Patchwork series for one project. The bot uses
the files a series touches to choose a section, applies your Patchwork state
rules, and can run a hook to start CI. It can also mark replaced series as
superseded and archive inactive ones.

## Configure and preview

Copy [the sample configuration](samples/linux-clk.yaml) and edit it for your
project. Set `patchwork.url` and `patchwork.project`, then review which paths
each section owns and which state transitions the bot may make. Keep the token
out of the YAML and use a Patchwork API token for an account allowed to make
those changes.

From the checkout, preview a sync with Git and Python 3.12 or later:

```sh
python3 -m venv .venv
.venv/bin/pip install .
cp samples/linux-clk.yaml myproject.yaml
# Edit myproject.yaml before running the bot.
.venv/bin/pw-triage-bot --config myproject.yaml --token /path/to/token --dry-run sync
```

The preview reads from Patchwork without writing changes. **A real `sync`
can change Patchwork and run hooks on its first run.** To fill the local store
without triaging, use `sync --no-triage`. If the project contains very old
Patchwork history, check the [compatibility notes](docs/reference.md#older-patchwork-history)
before the first sync.

## Deploy the scheduler

Use the Linux/amd64 image `ghcr.io/jbrun3t/pw-triage-bot`, tagged with the full
commit SHA from a successful [Publish image](https://github.com/jbrun3t/pw-triage-bot/actions/workflows/image.yml)
run. The image contains neither project configuration nor credentials or data.
For example, after previewing your rules:

```sh
revision=YOUR_IMAGE_COMMIT_SHA
podman secret create pw-triage-token /path/to/token
podman volume create pw-triage-data
podman run -d --name pw-triage \
    -v "$PWD/myproject.yaml:/root/.config/pw-triage-bot/pw-triage-bot.yaml:ro" \
    --secret pw-triage-token,target=/root/.config/pw-triage-bot/pw-triage-bot.token,mode=0400 \
    -v pw-triage-data:/root/.local/state/pw-triage-bot \
    "ghcr.io/jbrun3t/pw-triage-bot:$revision"
```

The image runs `sync` every 15 minutes and `archive 180` weekly. Use
`podman logs pw-triage` to inspect runs. After the first scheduled sync,
confirm that the expected series states appear in Patchwork. Manage the
container with a service manager if it needs to start at boot.

Keep the data volume across updates. If you already have a store, reuse its
volume instead of creating a new one. Use `check-db` to verify compatibility
before replacing a running version; don't discard the store on an error.
For a non-container deployment, schedule the CLI commands with the same
configuration, token, and store.

## Optional: report CI checks

Use `pw-check` to post results against a patch or series by Message-ID.
Point it at the same project and store as the scheduler. See the
[CI integration reference](docs/reference.md#pw-check) for a request example
and the JSON format.

See the [reference](docs/reference.md) for configuration, commands, and local
image builds.
