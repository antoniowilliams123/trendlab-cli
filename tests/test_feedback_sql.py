"""Uplift U31: prompt-level feedback and read-only SQL analytics."""

from pathlib import Path

from typer.testing import CliRunner

from trendlab.cli import app
from trendlab.config.loader import trendlab_home
from trendlab.sessions.store import SessionStore


def test_feedback_lands_in_stats_and_sql_is_read_only(project: Path, _trendlab_home: Path):
    store = SessionStore(trendlab_home() / "sessions.db")
    store.create_session(str(project.resolve()), "m", "m")
    store.close()
    runner = CliRunner()
    assert runner.invoke(app, ["feedback", "good", "nailed it", "-C", str(project)]).exit_code == 0
    assert runner.invoke(app, ["feedback", "bad", "-C", str(project)]).exit_code == 0
    assert runner.invoke(app, ["feedback", "meh", "-C", str(project)]).exit_code != 0
    store = SessionStore(trendlab_home() / "sessions.db")
    q = store.stats(7)["online_quality"]
    store.close()
    assert q["feedback"] == {"good": 1, "bad": 1} and q["satisfaction"] == 0.5
    out = runner.invoke(app, ["sql", "select type, count(*) n from events group by type"])
    assert out.exit_code == 0 and "feedback.given" in out.output
    bad = runner.invoke(app, ["sql", "delete from events"])
    assert bad.exit_code != 0  # writes are refused
    sneaky = runner.invoke(app, ["sql", "with x as (select 1) delete from events"])
    assert sneaky.exit_code != 0  # the database itself is opened read-only
