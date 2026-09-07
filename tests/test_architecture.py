from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_python_core_has_no_desktop_ui_imports(self) -> None:
        for path in (ROOT / "bloompilot").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    if node.module == "PIL":
                        modules.extend("PIL." + alias.name for alias in node.names)
                else:
                    continue
                for module in modules:
                    with self.subTest(path=path.name, module=module):
                        self.assertFalse(module == "tkinter" or module.startswith("tkinter."))
                        self.assertNotEqual(module, "PIL.ImageTk")

    def test_backend_imports_when_tkinter_is_unavailable(self) -> None:
        program = """
import importlib.abc
import sys
class NoDesktopUi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'tkinter' or fullname.startswith('tkinter.') or fullname == 'PIL.ImageTk':
            raise ImportError('Desktop UI must not be imported by the backend')
sys.meta_path.insert(0, NoDesktopUi())
from bloompilot.web.server import create_app
from bloompilot.phone_stream import ScrcpyRawCapture
from bloompilot.training_worker import main
assert create_app() is not None
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_backend_is_packaged_as_python_entry_point(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertFalse(project.get("gui-scripts"))
        self.assertEqual(project["scripts"]["bloompilot-backend"], "bloompilot.web.server:main")


if __name__ == "__main__":
    unittest.main()
