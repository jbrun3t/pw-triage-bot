"""Fixtures: a patchwork serving documents dumped from the real one."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
import yaml
from pwlite import PwError

from pw_triage_bot.bot import PatchworkBot
from pw_triage_bot.config import Config
from pw_triage_bot.time import now

DATA = Path(__file__).parent / "data"
CONFIG = DATA / "pw-triage-bot.yaml"

# Series dumped from linux-clk, named for what each one is to data/pw-triage-bot.yaml.
CORE_ONLY = 1163855  # drivers/clk/x86/
UPSTREAM_ONLY = 1093397  # drivers/clk/imx/
UPSTREAM_ONLY_V2 = 1097210  # the same series posted again
UPSTREAM_ONLY_V3 = 1105997  # and once more, so one posting is replaced twice over
CORE_AND_UPSTREAM = 1157898  # drivers/clk/tegra/ and include/linux/clk/
FOREIGN_ONLY = 1159548  # a mailbox binding, which the configuration claims nowhere
FOREIGN_MIXED = 1162185  # two patches, one an upstream's and one nobody's


def document(name: str) -> dict:
    """Return a fresh copy of the dumped patchwork document `name`."""
    return json.loads((DATA / f"{name}.json").read_text())


def patch_ids(client, series_id: int) -> list[int]:
    """The patches the served series holds, in the order it names them."""
    return [entry["id"] for entry in client.series_docs[series_id]["patches"]]


def days_ago(days: int) -> str:
    return (now() - datetime.timedelta(days=days)).isoformat()


class FakeClient:
    """Serves the dumped documents in place of a patchwork instance."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.patch_docs: dict[int, dict] = {}
        self.series_docs: dict[int, dict] = {}
        self.event_docs: list[dict] = []
        self.written: list[tuple[int, str | None, bool | None]] = []  # as the real client PATCHes
        self.asked: list[int] = []  # every patch document fetched, so a test can price a run
        self.checks: list[tuple[int, dict]] = []  # as the real client POSTs them

    def patch(self, *, id: int) -> dict:
        self.asked.append(id)
        if id not in self.patch_docs:
            raise PwError(f"patches/{id}: no such patch")
        return self.patch_docs[id]

    def series(self, *, id: int) -> dict:
        if id not in self.series_docs:
            raise PwError(f"series/{id}: no such series")
        return self.series_docs[id]

    def patches(self, **params: object):
        entries = list(self.patch_docs.values())
        if params.get("archived") is False:
            entries = [e for e in entries if not e.get("archived")]
        if msgid := params.get("msgid"):
            entries = [e for e in entries if e["msgid"].strip("<>") == msgid]
        order = str(params.get("order") or "id")
        return iter(sorted(entries, key=lambda e: e["id"], reverse=order.startswith("-")))

    def covers(self, **params: object):
        """The cover letters of the served series, as patchwork lists them."""
        entries = [
            series["cover_letter"] | {"series": [{"id": series["id"]}]}
            for series in self.series_docs.values()
            if series.get("cover_letter")
        ]
        if msgid := params.get("msgid"):
            entries = [e for e in entries if e["msgid"].strip("<>") == msgid]
        return iter(entries)

    def events(self, **params: object):
        patch_id = params.get("patch")
        found = [e for e in self.event_docs if e["payload"]["patch"]["id"] == patch_id]
        return iter(sorted(found, key=lambda e: e["date"], reverse=True))

    def update_patch(self, *, id: int, **fields: object) -> None:
        if self.dry_run:
            return
        self.written.append((id, fields.get("state"), fields.get("archived")))
        self.patch_docs[id] |= fields

    def create_check(self, *, id: int, **fields: object) -> None:
        if self.dry_run:
            return
        self.checks.append((id, fields))


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def serve(client):
    """Serve a dumped series, overriding the state, archived flag and patch ages it holds."""

    def make(
        series_id: int,
        *,
        state: str = "new",
        archived: bool = False,
        ages: list[int] | None = None,
    ) -> int:
        series = document(f"series-{series_id}")
        for number, entry in enumerate(series["patches"]):
            patch = document(f"patch-{entry['id']}")
            patch["state"] = state
            patch["archived"] = archived
            if ages is not None:
                patch["date"] = entry["date"] = days_ago(ages[number])
            client.patch_docs[patch["id"]] = patch
        client.series_docs[series_id] = series
        return series_id

    return make


@pytest.fixture
def config():
    return Config.load(CONFIG)


@pytest.fixture
def load_config(tmp_path: Path):
    """Load configuration text over the keys every configuration must carry."""

    def make(text: str):
        base = yaml.safe_load(CONFIG.read_text())
        raw = {key: base[key] for key in ("patchwork", "transitions")}
        raw |= yaml.safe_load(text) or {}
        path = tmp_path / "pw-triage-bot.yaml"
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        return Config.load(path)

    return make


@pytest.fixture
def bot(tmp_path, config, client):
    """A handler over the fake patchwork and a store of its own."""
    handler = PatchworkBot(config, tmp_path / "store.db")
    handler.client = client
    with handler:
        yield handler
