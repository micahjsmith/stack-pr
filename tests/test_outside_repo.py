"""stack-pr commands that don't need a git repo must work outside one."""

import configparser
import sys
from pathlib import Path

import pytest

from stack_pr import cli


@pytest.fixture
def outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()
    # Stop git from discovering any repository above the temp dir.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv("STACKPR_CONFIG", raising=False)
    monkeypatch.chdir(workdir)
    return workdir


def run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["stack-pr", *argv])
    cli.main()


@pytest.mark.usefixtures("outside_repo")
def test_help_outside_repo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--help")
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_config_outside_repo_writes_stackpr_config(
    outside_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = outside_repo / "stack-pr.cfg"
    monkeypatch.setenv("STACKPR_CONFIG", str(config_file))

    run_main(monkeypatch, "config", "repo.target=develop")

    config = configparser.ConfigParser()
    config.read(config_file)
    assert config.get("repo", "target") == "develop"


def test_config_outside_repo_without_stackpr_config_errors(
    outside_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "config", "repo.target=develop")
    assert exc.value.code == 1
    assert "STACKPR_CONFIG" in capsys.readouterr().out
    assert list(outside_repo.iterdir()) == []


@pytest.mark.usefixtures("outside_repo")
def test_repo_command_outside_repo_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "view")
    assert exc.value.code == 1
    assert "not inside a git repository" in capsys.readouterr().out.lower()
