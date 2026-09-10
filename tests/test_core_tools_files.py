from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from navi.core_tools.files import (
    _CheckpointStore,
    _checkpoint_store,
    _file_read,
    _file_write,
    _indent_replacement,
    _lock_payload,
    _lock_resource,
    _node_symbol_type,
    _python_ast_replace_symbol,
    _symbol_start_line,
    _write_transition,
)
from navi.loop_contracts import WorkspaceLock
from navi.workspaces import LockAcquireResult


def test_file_read_basic(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "test.txt"
    f.write_text("hello world", encoding="utf-8")

    res = _file_read({"path": "test.txt"}, project_dir=proj)
    assert res.ok is True
    assert res.facts["content"] == "hello world"
    assert res.facts["truncated"] is False

    res_trunc = _file_read({"path": "test.txt", "max_bytes": 5}, project_dir=proj)
    assert res_trunc.ok is True
    assert res_trunc.facts["content"] == "hello"
    assert res_trunc.facts["truncated"] is True

    res_not_found = _file_read({"path": "nonexistent.txt"}, project_dir=proj)
    assert res_not_found.ok is False
    assert res_not_found.error == "path not found"

    d = proj / "subdir"
    d.mkdir()
    res_dir = _file_read({"path": "subdir"}, project_dir=proj)
    assert res_dir.ok is False
    assert res_dir.error == "path is not a file"


def test_file_read_outside_project(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    res = _file_read({"path": "../../etc/passwd"}, project_dir=proj)
    assert res.ok is False


def test_file_write_basic(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()

    res_inv = _file_write({"path": "out.txt", "mode": "delete"}, project_dir=proj)
    assert res_inv.ok is False
    assert "mode must be overwrite or append" in res_inv.error

    d = proj / "dir"
    d.mkdir()
    res_isdir = _file_write({"path": "dir", "mode": "overwrite"}, project_dir=proj)
    assert res_isdir.ok is False
    assert "path is a directory" in res_isdir.error

    res_noparent = _file_write({"path": "sub/out.txt", "content": "1"}, project_dir=proj)
    assert res_noparent.ok is False
    assert "parent directory does not exist" in res_noparent.error

    res_create = _file_write(
        {"path": "sub/out.txt", "content": "line1\n", "create_dirs": True},
        project_dir=proj,
    )
    assert res_create.ok is True
    assert (proj / "sub" / "out.txt").read_text(encoding="utf-8") == "line1\n"

    res_append = _file_write(
        {"path": "sub/out.txt", "content": "line2\n", "mode": "append"},
        project_dir=proj,
    )
    assert res_append.ok is True
    assert (proj / "sub" / "out.txt").read_text(encoding="utf-8") == "line1\nline2\n"


def test_file_write_with_checkpoint_and_locks(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    with patch("navi.core_tools.files._checkpoint_store") as mock_chk:
        mock_store = MagicMock()
        mock_store.snapshot.return_value = "snap-123"
        mock_chk.return_value = mock_store

        res = _file_write(
            {"path": "data.txt", "content": "hello", "checkpoint": True},
            project_dir=proj,
            home=home,
        )
        assert res.ok is True
        assert res.facts["checkpoint_id"] == "snap-123"

    with patch("navi.harness.Harness.acquire_workspace_lock") as mock_lock:
        raw_lock = WorkspaceLock(
            owner_run_id="run-1",
            resource="data.txt",
            mode="write",
            lease_expiry=9999999999.0,
        )
        mock_lock.return_value = LockAcquireResult(
            acquired=False,
            lock=raw_lock,
            conflicts=(),
        )
        res_blocked = _file_write({"path": "data.txt", "content": "test"}, project_dir=proj, home=home)
        assert res_blocked.ok is False
        assert res_blocked.error == "workspace lock conflict"


def test_file_write_shadow_workspace(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()

    res_no_home = _file_write(
        {"path": "file.txt", "content": "x", "shadow_run_id": "sh-1"},
        project_dir=proj,
    )
    assert res_no_home.ok is False
    assert "shadow writes require home" in res_no_home.error

    with patch("navi.harness.Harness.get_shadow_workspace", return_value=None):
        res_no_sh = _file_write(
            {"path": "file.txt", "content": "x", "shadow_run_id": "sh-1"},
            project_dir=proj,
            home=home,
        )
        assert res_no_sh.ok is False
        assert "active shadow workspace not found" in res_no_sh.error

    shadow_meta = MagicMock(status="active", shadow_workspace=str(shadow_dir))
    with patch("navi.harness.Harness.get_shadow_workspace", return_value=shadow_meta):
        res_ok = _file_write(
            {"path": "file.txt", "content": "shadow_content", "shadow_run_id": "sh-1"},
            project_dir=proj,
            home=home,
        )
        assert res_ok.ok is True
        assert (shadow_dir / "file.txt").read_text(encoding="utf-8") == "shadow_content"


def test_python_ast_replace_symbol_validations(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    f_py = proj / "module.py"
    f_py.write_text("def old_fn():\n    return 1\n", encoding="utf-8")

    res_no_sym = _python_ast_replace_symbol({"path": "module.py"}, project_dir=proj)
    assert res_no_sym.ok is False
    assert "symbol_name is required" in res_no_sym.error

    res_inv_type = _python_ast_replace_symbol(
        {"path": "module.py", "symbol_name": "fn", "symbol_type": "variable"},
        project_dir=proj,
    )
    assert res_inv_type.ok is False
    assert "symbol_type must be" in res_inv_type.error

    f_txt = proj / "file.txt"
    f_txt.write_text("hello", encoding="utf-8")
    res_not_py = _python_ast_replace_symbol(
        {"path": "file.txt", "symbol_name": "fn"},
        project_dir=proj,
    )
    assert res_not_py.ok is False
    assert "requires a .py file" in res_not_py.error

    res_missing = _python_ast_replace_symbol(
        {"path": "missing.py", "symbol_name": "fn"},
        project_dir=proj,
    )
    assert res_missing.ok is False
    assert "path not found" in res_missing.error


def test_python_ast_replace_symbol_syntax_errors(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    broken_py = proj / "broken.py"
    broken_py.write_text("def broken_syntax(:\n", encoding="utf-8")

    res_broken = _python_ast_replace_symbol(
        {"path": "broken.py", "symbol_name": "fn", "replacement": "def fn(): pass"},
        project_dir=proj,
    )
    assert res_broken.ok is False
    assert "existing file is not valid Python" in res_broken.error

    valid_py = proj / "valid.py"
    valid_py.write_text("def fn():\n    pass\n", encoding="utf-8")

    res_empty_rep = _python_ast_replace_symbol(
        {"path": "valid.py", "symbol_name": "fn", "replacement": "   "},
        project_dir=proj,
    )
    assert res_empty_rep.ok is False
    assert "replacement is required" in res_empty_rep.error

    res_bad_rep = _python_ast_replace_symbol(
        {"path": "valid.py", "symbol_name": "fn", "replacement": "def bad_syntax(:\n"},
        project_dir=proj,
    )
    assert res_bad_rep.ok is False
    assert "replacement is not valid Python" in res_bad_rep.error

    res_mismatch = _python_ast_replace_symbol(
        {"path": "valid.py", "symbol_name": "fn", "replacement": "def other(): pass"},
        project_dir=proj,
    )
    assert res_mismatch.ok is False
    assert "replacement must define exactly one matching" in res_mismatch.error

    res_not_found = _python_ast_replace_symbol(
        {"path": "valid.py", "symbol_name": "absent", "replacement": "def absent(): pass"},
        project_dir=proj,
    )
    assert res_not_found.ok is False
    assert "expected exactly one matching symbol, found 0" in res_not_found.error


def test_python_ast_replace_symbol_success(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    target_py = proj / "app.py"
    target_py.write_text(
        "import os\n\ndef compute(x: int) -> int:\n    return x + 1\n\nclass Worker:\n    def run(self):\n        pass\n",
        encoding="utf-8",
    )

    res_fn = _python_ast_replace_symbol(
        {
            "path": "app.py",
            "symbol_name": "compute",
            "replacement": "def compute(x: int) -> int:\n    return x * 2\n",
        },
        project_dir=proj,
    )
    assert res_fn.ok is True
    assert "return x * 2" in target_py.read_text(encoding="utf-8")
    assert res_fn.facts["symbol_type"] == "function"

    res_cls = _python_ast_replace_symbol(
        {
            "path": "app.py",
            "symbol_name": "Worker",
            "symbol_type": "class",
            "replacement": "class Worker:\n    enabled = True\n",
        },
        project_dir=proj,
    )
    assert res_cls.ok is True
    assert "enabled = True" in target_py.read_text(encoding="utf-8")
    assert res_cls.facts["symbol_type"] == "class"


def test_checkpoint_store_lifecycle(tmp_path: Path):
    proj = tmp_path / "git_proj"
    proj.mkdir()
    file_a = proj / "a.txt"
    file_a.write_text("version 1", encoding="utf-8")

    store = _checkpoint_store(proj)

    with patch("navi.core_tools.run_command._run_git") as mock_git:
        mock_git.side_effect = [
            {"exit_code": 0, "stdout": "stash@{0}\n", "stderr": ""},
            {"exit_code": 0, "stdout": "", "stderr": ""},
        ]
        cid = store.snapshot(path=file_a, reason="before test edit")
        assert len(cid) == 32
        assert (proj / ".navi-checkpoints.json").exists()

        restored = store.restore(cid)
        assert restored is True

        restored_missing = store.restore("nonexistent-id")
        assert restored_missing is False

    corrupt_sidecar = proj / ".navi-checkpoints.json"
    corrupt_sidecar.write_text('{"invalid": "format"}', encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint metadata must be a list"):
        store.snapshot(path=file_a, reason="test")

    with pytest.raises(ValueError, match="checkpoint metadata must be a list"):
        store.restore(cid)


def test_helpers(tmp_path: Path):
    assert _write_transition("overwrite", shadow=False) == "written"
    assert _write_transition("append", shadow=True) == "shadow_appended"
    assert _write_transition("custom", shadow=False) == "written"

    assert _lock_payload(None) == {}
    lock = WorkspaceLock(
        owner_run_id="run-1",
        resource="test",
        mode="write",
        lease_expiry=12345.0,
    )
    assert _lock_payload(lock)["owner_run_id"] == "run-1"

    rel = _lock_resource(tmp_path / "foo.txt", project_dir=tmp_path)
    assert rel == "foo.txt"

    other_dir = tmp_path.parent / "other"
    rel_other = _lock_resource(other_dir / "foo.txt", project_dir=tmp_path)
    assert str(other_dir) in rel_other

    import ast
    dummy_ast = ast.Pass()
    assert _node_symbol_type(dummy_ast) == "unknown"

    indented = _indent_replacement("line1\nline2", "    ")
    assert indented == "    line1\n    line2\n"

    parsed = ast.parse("@dec\ndef foo(): pass")
    fn_node = parsed.body[0]
    assert _symbol_start_line(fn_node) > 0


def test_python_ast_replace_symbol_shadow_and_concurrent(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()

    f_py = proj / "mod.py"
    f_py.write_text("def fn():\n    return 1\n", encoding="utf-8")

    res_no_home = _python_ast_replace_symbol(
        {"path": "mod.py", "symbol_name": "fn", "replacement": "def fn(): pass", "shadow_run_id": "sh-1"},
        project_dir=proj,
    )
    assert res_no_home.ok is False
    assert "shadow AST patches require home" in res_no_home.error

    with patch("navi.harness.Harness.get_shadow_workspace", return_value=None):
        res_no_sh = _python_ast_replace_symbol(
            {"path": "mod.py", "symbol_name": "fn", "replacement": "def fn(): pass", "shadow_run_id": "sh-1"},
            project_dir=proj,
            home=home,
        )
        assert res_no_sh.ok is False
        assert "active shadow workspace not found" in res_no_sh.error

    shadow_meta = MagicMock(status="active", shadow_workspace=str(shadow_dir))
    shadow_f = shadow_dir / "mod.py"
    shadow_f.write_text("def fn():\n    return 1\n", encoding="utf-8")
    with patch("navi.harness.Harness.get_shadow_workspace", return_value=shadow_meta):
        res_sh_ok = _python_ast_replace_symbol(
            {"path": "mod.py", "symbol_name": "fn", "replacement": "def fn():\n    return 2\n", "shadow_run_id": "sh-1"},
            project_dir=proj,
            home=home,
        )
        assert res_sh_ok.ok is True
        assert "return 2" in shadow_f.read_text(encoding="utf-8")

