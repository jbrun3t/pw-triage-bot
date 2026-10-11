"""Command line entry point: the commands and their output."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import tempfile
from pathlib import Path

import click
import platformdirs
from pwlite import PwError

from . import NAME, Error
from .bot import DiffError, PatchworkBot, Series
from .config import FOREIGN, SUPERSEDED, Config, Handling
from .time import now

CONFIG = Path(platformdirs.user_config_dir(NAME), f"{NAME}.yaml")
STORE = Path(platformdirs.user_state_dir(NAME), f"{NAME}.db")
TOKEN = Path(platformdirs.user_config_dir(NAME), f"{NAME}.token")
TOKEN_ENV = "PW_TRIAGE_BOT_TOKEN"


def token(path: Path) -> str | None:
    """The token authenticating writes: the file at `path` if it is there, else the environment."""
    if path.is_file():
        return path.read_text().strip() or None
    return os.environ.get(TOKEN_ENV) or None


def failed(reason: object) -> SystemExit:
    """Report why the run failed, on stderr, and return the SystemExit to raise."""
    click.echo(reason, err=True)
    return SystemExit(1)


class Main(click.Group):
    """The command group: builds the handler and ends the run on `Error`."""

    def invoke(self, ctx: click.Context) -> object:
        try:
            with PatchworkBot(
                Config.load(ctx.params["config_path"]),
                ctx.params["store_path"],
                token=token(ctx.params["token_path"]),
                dry_run=ctx.params["dry_run"],
                no_hook=ctx.params["no_hook"],
            ) as bot:
                ctx.obj = bot
                return super().invoke(ctx)
        except (Error, PwError) as e:
            raise failed(e) from e


def fire(bot: PatchworkBot, section: Handling, series: Series) -> bool:
    """Run `section`'s hook with PW_TRIAGE_BOT_JSON naming the series document; True if it was happy."""
    if not section.hook:
        return True
    if bot.dry_run or bot.no_hook:
        return True

    with tempfile.TemporaryDirectory(prefix="pw-triage-bot-") as tmp:
        path = Path(tmp) / "series.json"
        path.write_text(json.dumps(series.document(section.name), indent=2) + "\n")
        env = os.environ | {"PW_TRIAGE_BOT_JSON": str(path)}
        status = subprocess.run(section.hook, shell=True, env=env, check=False).returncode

    if status:
        click.echo(f"series #{series.id}: {section.name}.hook exited {status}", err=True)
    return not status


def triage_series(bot: PatchworkBot, series: Series, *, force: bool = False) -> bool:
    """Triage one series and supersede what it replaced; it leaves the queue even when it fails."""
    try:
        if force:
            series.patches_set_triaged(False)

        if not series.patches:
            click.echo(f"series #{series.id}: no patches in series", err=True)
            return False

        if not series.patches_untriaged:
            click.echo(f"series #{series.id}: already triaged")
            return True

        handler = series.handler or FOREIGN
        for patch in series.patches:
            owner = handler if patch.owners else FOREIGN
            owners = " ".join(sorted(patch.owners))
            click.echo(f"triage: series #{series.id} - patch #{patch.id}: {owner} - [{owners}]")

            origin = patch.state
            target = bot.config.sections[owner].state
            if patch.set_state(target):
                click.echo(f"triage: series #{series.id} - patch #{patch.id}: {origin} -> {target}")

        click.echo(f"triage: series #{series.id}  handler: {handler}")
        fired = fire(bot, bot.config.sections[handler], series)

        series.patches_set_triaged()

        for posting in series.replaced():
            if posting.superseded_by_id is None:
                supersede(posting, series)

    except DiffError as e:
        click.echo(f"series #{series.id}: cannot be read: {e}", err=True)
        series.patches_set_triaged()
        return False

    return fired


def supersede(series: Series, by: Series) -> None:
    """Record that `by` replaced `series`, moving each patch's state as the transitions table allows."""
    for patch in series.patches:
        before = patch.state
        if patch.set_state(SUPERSEDED):
            click.echo(f"triage: superseded: patch #{patch.id}  {before} -> {SUPERSEDED}")
    series.replaced_by(by)
    click.echo(f"triage: superseded: series #{series.id} => {by.id}")


@click.group(cls=Main)
@click.version_option()
@click.option(
    "--config",
    "config_path",
    default=CONFIG,
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Configuration file.",
)
@click.option(
    "--store",
    "store_path",
    default=STORE,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Store to read and write.",
)
@click.option(
    "--token",
    "token_path",
    default=TOKEN,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"File holding the patchwork API token; ${TOKEN_ENV} if there is none.",
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    help="Report what would be written, to patchwork and to the store, and write nothing.",
)
@click.option("--no-hook", is_flag=True, help="Hold every hook back, and run as usual otherwise.")
def main(
    config_path: Path, store_path: Path, token_path: Path, dry_run: bool, no_hook: bool
) -> None:
    """Triage and housekeeping for a patchwork project."""


@main.command()
@click.option(
    "--force",
    is_flag=True,
    help="Triage the series named even if the store has already decided about it.",
)
@click.argument("series_id", type=int)
@click.pass_obj
def triage(bot: PatchworkBot, force: bool, series_id: int) -> None:
    """Triage one patchwork series against maintained and delegated paths."""
    series = bot.series_get(series_id)
    if not triage_series(bot, series, force=force):
        raise failed(f"series #{series.id} failed")


@main.command()
@click.option("--no-triage", is_flag=True, help="Fill the store and triage nothing it turns up.")
@click.pass_obj
def sync(bot: PatchworkBot, no_triage: bool) -> None:
    """Read what has arrived into the store, and triage what nobody has looked at."""
    bot.sync()
    if no_triage:
        return

    broken = sum(not triage_series(bot, series) for series in list(bot.series_untriaged()))
    if broken:
        raise failed(f"{broken} series failed")


@main.command()
@click.option(
    "--force",
    is_flag=True,
    help="Archive on the recorded timers alone, without asking patchwork for fresh activity.",
)
@click.argument("days", type=click.IntRange(min=1))
@click.pass_obj
def archive(bot: PatchworkBot, force: bool, days: int) -> None:
    """Archive patches quiet for DAYS; this is also how the tool forgets one."""
    cutoff = now() - datetime.timedelta(days=days)

    if not force:
        for patch in bot.patch_idle(cutoff):
            if when := patch.last_event:
                patch.touch(when)

    for patch in bot.patch_idle(cutoff):
        click.echo(f"archive: patch #{patch.id}  {patch.name}")
        patch.archive()

    for series in list(bot.series_empty()):
        series.delete_instance()
