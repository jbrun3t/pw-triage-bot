"""Every command end to end: what it writes to patchwork, what it runs, and how it ends."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from conftest import (
    CONFIG,
    CORE_AND_UPSTREAM,
    CORE_ONLY,
    FOREIGN_MIXED,
    FOREIGN_ONLY,
    UPSTREAM_ONLY,
    UPSTREAM_ONLY_V2,
    UPSTREAM_ONLY_V3,
    days_ago,
    document,
    patch_ids,
)
from pwlite import PwError

from pw_triage_bot import bot as bot_module
from pw_triage_bot import check, cli
from pw_triage_bot.bot import DiffError, Series
from pw_triage_bot.config import ConfigError


@pytest.fixture
def run(tmp_path, client, monkeypatch):
    """Invoke a command in tmp_path, against the fake patchwork and a store of its own."""

    def build(url, project, *, dry_run=False, **settings):
        client.dry_run = dry_run
        return client

    monkeypatch.setattr(bot_module, "Client", build)
    monkeypatch.chdir(tmp_path)

    def make(*args, dry_run=False, no_hook=False, entry=cli.main):
        line = ["--config", str(CONFIG), "--store", str(tmp_path / "store.db")]
        if dry_run:
            line.append("-n")
        if no_hook:
            line.append("--no-hook")
        return CliRunner().invoke(entry, [*line, *[str(a) for a in args]])

    return make


def fired(tmp_path) -> list[dict]:
    """The series documents the hooks were handed, one per firing."""
    log = tmp_path / "hook.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# A diff patchwork served with its hunk a line short: unidiff takes the "--" of
# the signature for a removed line, then quotes "2.43.0" back with its newline.
MALFORMED = document("patch-14801502")["diff"]


def states(client):
    return {pid: doc["state"] for pid, doc in client.patch_docs.items()}


# -- triage ----------------------------------------------------------------


def test_triage_owner_state(run, client, serve):
    """Core's tree, an upstream's subtree of it, and a series touching neither."""
    serve(CORE_ONLY)
    serve(UPSTREAM_ONLY)
    serve(FOREIGN_ONLY)
    (core,) = patch_ids(client, CORE_ONLY)
    (upstream,) = patch_ids(client, UPSTREAM_ONLY)
    (foreign,) = patch_ids(client, FOREIGN_ONLY)

    result = run("sync")

    assert result.exit_code == 0
    assert states(client) == {
        core: "under-review",
        upstream: "awaiting-upstream",
        foreign: "not-applicable",
    }


def test_triage_core_takes_back(run, client, serve):
    """A patch crossing an upstream's subtree and core's own tree is core's."""
    serve(CORE_AND_UPSTREAM)
    (patch,) = patch_ids(client, CORE_AND_UPSTREAM)

    run("triage", CORE_AND_UPSTREAM)

    assert states(client) == {patch: "under-review"}


def test_triage_foreign_mixed(run, client, serve):
    """A patch the handler owns nothing of is still nobody's."""
    serve(FOREIGN_MIXED)
    owned, stranger = patch_ids(client, FOREIGN_MIXED)

    run("triage", FOREIGN_MIXED)

    assert states(client) == {owned: "awaiting-upstream", stranger: "not-applicable"}


def test_triage_transition_refused(run, client, serve):
    """The table names no way back into under-review, so a patch there is left alone."""
    serve(CORE_ONLY, state="under-review")

    result = run("triage", "--force", CORE_ONLY)

    assert result.exit_code == 0
    assert client.written == []


def test_triage_twice_writes_once(run, client, serve):
    serve(CORE_ONLY)
    run("triage", CORE_ONLY)
    client.written.clear()
    client.asked.clear()

    result = run("triage", CORE_ONLY)

    assert result.exit_code == 0
    assert client.written == []
    assert client.asked == [], "a series already decided about is not read again"


def test_triage_malformed_diff(run, client, serve, tmp_path):
    """Ownership is settled before anything is written, so one bad diff costs the series."""
    serve(FOREIGN_MIXED)
    _, stranger = patch_ids(client, FOREIGN_MIXED)
    client.patch_docs[stranger]["diff"] = MALFORMED

    result = run("triage", FOREIGN_MIXED)

    assert result.exit_code == 1
    assert client.written == []
    assert fired(tmp_path) == []


