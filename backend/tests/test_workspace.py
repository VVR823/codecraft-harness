import importlib

import pytest


def module():
    return importlib.import_module("app.runtime.workspace")


def test_two_runs_have_private_copies(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("x = 1")
    (source / ".env").write_text("SECRET=excluded")
    a = module().prepare_workspace(source, "run-a", tmp_path / "data")
    b = module().prepare_workspace(source, "run-b", tmp_path / "data")
    (a.root / "code.py").write_text("x = 2")
    assert (b.root / "code.py").read_text() == "x = 1"
    assert (source / "code.py").read_text() == "x = 1"
    assert not (a.root / ".env").exists()


@pytest.mark.parametrize("name", ["../source", "../../backend", ".", "a/b", "C:/Windows"])
def test_pack_names_cannot_select_other_directories(tmp_path, name):
    with pytest.raises(ValueError):
        module().resolve_pack(name, tmp_path)


def test_symlink_source_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret")
    try:
        (source / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit symlink creation")
    with pytest.raises(ValueError, match="链接"):
        module().prepare_workspace(source, "r", tmp_path / "data")


def test_gitfile_is_not_copied_into_any_snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").write_text("gitdir: /some/other/repository")
    (source / "code.py").write_text("x=1")
    snapshot = module().prepare_workspace(source, "gitfile", tmp_path / "data")
    assert not (snapshot.root / ".git").exists()
    assert not (snapshot.root.parent / "original/.git").exists()
    assert ".git" not in snapshot.manifest


def test_github_requires_independent_git_directory_before_any_mutation(tmp_path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").write_text("gitdir: /some/other/repository")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("must reject gitfile before running git")
    )
    with pytest.raises(ValueError, match="独立"):
        module().prepare_workspace(source, "delivery", tmp_path / "data", github=True)
    assert not (tmp_path / "data/runs/delivery").exists()


def test_sensitive_names_use_conservative_case_insensitive_checks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in [".ENV", ".env.production", "private.KEY", "private.PEM"]:
        (source / name).write_text("secret")
    (source / "code.py").write_text("x=1")
    snapshot = module().prepare_workspace(source, "case", tmp_path / "data")
    assert {p.name for p in snapshot.root.iterdir()} == {"code.py"}
    from pathlib import Path

    assert module().protected(Path("Tests/check.py"))
    assert module().protected(Path(".GIT/config"))


def test_github_copy_preserves_source_history_without_touching_source(tmp_path):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    (source / "code.py").write_text("x=1\n")
    git("add", "code.py")
    git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    git("remote", "add", "origin", "https://example.invalid/project.git")
    before = git("rev-parse", "HEAD")
    snapshot = module().prepare_workspace(source, "history", tmp_path / "data", github=True)
    assert git("merge-base", before, "HEAD", cwd=snapshot.root) == before
    assert git("rev-parse", "HEAD", cwd=snapshot.root) == before
    assert git("remote", "get-url", "origin", cwd=snapshot.root) == "https://example.invalid/project.git"
    assert git("rev-parse", "HEAD") == before and git("status", "--porcelain") == ""


def test_github_source_never_copies_credentials_embedded_in_origin(tmp_path):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git("remote", "add", "origin", "https://user:fixture-private-token@github.com/example/repo.git")
    with pytest.raises(ValueError, match="凭证"):
        module().prepare_workspace(source, "credentials", tmp_path / "data", github=True)
    assert not (tmp_path / "data/runs/credentials").exists()
