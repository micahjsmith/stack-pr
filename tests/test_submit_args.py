import configparser
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli


def _submit_args(argv: list[str], config=None):  # noqa: ANN001, ANN202
    parser = cli.create_argparser(config or configparser.ConfigParser())
    return parser.parse_args(["submit", *argv])


def test_keep_body_and_title_default_true() -> None:
    args = _submit_args([])
    assert args.keep_body is True
    assert args.keep_title is True


def test_no_keep_flags_disable() -> None:
    args = _submit_args(["--no-keep-body", "--no-keep-title"])
    assert args.keep_body is False
    assert args.keep_title is False


def test_keep_flags_can_be_forced_on() -> None:
    args = _submit_args(["--keep-body", "--keep-title"])
    assert args.keep_body is True
    assert args.keep_title is True


def test_config_can_override_default_to_false() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("common")
    cfg.set("common", "keep_body", "false")
    cfg.set("common", "keep_title", "false")

    # With the config default false and no flag, both are off...
    assert _submit_args([], cfg).keep_body is False
    assert _submit_args([], cfg).keep_title is False
    # ...but an explicit flag still wins over the config.
    forced = _submit_args(["--keep-body", "--keep-title"], cfg)
    assert forced.keep_body is True
    assert forced.keep_title is True
