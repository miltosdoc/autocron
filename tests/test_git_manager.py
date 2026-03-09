"""Tests for the GitManager — git version control."""
import os
import tempfile
import pytest
from autocron.git_manager import GitManager


@pytest.fixture
def git_repo():
    d = tempfile.mkdtemp()
    gm = GitManager(repo_dir=d)
    gm.init()
    return gm


def test_init_creates_repo(git_repo):
    """init() should create a .git directory."""
    assert os.path.isdir(os.path.join(git_repo.repo_dir, ".git"))


def test_init_idempotent(git_repo):
    """Calling init() again should not fail."""
    result = git_repo.init()
    assert result is False  # Already exists


def test_commit_script(git_repo):
    """Committing a script should create a commit."""
    script_path = os.path.join(git_repo.repo_dir, "test.sh")
    with open(script_path, "w") as f:
        f.write("#!/bin/bash\necho test\n")

    commit_hash = git_repo.commit_script("test.sh", "Add test script")
    assert commit_hash is not None
    assert len(commit_hash) >= 7


def test_log(git_repo):
    """Log should return recent commits."""
    # There's already an initial commit from init()
    log = git_repo.log(n=5)
    assert len(log) >= 1
    assert "message" in log[0]


def test_tag_deployment(git_repo):
    """Tagging should create a git tag."""
    tag = git_repo.tag_deployment("backup", round_count=3)
    assert "deploy/" in tag
    assert "backup" in tag


def test_commit_all(git_repo):
    """commit_all should stage and commit everything."""
    new_file = os.path.join(git_repo.repo_dir, "new.txt")
    with open(new_file, "w") as f:
        f.write("hello\n")

    commit_hash = git_repo.commit_all("Add new file")
    assert commit_hash is not None


def test_status_summary(git_repo):
    """Status summary should include repo info."""
    summary = git_repo.status_summary()
    assert "repo_dir" in summary
    assert "recent_commits" in summary


def test_no_remote_by_default(git_repo):
    """Fresh repo should not have a remote."""
    assert not git_repo.has_remote()
