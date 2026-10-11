"""The configuration file: which paths belong to whom, and what to do about them."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pathspec import GitIgnoreSpec
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from . import Error

CORE = "core"
FOREIGN = "foreign"
DEFAULT = "default"

# The state slugs patchwork.kernel.org serves
State = Literal[
    "new",
    "under-review",
    "changes-requested",
    "awaiting-upstream",
    "handled-elsewhere",
    "not-applicable",
    "superseded",
    "accepted",
    "rejected",
    "deferred",
    "rfc",
]

NEW = "new"
SUPERSEDED = "superseded"

Text = Annotated[str, StringConstraints(min_length=1)]

# The states a target may be written from, and the table of every target.
Origins = Annotated[list[State], Field(min_length=1)]
Table = Annotated[dict[State, Origins], Field(min_length=1)]


class ConfigError(Error):
    """The configuration file is missing or malformed."""


class Strict(BaseModel):
    """Rejects a key the tool does not know, so a typo is never silently ignored."""

    model_config = ConfigDict(extra="forbid")


class Patchwork(Strict):
    """Where to reach the patchwork instance, and what to ask it for."""

    url: Text
    project: Text

    @field_validator("url")
    @classmethod
    def _reachable(cls, url: str) -> str:
        if not url.startswith("https://"):
            raise ValueError("must be https, the token being sent with it")
        return url.rstrip("/")


class Handling(Strict):
    """What a section does about the patches it owns"""

    state: State | None = None
    hook: Text | None = None

    _name: str = PrivateAttr(default="")

    @property
    def name(self) -> str:
        """The key this was read from"""
        return self._name


class Section(Handling):
    """A group of paths, with the patchwork state and hook that apply to them."""

    paths: list[Text] = Field(default_factory=list, min_length=1)

    @cached_property
    def spec(self) -> GitIgnoreSpec:
        return GitIgnoreSpec.from_lines(self.paths)

    def matches(self, path: str) -> bool:
        return self.spec.match_file(path)


class Transitions(RootModel[Table]):
    """Every state move the tool may make, as target and the states it follows"""

    def allows(self, origin: str | None, target: str) -> bool:
        """Return whether a patch in `origin` may be written `target`."""
        return origin in self.root.get(target, ())

    @property
    def table(self) -> dict[str, list[str]]:
        return self.root


class Config(Strict):
    """Who owns which paths, and what to write and run about them."""

    core: Section = Section()
    foreign: Handling = Handling()
    upstream: dict[str, Section] = {}
    patchwork: Patchwork
    transitions: Transitions

    @classmethod
    def load(cls, path: Path) -> Config:
        """Read and check the file at `path`, naming in the error where it went wrong."""
        try:
            raw = yaml.safe_load(path.read_bytes()) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"{path}: {e}") from e

        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            raise ConfigError(f"{path}: {_faults(e)}") from e

    @model_validator(mode="before")
    @classmethod
    def _inherit(cls, raw: object) -> object:
        """Fold ``upstream.default`` into each upstream, the upstream's own keys winning."""
        if not isinstance(raw, dict) or not isinstance(raw.get("upstream"), dict):
            return raw

        upstream = dict(raw["upstream"])
        try:
            default = Handling.model_validate(upstream.pop(DEFAULT, None) or {})
        except ValidationError as e:
            raise ValueError(f"upstream.{DEFAULT}: {_faults(e)}") from e
        if reserved := sorted({CORE, FOREIGN} & set(upstream)):
            raise ValueError(f"{reserved} is reserved and cannot name an upstream")

        shared = default.model_dump(exclude_none=True)
        return raw | {"upstream": {name: shared | (d or {}) for name, d in upstream.items()}}

    @model_validator(mode="after")
    def _named(self) -> Config:
        """Give every section the key it was read from, for the messages that name it."""
        self.core._name = CORE
        self.foreign._name = FOREIGN
        for name, section in self.upstream.items():
            section._name = name
        return self

    def section_of(self, path: str) -> str | None:
        """Return the section a path belongs to: an upstream, core, or nobody."""
        if not self.core.matches(path):
            return None

        for section in self.upstream.values():
            if section.matches(path):
                return section.name
        return CORE

    @cached_property
    def sections(self) -> dict[str, Handling]:
        """Return every section by name: `core`, `foreign`, then each upstream."""
        return {s.name: s for s in (self.core, self.foreign, *self.upstream.values())}


# Prefixes what a validator raised as `ValueError`; the message alone reads better.
NOISE = "Value error, "


def _faults(e: ValidationError) -> str:
    """Render what failed as ``where: why``, the where being the keys walked to reach it."""
    return "; ".join(
        f"{'.'.join(str(key) for key in fault['loc'])}: {fault['msg'].removeprefix(NOISE)}".lstrip(
            ": "
        )
        for fault in e.errors()
    )
