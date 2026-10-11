"""Peewee rows for series and patches, backed by patchwork documents fetched on demand."""

from __future__ import annotations

import contextlib
import datetime
import functools
import re
from itertools import takewhile
from pathlib import Path
from typing import ClassVar, Self

import peewee
import unidiff
from pwlite import Client

from . import NAME, VERSION, Error
from .config import CORE, NEW, Config
from .time import now, stamp

DEV_NULL = "/dev/null"

# A bracketed subject prefix, e.g. [PATCH v2 3/7], [RFC], [v5].
PREFIX = re.compile(r"^\s*\[[^]]*]\s*")


class StoreError(Error):
    """The store could not be opened, read, or written."""


class DiffError(Error):
    """A diff could not be parsed."""


class PwBotBaseModel(peewee.Model):
    # The run these rows belong to; they reach the client and config through it.
    bot: ClassVar[PatchworkBot]


class Series(PwBotBaseModel):
    """One posting of a series, keyed by patchwork's id.

    `subject` is the chain key (case-insensitive); `prefix` is the bracketed
    run patchwork stacks in front of it. `superseded_by` names the posting
    that replaced this one; whether a series was looked at lives on its patches.
    """

    id = peewee.IntegerField(primary_key=True)
    version = peewee.IntegerField()
    date = peewee.DateTimeField()
    prefix = peewee.TextField(null=True)
    subject = peewee.TextField(null=True, index=True, collation="NOCASE")
    superseded_by = peewee.ForeignKeyField(
        "self", null=True, backref="supersedes", on_delete="SET NULL"
    )

    @staticmethod
    def split(name: str | None) -> tuple[str, str]:
        """Split a mail subject into its stacked prefixes and the chain-grouping subject."""
        rest = name or ""
        while (shorter := PREFIX.sub("", rest)) != rest:
            rest = shorter
        taken = len(name or "") - len(rest)
        lead = len(rest) - len(rest.lstrip())
        return (name or "")[: taken + lead], rest.strip()

    @property
    def name(self) -> str | None:
        """The mail subject as patchwork served it, less any trailing blanks."""
        if self.prefix is None and self.subject is None:
            return None
        return (self.prefix or "") + (self.subject or "")

    @functools.cached_property
    def data(self) -> dict:
        """Patchwork's document for this series, fetched on first use."""
        return self.bot.client.series(id=self.id)

    # Cached so callers get the same Patch objects; the peewee backref does not.
    @functools.cached_property
    def patches(self) -> list[Patch]:
        """The patches held for this series, in the order they were numbered."""
        return list(self._patchset.order_by(Patch.id))

    @property
    def patches_untriaged(self) -> list[Patch]:
        """The patches nobody has decided about; empty means the series needs no triage."""
        return [p for p in self.patches if not p.triaged]

    def patches_set_triaged(self, triaged: bool = True) -> None:
        """Set whether every patch of this series has been decided about."""
        for patch in self.patches:
            patch.triaged = triaged
            patch.save()

    def replaced_by(self, other: Self) -> None:
        """Record that `other` replaced this posting, and mark its patches triaged."""
        with self.bot.atomic():
            self.superseded_by = other.id
            self.patches_set_triaged()
            self.save()

    @functools.cached_property
    def owners(self) -> set[str]:
        """Every section the patches of this series belong to."""
        return {owner for patch in self.patches for owner in patch.owners}

    @property
    def delegate(self) -> dict | None:
        """The patchwork user the first delegated patch is delegated to, or None."""
        return next((d for patch in self.patches if (d := patch.delegate)), None)

    def document(self, handler: str) -> dict:
        """Patchwork's series document plus `handler` and `owners`, for hooks."""
        return self.data | {"handler": handler, "owners": sorted(self.owners)}

    @functools.cached_property
    def handler(self) -> str | None:
        """The section owning the whole series, or None; core owns ties and its own share."""
        if len(self.owners) > 1 or CORE in self.owners:
            return CORE
        return next(iter(self.owners), None)

    def replaced(self) -> list[Series]:
        """Return the postings this one replaced, oldest first, matched by `subject` and ordered by post date rather than version."""
        if not self.subject:
            return []

        return list(
            Series.select()
            .where((Series.subject == self.subject) & (Series.date < self.date))
            .order_by(Series.date, Series.id)
        )


