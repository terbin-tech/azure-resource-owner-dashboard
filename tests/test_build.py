import os
import shutil
import zipfile
from pathlib import Path

from scripts import build_function

ROOT = Path(__file__).resolve().parents[1]
TOP = ["function_app.py", "host.json", "requirements.txt", "settings.json"]


def _package() -> list[str]:
    pkg = ROOT / "src" / "dashboard"
    return [
        "dashboard/" + p.relative_to(pkg).as_posix()
        for p in pkg.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _copy_root(dest: Path) -> Path:
    """The files the build reads, copied to `dest`, so a test can plant files or touch times."""
    for name in TOP:
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, dest / name)
    shutil.copytree(
        ROOT / "src" / "dashboard",
        dest / "src" / "dashboard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return dest


def test_zip_holds_exactly_the_function_files(tmp_path):
    dest = tmp_path / "function.zip"
    names = build_function.build(ROOT, dest)
    assert names == sorted(TOP + _package())
    assert "dashboard/__init__.py" in names
    assert "dashboard/web.py" in names
    assert zipfile.ZipFile(dest).namelist() == names


def test_zip_is_reproducible(tmp_path):
    root = _copy_root(tmp_path / "root")
    build_function.build(root, tmp_path / "a.zip")
    # A fresh checkout changes times and may change modes.
    os.utime(root / "function_app.py", (0, 0))
    os.chmod(root / "src" / "dashboard" / "web.py", 0o700)
    build_function.build(root, tmp_path / "b.zip")
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()
    for info in zipfile.ZipFile(tmp_path / "a.zip").infolist():
        assert info.date_time == build_function.FIXED_TIME
        assert info.external_attr == 0o644 << 16


def test_zip_has_no_bytecode_or_secrets(tmp_path):
    root = _copy_root(tmp_path / "root")
    planted = [
        "src/dashboard/__pycache__/web.cpython-314.pyc",
        "src/dashboard/__pycache__/x.py",
        "src/dashboard/web.pyc",
        "__pycache__/function_app.cpython-314.pyc",
        "dashboard.toml",
        "roster.csv",
        ".denylist",
        "local.settings.json",
        "out/deploy/errors.log",
        "src/dashboard/dashboard.toml",
    ]
    for name in planted:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("planted\n")
    names = build_function.build(root, tmp_path / "function.zip")
    assert names == sorted(TOP + _package())
    for name in names:
        assert "__pycache__" not in name
        assert not name.endswith(".pyc")
        assert Path(name).name not in {
            "dashboard.toml",
            "roster.csv",
            ".denylist",
            "local.settings.json",
        }
        assert not name.startswith("out/")
