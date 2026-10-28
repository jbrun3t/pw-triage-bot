"""pw-check: report a CI result to patchwork against the mail it was run on."""

from __future__ import annotations

from pathlib import Path

import click

from .cli import failed, opened, run_options

STATES = ["pending", "success", "warning", "fail"]


@click.command()
@click.version_option()
@run_options
@click.option(
    "--series",
    "whole",
    is_flag=True,
    help="MSGID names a series, or one of its patches: check every patch of the series.",
)
@click.option("--state", required=True, type=click.Choice(STATES), help="Outcome of the check.")
@click.option(
    "--context",
    required=True,
    help="Name telling this check from other testing systems' (letters, digits, - and _).",
)
@click.option("--description", help="A brief description of the outcome.")
@click.option("--url", help="Where the details of this run are.")
@click.argument("msgid")
def main(
    config_path: Path,
    store_path: Path,
    token_path: Path,
    dry_run: bool,
    whole: bool,
    state: str,
    context: str,
    description: str | None,
    url: str | None,
    msgid: str,
) -> None:
    """Upload a check against the patch, or series, the mail MSGID is held for."""
    msgid = msgid.strip("<>")
    with opened(config_path, store_path, token_path, dry_run) as bot:
        if whole:
            if (series := bot.series_find(msgid)) is None:
                raise failed(f"{msgid}: no series known by this message-id")
            patches = series.patches
            context = f"series-{context}"
        else:
            if (patch := bot.patch_find(msgid)) is None:
                raise failed(f"{msgid}: no patch known by this message-id")
            patches = [patch]

        for patch in patches:
            patch.add_check(state=state, context=context, description=description, target_url=url)
            click.echo(f"check: patch #{patch.id}  {context}: {state}  {patch.name}")
