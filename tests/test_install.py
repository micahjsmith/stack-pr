import configparser
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr import cli


def test_install_writes_global_alias(mocker) -> None:  # noqa: ANN001
    spy = mocker.patch("stack_pr.cli.run_shell_command")
    cli.command_install("stack", local=False)
    spy.assert_called_once()
    cmd = spy.call_args.args[0]
    assert cmd == ["git", "config", "--global", "alias.stack", "!stack-pr"]


def test_install_local_and_custom_name(mocker) -> None:  # noqa: ANN001
    spy = mocker.patch("stack_pr.cli.run_shell_command")
    cli.command_install("sp", local=True)
    cmd = spy.call_args.args[0]
    assert cmd == ["git", "config", "--local", "alias.sp", "!stack-pr"]


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
