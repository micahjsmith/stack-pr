import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr.git import GitError, check_gh_installed


def test_check_gh_installed_raises_git_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(GitError, match="not installed"):
        check_gh_installed()
