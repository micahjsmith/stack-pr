import configparser
from pathlib import Path

import pytest

from stack_pr import cli
from tests.helpers import git, init_repo


def test_install_writes_global_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    cli.command_install("stack", local=False)

    alias = git(tmp_path, "config", "--file", str(global_config), "alias.stack")
    assert alias.strip() == "!stack-pr"


def test_install_local_and_custom_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    repo = init_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    cli.command_install("sp", local=True)

    assert git(repo, "config", "--local", "alias.sp").strip() == "!stack-pr"
    assert not global_config.exists()


def test_help_no_topic_prints_main_help(capsys) -> None:  # noqa: ANN001
    parser = cli.create_argparser(configparser.ConfigParser())
    cli.command_help(parser, None)
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "install" in out
    assert "help" in out


def test_help_topic_prints_subcommand_help(capsys) -> None:  # noqa: ANN001
    parser = cli.create_argparser(configparser.ConfigParser())
    # argparse prints the subcommand help and exits.
    with pytest.raises(SystemExit) as exc:
        cli.command_help(parser, "submit")
    assert exc.value.code == 0
    assert "submit" in capsys.readouterr().out


def test_install_and_help_args_parse() -> None:
    parser = cli.create_argparser(configparser.ConfigParser())

    install_args = parser.parse_args(["install", "--name", "sp", "--local"])
    assert install_args.command == "install"
    assert install_args.name == "sp"
    assert install_args.local is True

    help_args = parser.parse_args(["help", "submit"])
    assert help_args.command == "help"
    assert help_args.topic == "submit"
