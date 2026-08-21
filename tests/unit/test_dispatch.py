import shutil
import subprocess
from pathlib import Path


def test_dispatch_imports_dependencies_from_a_different_python_minor(tmp_path: Path) -> None:
    """Dispatch exposes packaged dependencies when the host Python minor differs."""
    charm_root = tmp_path / "charm"
    dispatch_source = Path(__file__).parents[2] / "dispatch"

    assert dispatch_source.is_file(), "the charm must supply a cross-minor dispatch"

    charm_root.mkdir()
    shutil.copy2(dispatch_source, charm_root / "dispatch")
    dependency_dir = charm_root / "venv/lib/python3.10/site-packages/example_dependency"
    dependency_dir.mkdir(parents=True)
    (dependency_dir / "__init__.py").write_text("VALUE = 'packaged dependency loaded'\n")
    (charm_root / "venv/pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.10.12\n")
    source_dir = charm_root / "src"
    source_dir.mkdir()
    (source_dir / "charm.py").write_text("from example_dependency import VALUE\nprint(VALUE)\n")
    (charm_root / "lib").mkdir()

    result = subprocess.run([str(charm_root / "dispatch")], check=True, capture_output=True, text=True)

    assert result.stdout.strip() == "packaged dependency loaded"
    assert not (charm_root / "venv/bin/python").exists()
