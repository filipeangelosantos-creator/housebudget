"""Where the data lives. Getting this wrong loses someone's financial history,
so the resolution rules are pinned down here.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def resolve_data_dir(env: dict, sys_path: str = str(REPO)) -> str:
    """Import app.config in a clean subprocess and report DATA_DIR."""
    code = (f"import sys; sys.path.insert(0, {sys_path!r});"
            "from app import config; print(config.DATA_DIR)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, check=True)
    return out.stdout.strip()


def base_env(tmp_path: Path) -> dict:
    return {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}


def test_fresh_install_keeps_data_outside_the_checkout(tmp_path):
    """Deleting or re-cloning the code must never take the database with it."""
    data_dir = Path(resolve_data_dir(base_env(tmp_path)))
    assert data_dir == tmp_path / "home" / ".housebudget"
    assert REPO not in data_dir.parents, "data would live inside the git checkout"


def test_explicit_setting_wins(tmp_path):
    env = base_env(tmp_path) | {"HB_DATA_DIR": str(tmp_path / "elsewhere")}
    assert resolve_data_dir(env) == str(tmp_path / "elsewhere")


def test_existing_install_keeps_its_data_dir(tmp_path):
    """An install that already stores data in the checkout must not silently
    switch locations and appear to have lost everything."""
    fake_repo = tmp_path / "housebudget"
    (fake_repo / "app").mkdir(parents=True)
    (fake_repo / "data").mkdir()
    (fake_repo / "app" / "config.py").write_text(
        (REPO / "app" / "config.py").read_text())
    resolved = resolve_data_dir(base_env(tmp_path), sys_path=str(fake_repo))
    assert resolved == str(fake_repo / "data")


def test_data_dir_is_gitignored():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "data/budget.db"], cwd=REPO)
    assert ignored.returncode == 0, "data/ must never be committable"


def test_no_data_files_are_tracked_by_git():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    leaked = [f for f in tracked
              if f.startswith("data/") or f.endswith((".db", ".sqlite3", ".key"))]
    assert not leaked, f"financial data tracked in git: {leaked}"


def test_config_module_creates_nothing_on_import(tmp_path):
    """Importing config must not create directories — run.sh imports it just to
    print the path, and a typo'd HB_DATA_DIR shouldn't scatter folders."""
    target = tmp_path / "should-not-exist"
    env = base_env(tmp_path) | {"HB_DATA_DIR": str(target)}
    assert resolve_data_dir(env) == str(target)
    assert not target.exists()
