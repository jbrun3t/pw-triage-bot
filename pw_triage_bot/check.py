"""pw-check: report CI results to patchwork against the mail they were run on."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import click
from pwlite import PwError
from pydantic import StringConstraints, ValidationError, field_validator

from . import Error
from .bot import PatchworkBot
from .cli import run_options, token
from .config import Config, Strict

# Patchwork keeps a check's context as a slug, and refuses anything else.
Context = Annotated[str, StringConstraints(pattern=r"^[-a-zA-Z0-9_]+$", max_length=255)]


class Check(Strict):
    """One check to upload."""

    context: Context
    state: Literal["pending", "success", "warning", "fail"]
    description: str | None = None
    url: str | None = None


class Request(Strict):
    """The checks to upload against the mail `msgid`, or with `series`, against every patch of its series."""

    msgid: str
    series: bool = False
    checks: list[Check]

    @field_validator("msgid")
    @classmethod
    def _bare(cls, msgid: str) -> str:
        return msgid.strip("<>")


def upload(bot: PatchworkBot, request: Request, uploaded: list[dict]) -> None:
    """Upload every check `request` asks for, recording each in `uploaded` once patchwork took it."""
    if request.series:
        if (series := bot.series_find(request.msgid)) is None:
            raise Error(f"{request.msgid}: no series known by this message-id")
        patches, prefix = series.patches, "series-"
    else:
        if (patch := bot.patch_find(request.msgid)) is None:
            raise Error(f"{request.msgid}: no patch known by this message-id")
        patches, prefix = [patch], ""

    for patch in patches:
        for check in request.checks:
            context = prefix + check.context
            patch.add_check(
                state=check.state,
                context=context,
                description=check.description,
                target_url=check.url,
            )
            uploaded.append({"patch": patch.id, "context": context, "state": check.state})


@click.command()
@click.version_option()
@run_options
def main(config_path: Path, store_path: Path, token_path: Path, dry_run: bool) -> None:
    """Upload the checks the JSON request on stdin asks for, and answer in JSON on stdout.

    The whole request is checked before anything is uploaded. The answer lists
    the checks uploaded, and on failure, the `error` that stopped the rest.
    """
    reply: dict = {"uploaded": []}
    try:
        request = Request.model_validate_json(sys.stdin.read())
        if request.checks:
            with PatchworkBot(
                Config.load(config_path),
                store_path,
                token=token(token_path),
                dry_run=dry_run,
                shared=True,
            ) as bot:
                upload(bot, request, reply["uploaded"])
    except (Error, PwError, ValidationError) as e:
        reply["error"] = str(e)

    click.echo(json.dumps(reply))
    if "error" in reply:
        raise SystemExit(1)