class Patch(PwBotBaseModel):
    """One patch, under the id patchwork gave it.

    `timestamp` is the idle timer, moved only by `archive`. `triaged` records
    that the patch was decided about, by the tool or a person.
    """

    id = peewee.IntegerField(primary_key=True)
    name = peewee.TextField(null=True)
    series = peewee.ForeignKeyField(Series, null=True, backref="_patchset", on_delete="CASCADE")
    timestamp = peewee.DateTimeField()
    triaged = peewee.BooleanField(default=False)
    msgid = peewee.CharField()

    @staticmethod
    def _strip_prefix(name: str) -> str:
        """Drop the ``a/``/``b/`` prefix git puts on diff filenames, on either side."""
        return name[2:] if name.startswith(("a/", "b/")) else name

    @functools.cached_property
    def data(self) -> dict:
        """Patchwork's detail document (mail and diff), fetched on first use."""
        return self.bot.client.patch(id=self.id)

    @property
    def state(self) -> str:
        """The state patchwork holds now."""
        return self.data.get("state") or ""

    @property
    def delegate(self) -> dict | None:
        """The patchwork user the patch is delegated to, or None."""
        return self.data.get("delegate")

    @property
    def last_event(self) -> datetime.datetime | None:
        """When anything last happened to this patch, or None; per-patch since comment events name no series."""
        events = self.bot.client.events(patch=self.id, order="-date", per_page=1)
        return stamp(next(events, {}).get("date"))

    def paths(self) -> list[str]:
        """The paths the diff touches; a rename touches both source and target."""
        try:
            parsed = unidiff.PatchSet.from_string(self.data.get("diff") or "")
        except unidiff.UnidiffParseError as e:
            raise DiffError(f"patch {self.id}: cannot parse diff: {str(e).strip()}") from e

        sides = (
            self._strip_prefix(side)
            for entry in parsed
            for side in (entry.source_file, entry.target_file)
            if side and side != DEV_NULL
        )
        return list(dict.fromkeys(sides))

    @functools.cached_property
    def owners(self) -> set[str]:
        """The sections this patch's paths belong to. Empty concerns nobody."""
        return {owner for path in self.paths() if (owner := self.bot.config.section_of(path))}

    def set_state(self, state: str) -> bool:
        """Write `state` if the transitions table allows it; return whether it did."""
        if not self.bot.config.transitions.allows(self.state, state):
            return False
        self.bot.client.update_patch(id=self.id, state=state)
        return True

    def set_delegate(self, user: dict) -> None:
        """Delegate the patch to `user`, a patchwork user document."""
        self.bot.client.update_patch(id=self.id, delegate=user["id"])

    def archive(self) -> None:
        """Archive the patch in patchwork, then drop it from the store."""
        self.bot.client.update_patch(id=self.id, archived=True)
        self.delete_instance()

    def touch(self, when: datetime.datetime) -> None:
        """Move the idle timer forward to `when`, if later than what is recorded."""
        if when > self.timestamp:
            self.timestamp = when
            self.save()


class Tracker(PwBotBaseModel):
    """The newest patch read, where the next read starts. One row, recorded rather than derived."""

    patch_id = peewee.IntegerField()
    date = peewee.DateTimeField()

    class Meta:
        primary_key = False


MODELS = [Patch, Series, Tracker]