def test_triage_dry_run(run, client, serve):
    """Nothing reaches patchwork and nothing is remembered, so the next run repeats it."""
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)

    result = run("triage", CORE_ONLY, dry_run=True)
    assert result.exit_code == 0
    assert client.written == []

    run("triage", CORE_ONLY)
    assert states(client) == {patch: "under-review"}


# -- the hook --------------------------------------------------------------


def test_triage_hook_handler_only(run, serve, tmp_path):
    """The series is core's, so the upstream it was taken back off hears nothing."""
    serve(CORE_AND_UPSTREAM)

    run("triage", CORE_AND_UPSTREAM)

    assert [document["handler"] for document in fired(tmp_path)] == ["core"]


def test_triage_hook_document(run, client, serve, tmp_path):
    serve(FOREIGN_MIXED)

    run("triage", FOREIGN_MIXED)

    (document,) = fired(tmp_path)
    assert document["id"] == FOREIGN_MIXED
    assert (document["handler"], document["owners"]) == ("imx", ["imx"])
    assert [p["id"] for p in document["patches"]] == patch_ids(client, FOREIGN_MIXED), (
        "patchwork's own keys, as served"
    )


def test_triage_hook_failure(run, client, serve, tmp_path):
    """A failing hook fails the run, but the series still leaves the queue."""
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)
    log = tmp_path / "hook.log"
    log.touch()
    log.chmod(0o444)
    try:
        result = run("triage", CORE_ONLY)
    finally:
        log.chmod(0o644)

    assert result.exit_code == 1
    assert states(client) == {patch: "under-review"}

    assert run("triage", CORE_ONLY).exit_code == 0
    assert fired(tmp_path) == [], "the series left the queue, so nothing runs a second time"


def test_triage_no_hook_flag(run, client, serve, tmp_path):
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)

    result = run("triage", CORE_ONLY, no_hook=True)

    assert result.exit_code == 0
    assert fired(tmp_path) == []
    assert states(client) == {patch: "under-review"}, "the triage happened all the same"


# -- sync ------------------------------------------------------------------


def test_sync_triages_what_arrives(run, client, serve):
    serve(FOREIGN_MIXED)
    owned, stranger = patch_ids(client, FOREIGN_MIXED)

    result = run("sync")

    assert result.exit_code == 0
    assert states(client) == {owned: "awaiting-upstream", stranger: "not-applicable"}


def test_sync_supersedes_earlier(run, client, serve):
    """A posting replaces the one before it, and one already replaced is left alone."""
    serve(UPSTREAM_ONLY)
    serve(UPSTREAM_ONLY_V2)
    run("sync")
    client.written.clear()
    client.asked.clear()

    serve(UPSTREAM_ONLY_V3)
    run("sync")

    (first,) = patch_ids(client, UPSTREAM_ONLY)
    (second,) = patch_ids(client, UPSTREAM_ONLY_V2)
    (third,) = patch_ids(client, UPSTREAM_ONLY_V3)
    assert states(client) == {
        first: "superseded",
        second: "superseded",
        third: "awaiting-upstream",
    }
    assert client.written == [(third, "awaiting-upstream", None), (second, "superseded", None)]
    assert client.asked == [third, second], "the posting already replaced is not read again"


def test_sync_supersede_inherits_delegate(run, client, serve):
    """A posting nobody is delegated keeps the delegate of the one it replaced; one delegated keeps its own."""
    serve(UPSTREAM_ONLY)
    serve(UPSTREAM_ONLY_V2)
    (first,) = patch_ids(client, UPSTREAM_ONLY)
    (second,) = patch_ids(client, UPSTREAM_ONLY_V2)
    owner = {"id": 2, "username": "b"}
    client.patch_docs[first]["delegate"] = {"id": 1, "username": "a"}
    client.patch_docs[second]["delegate"] = owner
    run("sync")

    serve(UPSTREAM_ONLY_V3)
    result = run("sync")

    (third,) = patch_ids(client, UPSTREAM_ONLY_V3)
    assert result.exit_code == 0
    assert client.patch_docs[second]["delegate"] == owner
    assert client.patch_docs[third]["delegate"] == owner["id"]


