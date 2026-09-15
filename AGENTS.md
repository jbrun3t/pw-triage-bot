# pw-triage-bot

See `README.md` for what the tool does.

## Environment

Build your own venv if there is already one

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Run all three before calling anything done, and have them clean:

```sh
.venv/bin/pytest
ruff check .
ruff format .
```

## Patchwork

Do not hammer patchwork with requests.  `--dry-run` stills call the GET part of
the REST API, like with `sync`  and `archive` still asks for the events of every
idle patch. It is Ok use it but with care.

Favor tests against `tests/data/`, which holds documents dumped from
patchwork.kernel.org. If one test case is genuinely missing, fetch that single
document, save it there, and `git add -f` it — `*.json` is gitignored.

Do not run the tool against `pw-triage-bot.db`. Copy it and point `--store` at
the copy.

## Tests

One file, `tests/test_bot.py`, fifteen to thirty tests. Delete a test that
duplicates another rather than keeping both.

Assert on what reached patchwork, on the exit status, and on what the hook was
handed. Do not assert on printed output, or on store columns

Name a test for the command and the case