class PatchworkBot:
    """One run against one patchwork project."""

    DB_SCHEMA_VERSION = 2

    def __init__(
        self,
        config: Config,
        store: Path,
        *,
        token: str | None = None,
        dry_run: bool = False,
        no_hook: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.dry_run = dry_run
        self.no_hook = no_hook
        self.client = Client(
            config.patchwork.url,
            config.patchwork.project,
            token=token,
            user_agent=f"{NAME}/{VERSION}",
            dry_run=dry_run,
        )

    def __enter__(self) -> Self:
        with contextlib.ExitStack() as opening:
            self._database = peewee.SqliteDatabase(self.store, pragmas={"foreign_keys": 1})
            opening.enter_context(self._database.bind_ctx(MODELS))
            opening.callback(self._database.close)
            self._database_prepare()
            PwBotBaseModel.bot = self
            opening.callback(delattr, PwBotBaseModel, "bot")
            if self.dry_run:
                opening.enter_context(self._database_dryrun())
            self._open = opening.pop_all()
        return self

    def __exit__(self, *exc: object) -> None:
        self._open.__exit__(*exc)

    def _database_prepare(self) -> None:
        """Create and stamp a new store; refuse one written by another schema version."""
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            version = self._database.pragma("user_version")
            if version != self.DB_SCHEMA_VERSION and (version or self._database.get_tables()):
                raise StoreError(
                    f"{self.store}: holds version {version}, not {self.DB_SCHEMA_VERSION}; "
                    f"delete the store and read the project again"
                )
            self._database.create_tables(MODELS)
            self._database.pragma("user_version", self.DB_SCHEMA_VERSION)
        except (OSError, peewee.PeeweeException) as e:
            raise StoreError(f"{self.store}: {e}") from e

    @contextlib.contextmanager
    def _database_dryrun(self):
        """Roll the run's writes to the store back at the end of it."""
        with self._database.atomic() as transaction:
            yield
            transaction.rollback()

    def atomic(self):
        """Open a transaction, so a run that dies leaves no half-read project."""
        return self._database.atomic()

    def patch_get(self, patch_id: int) -> Patch:
        """Return the patch, reading it into the store if it is not held yet."""
        if (held := Patch.get_or_none(Patch.id == patch_id)) is not None:
            return held

        document = self.client.patch(id=patch_id)
        fresh = self.patch_record(document)
        fresh.data = document
        return fresh

    def patch_record(self, entry: dict, series_id: int | None = None) -> Patch:
        """Store one patch (and its series) from a listing or series-document entry, and return it; leaves an already-held patch alone."""
        with self.atomic():
            if series := entry.get("series"):
                series_id = self.series_record(series[0]).id
            patch, _ = Patch.get_or_create(
                id=entry["id"],
                defaults={
                    "series": series_id,
                    "name": entry.get("name"),
                    "timestamp": stamp(entry.get("date")) or now(),
                    "triaged": entry.get("state", NEW) != NEW,
                    "msgid": (entry.get("msgid") or "").strip("<>"),
                },
            )
        return patch

    @staticmethod
    def patch_idle(cutoff: datetime.datetime) -> peewee.ModelSelect:
        """Return the patches of every series quiet since `cutoff` (every patch of the series, not just one)."""
        alive = Patch.alias()
        return (
            Patch.select()
            .where(
                ~peewee.fn.EXISTS(
                    alive.select(alive.id).where(
                        (alive.series == Patch.series) & (alive.timestamp >= cutoff)
                    )
                )
            )
            .order_by(Patch.id)
        )

    def series_get(self, series_id: int) -> Series:
        """Return the series and its patches, reading them in if not held yet; raises for an id patchwork won't serve."""
        if (held := Series.get_or_none(Series.id == series_id)) is not None:
            return held

        document = self.client.series(id=series_id)
        fresh = self.series_record(document)
        fresh.data = document

        for entry in document.get("patches") or []:
            self.patch_record(entry, series_id=fresh.id)

        return fresh

    @staticmethod
    def series_record(data: dict) -> Series:
        """Store one series, if not held already, and return it."""
        prefix, subject = Series.split(data.get("name"))
        series, _ = Series.get_or_create(
            id=data["id"],
            defaults={
                "prefix": prefix,
                "subject": subject,
                "version": 1 if data.get("version") is None else data["version"],
                "date": stamp(data.get("date")) or now(),
            },
        )
        return series

    @staticmethod
    def series_empty() -> peewee.ModelSelect:
        """Return the postings the store holds no patch of."""
        return (
            Series.select()
            .where(~peewee.fn.EXISTS(Patch.select(Patch.id).where(Patch.series == Series.id)))
            .order_by(Series.id)
        )

    @staticmethod
    def series_untriaged() -> peewee.ModelSelect:
        """Return the series still holding a patch nobody has decided about."""
        return (
            Series.select().join(Patch).where(~Patch.triaged).group_by(Series).order_by(Series.id)
        )

    def sync(self) -> int:
        """Read the patches arrived since the last run (by id, newest first) and return how many."""
        params = {"order": "-id", "archived": False}
        if mark := Tracker.select().first():
            id = mark.patch_id
        else:
            id = 0
            params["per_page"] = 250  # Nothing read yet: page for the whole project.

        patches = self.client.patches(**params)
        entries = list(takewhile(lambda entry: entry["id"] > id, patches))

        with self.atomic():
            for entry in entries:
                self.patch_record(entry)
            if entries:
                self._mark(entries[0])
        return len(entries)

    def _mark(self, entry: dict) -> None:
        """Record `entry` as the newest patch read; there is only ever one such row."""
        with self.atomic():
            Tracker.delete().execute()
            Tracker.create(patch_id=entry["id"], date=stamp(entry["date"]) or now())