def test_sync_writes_only_what_arrived(run, client, serve):
    """The mark is the newest patch read, so only what is newer is written."""
    serve(UPSTREAM_ONLY)
    run("sync")
    client.written.clear()

    serve(CORE_ONLY)
    run("sync")

    (landed,) = patch_ids(client, CORE_ONLY)
    assert client.written == [(landed, "under-review", None)]


def test_sync_skips_archived(run, client, serve):
    serve(CORE_ONLY, archived=True)
    serve(UPSTREAM_ONLY)
    (live,) = patch_ids(client, UPSTREAM_ONLY)

    run("sync")

    assert client.written == [(live, "awaiting-upstream", None)]


def test_sync_no_triage(run, client, serve):
    """The store is filled and nothing is decided, until a triage asks."""
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)

    result = run("sync", "--no-triage")
    assert result.exit_code == 0
    assert client.written == []

    run("triage", CORE_ONLY)
    assert states(client) == {patch: "under-review"}


def test_sync_bad_series_does_not_block(run, client, serve):
    """It is stepped over, and reported once in all rather than once a run."""
    serve(CORE_ONLY)
    serve(UPSTREAM_ONLY)
    (broken,) = patch_ids(client, CORE_ONLY)
    (fine,) = patch_ids(client, UPSTREAM_ONLY)
    client.patch_docs[broken]["diff"] = MALFORMED

    result = run("sync")

    assert result.exit_code == 1
    assert states(client) == {broken: "new", fine: "awaiting-upstream"}
    assert run("sync").exit_code == 0, "the failure is not reported again"


# -- archive ---------------------------------------------------------------


def test_archive_idle_series(run, client, serve):
    """Quiet belongs to the series: it goes whole, or a reply on one patch keeps all."""
    serve(CORE_ONLY, ages=[200])
    serve(FOREIGN_MIXED, ages=[200, 10])
    (quiet,) = patch_ids(client, CORE_ONLY)
    run("sync", "--no-triage")

    result = run("archive", "--force", 180)

    assert result.exit_code == 0
    assert client.written == [(quiet, None, True)]


def test_archive_activity_saves_a_series(run, client, serve):
    """A reply after the timer keeps the series; one from before it changes nothing."""
    serve(CORE_ONLY, ages=[200])
    serve(UPSTREAM_ONLY, ages=[200])
    (replied,) = patch_ids(client, CORE_ONLY)
    (stale,) = patch_ids(client, UPSTREAM_ONLY)
    run("sync", "--no-triage")
    client.event_docs = [
        {"id": 1, "date": days_ago(3), "payload": {"patch": {"id": replied}}},
        {"id": 2, "date": days_ago(300), "payload": {"patch": {"id": stale}}},
    ]

    run("archive", 180)

    assert client.written == [(stale, None, True)]


def test_archive_force_skips_events(run, client, serve):
    """The reply would have saved the series, had patchwork been asked."""
    serve(CORE_ONLY, ages=[200])
    (patch,) = patch_ids(client, CORE_ONLY)
    run("sync", "--no-triage")
    client.event_docs = [{"id": 1, "date": days_ago(3), "payload": {"patch": {"id": patch}}}]

    run("archive", "--force", 180)

    assert client.written == [(patch, None, True)]


def test_archive_write_refused(run, client, serve):
    """Patchwork is written before the row is dropped, so a sweep that dies loses nothing."""
    serve(CORE_ONLY, ages=[200])
    (patch,) = patch_ids(client, CORE_ONLY)
    run("sync", "--no-triage")

    def refuse(**fields):
        raise PwError("patchwork is down")

    held, client.update_patch = client.update_patch, refuse
    assert run("archive", 180).exit_code == 1

    client.update_patch = held
    run("archive", 180)

    assert client.written == [(patch, None, True)]


# -- check ---------------------------------------------------------------

CHECK = ["--state", "success", "--context", "build", "--description", "ok", "--url", "https://ci"]


def checked(context: str = "build") -> dict:
    """The fields a check uploads, as patchwork spells them."""
    return {
        "state": "success",
        "context": context,
        "description": "ok",
        "target_url": "https://ci",
    }


def test_check_patch(run, client, serve):
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)
    run("sync")

    result = run(*CHECK, client.patch_docs[patch]["msgid"], entry=check.main)

    assert result.exit_code == 0
    assert client.checks == [(patch, checked())]


def test_check_series_cover_letter(run, client, serve):
    """The cover letter's message-id matches no patch; the series learns it when its hook reads it."""
    serve(FOREIGN_MIXED)
    run("sync")
    cover = client.series_docs[FOREIGN_MIXED]["cover_letter"]["msgid"]

    result = run("--series", *CHECK, cover, entry=check.main)

    assert result.exit_code == 0
    assert sorted(client.checks) == [
        (patch, checked("series-build")) for patch in sorted(patch_ids(client, FOREIGN_MIXED))
    ]


def test_check_series_from_patch(run, client, serve):
    """Any patch of the series names it, even one read from a listing alone."""
    serve(FOREIGN_MIXED)
    _, second = patch_ids(client, FOREIGN_MIXED)
    run("sync", "--no-triage")

    result = run("--series", *CHECK, client.patch_docs[second]["msgid"], entry=check.main)

    assert result.exit_code == 0
    assert sorted(client.checks) == [
        (patch, checked("series-build")) for patch in sorted(patch_ids(client, FOREIGN_MIXED))
    ]


def test_check_unknown_msgid(run, client, serve):
    serve(CORE_ONLY)
    run("sync")

    result = run("--series", *CHECK, "nobody@example.com", entry=check.main)

    assert result.exit_code == 1
    assert client.checks == []


# -- the paths a diff touches ----------------------------------------------


def test_paths_every_side_of_a_diff(bot, client, serve):
    """A file added has no source side, and /dev/null is not a path."""
    serve(FOREIGN_MIXED)
    owned, stranger = patch_ids(client, FOREIGN_MIXED)

    assert bot.patch_get(owned).paths() == ["drivers/clk/imx/clk-fracn-gppll.c"]
    assert bot.patch_get(stranger).paths() == [
        "arch/arm64/boot/dts/freescale/Makefile",
        "arch/arm64/boot/dts/freescale/imx93-kontron-dl.dtso",
    ]


def test_paths_malformed_diff(bot, client, serve):
    """The line unidiff quotes keeps its newline, which must not reach the message."""
    serve(CORE_ONLY)
    (patch,) = patch_ids(client, CORE_ONLY)
    client.patch_docs[patch]["diff"] = MALFORMED

    with pytest.raises(DiffError) as raised:
        bot.patch_get(patch).paths()

    assert str(raised.value) == (
        f"patch {patch}: cannot parse diff: Hunk diff line expected: 2.43.0"
    )


# -- the subject a chain is grouped on -------------------------------------


def test_split_strips_every_prefix():
    assert Series.split("[v2] clk: qcom: ipq-cmn-pll") == ("[v2] ", "clk: qcom: ipq-cmn-pll")
    assert Series.split("[RFC][PATCH v2] clk: x") == ("[RFC][PATCH v2] ", "clk: x")
    assert Series.split("[v3,1/2] dt-bindings: clock") == ("[v3,1/2] ", "dt-bindings: clock")
    assert Series.split("  clk: X  ") == ("  ", "clk: X")


def test_split_keeps_the_name():
    """Grouping takes the subject apart, costing it nothing but its trailing blanks."""
    prefix, subject = Series.split("[RFC][PATCH v2] clk: X  ")

    assert Series(prefix=prefix, subject=subject).name == "[RFC][PATCH v2] clk: X"


# -- the configuration file ------------------------------------------------


def test_config_upstream_default(load_config):
    config = load_config(
        "upstream:\n"
        "  default: {state: awaiting-upstream, hook: shared.sh}\n"
        "  imx: {paths: [drivers/clk/imx/], state: not-applicable}\n"
        "  tegra: {paths: [drivers/clk/tegra/]}\n"
    )

    imx, tegra = config.sections["imx"], config.sections["tegra"]
    assert (imx.state, imx.hook) == ("not-applicable", "shared.sh")
    assert (tegra.state, tegra.hook) == ("awaiting-upstream", "shared.sh")


def test_config_error_names_the_key(load_config):
    """The message names the key that was wrong."""
    with pytest.raises(ConfigError, match=r"patchwork\.url: must be https"):
        load_config("patchwork:\n  url: http://patchwork.kernel.org\n  project: linux-clk\n")
