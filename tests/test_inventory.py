import json
import subprocess
import sys
from pathlib import Path

import pytest

from inventory import inventory, tarjan

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inventory.py"

APP_CORE = '''"""Core."""
import os
import json
import requests
from . import utils
from .models import User


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except:
        return {}


def risky(cmd, items=[]):
    import subprocess
    subprocess.run(cmd, shell=True)
    password = "hunter2hunter2"
    if cmd and items or path_exists():
        for i in items:
            if i:
                eval(i)
    return password


def path_exists():
    return os.path.exists("x")  # TODO: cache
'''


@pytest.fixture
def repo(tmp_path):
    files = {
        "pyproject.toml": '[project]\nname = "demo"\ndependencies = ["requests>=2", "pyyaml"]\n'
                          '[project.scripts]\ndemo = "demo.cli:main"\n[tool.ruff]\nline-length = 100\n',
        "README.md": "# demo\n",
        "LICENSE": "MIT\n",
        ".github/workflows/ci.yml": "name: ci\n",
        "src/demo/__init__.py": '"""Demo."""\n',
        "src/demo/core.py": APP_CORE,
        "src/demo/utils.py": "from . import core\n\ndef helper():\n    return core.load('x')\n",
        "src/demo/models.py": "class User:\n    def a(self): ...\n    def b(self): ...\n",
        "src/demo/cli.py": "import yaml\nimport numpy\nfrom demo.core import load\n\n\ndef main():\n    print(load('x'))\n\n\nif __name__ == '__main__':\n    main()\n",
        "tests/test_core.py": "from demo.core import load\n\ndef test_load():\n    assert load('nope') == {}\n",
        "Dockerfile": "FROM python:latest\nCOPY . /app\nENTRYPOINT [\"python\", \"-m\", \"demo\"]\n",
        "legacy.py": "print 'python 2'\n",
        "node_modules/x/index.js": "x",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def line_of(text, needle):
    """1-based line of the first line containing needle (keeps expectations honest)."""
    return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)


def signals(inv, kind):
    return [s for s in inv["python"]["signals"] if s["kind"] == kind]


def test_languages_and_skips(repo):
    inv = inventory(str(repo))
    assert inv["languages"]["Python"]["files"] == 7
    assert "JavaScript" not in inv["languages"]
    assert "node_modules" in inv["skipped_dirs"]


def test_python_parse_errors_reported_not_fatal(repo):
    inv = inventory(str(repo))
    assert [e["path"] for e in inv["parse_errors"]] == ["legacy.py"]
    assert any("failed to parse" in item for item in inv["limits"])


def test_dependencies_and_undeclared_heuristic(repo):
    inv = inventory(str(repo))
    names = {d["name"] for d in inv["dependencies"]["declared"]}
    assert names == {"requests", "pyyaml"}
    dep = next(d for d in inv["dependencies"]["declared"] if d["name"] == "pyyaml")
    assert dep["line"] == 3 and dep["pinned"] is False
    undeclared = inv["python"]["possibly_undeclared_deps"]
    assert undeclared["checked"] is True
    assert [i["import"] for i in undeclared["items"]] == ["numpy"]  # yaml maps to pyyaml


def test_entry_points(repo):
    inv = inventory(str(repo))
    kinds = {(e["kind"], e["name"]) for e in inv["entry_points"]}
    assert ("console_script", "demo") in kinds
    assert ("dockerfile_entrypoint", "ENTRYPOINT") in kinds
    guard = next(e for e in inv["entry_points"] if e["kind"] == "main_guard")
    assert guard["path"] == "src/demo/cli.py" and guard["line"] == 10


def test_dockerfile_unpinned_base(repo):
    docker = inventory(str(repo))["dockerfiles"][0]
    assert docker["from"][0]["unpinned"] is True
    assert docker["user"] is None


def test_signals_have_path_and_line(repo):
    inv = inventory(str(repo))
    expected = {"bare_except": line_of(APP_CORE, "except:"),
                "shell_true": line_of(APP_CORE, "shell=True"),
                "mutable_default": line_of(APP_CORE, "items=[]"),
                "eval_exec": line_of(APP_CORE, "eval(i)"),
                "todo_comment": line_of(APP_CORE, "TODO"),
                "hardcoded_secret_candidate": line_of(APP_CORE, "password =")}
    for kind, line in expected.items():
        found = signals(inv, kind)
        assert found, kind
        assert found[0]["path"] == "src/demo/core.py"
        assert found[0]["line"] == line, kind


def test_secret_value_is_never_echoed(repo):
    inv = inventory(str(repo))
    assert "hunter2" not in json.dumps(inv)


def test_import_graph_cycle_and_lazy(repo):
    graph = inventory(str(repo))["python"]["import_graph"]
    assert graph["cycles_all"] == [["demo.core", "demo.utils"]]
    assert graph["cycles_eager"] == [["demo.core", "demo.utils"]]
    edges = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("demo.cli", "demo.core") in edges
    assert ("demo.core", "demo.models") in edges


def test_function_metrics(repo):
    inv = inventory(str(repo), full=True)
    risky = next(f for f in inv["python"]["functions"] if f["qualname"] == "risky")
    assert risky["line"] == line_of(APP_CORE, "def risky") and risky["cyclomatic"] >= 6
    assert inv["python"]["most_complex_functions"][0]["qualname"] == "risky"
    assert inv["python"]["largest_classes"][0]["name"] == "User"


def test_project_signals(repo):
    sig = inventory(str(repo))["project_signals"]
    assert sig["test_modules"] == 1 and sig["test_functions"] == 1
    assert sig["ci_files"] == [".github/workflows/ci.yml"]
    assert "pyproject.toml" in sig["lint_or_type_config"]
    assert sig["license_files"] == ["LICENSE"]


def test_tarjan_finds_only_real_cycles():
    assert tarjan({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [["a", "b", "c"]]
    assert tarjan({"a": {"b"}, "b": set()}) == []


def test_setup_py_read_statically(tmp_path):
    marker = tmp_path / "ran"
    (tmp_path / "setup.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n"
        "from setuptools import setup\n"
        "setup(name='x', install_requires=['flask>=2'], entry_points={'console_scripts': ['x=x.cli:main']})\n")
    inv = inventory(str(tmp_path))
    assert not marker.exists()
    assert inv["dependencies"]["declared"][0]["name"] == "flask"
    assert any(e["name"] == "x" for e in inv["entry_points"])


def test_dynamic_install_requires_is_declared(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(install_requires=read())\n")
    notes = inventory(str(tmp_path))["dependencies"]["notes"]
    assert any("dynamically" in n for n in notes)


def test_no_manifest_means_undeclared_check_skipped(tmp_path):
    (tmp_path / "a.py").write_text("import numpy\n")
    result = inventory(str(tmp_path))["python"]["possibly_undeclared_deps"]
    assert result["checked"] is False


def test_single_file_input(tmp_path):
    f = tmp_path / "solo.py"
    f.write_text("def f():\n    return 1\n")
    inv = inventory(str(f))
    assert inv["python"]["stats"]["functions"] == 1
    assert any("Single-file" in item for item in inv["limits"])


def test_max_files_truncation(tmp_path):
    for i in range(5):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    inv = inventory(str(tmp_path), max_files=3)
    assert any("stopped" in item for item in inv["limits"])


def test_cli_writes_json_file(repo, tmp_path):
    out = tmp_path / "inv.json"
    subprocess.run([sys.executable, str(SCRIPT), str(repo), "--out", str(out)], check=True, capture_output=True)
    assert json.loads(out.read_text())["python"]["stats"]["functions"] > 0


def test_sql_built_in_a_variable_is_flagged(tmp_path):
    (tmp_path / "db.py").write_text(
        "def a(term, conn):\n"
        "    query = f\"SELECT code FROM links WHERE url LIKE '%{term}%'\"\n"
        "    return conn.execute(query)\n"
        "def b(x):\n    return \"DELETE FROM t WHERE id = %s\" % x\n"
        "def c(x):\n    return \"UPDATE t SET a = {}\".format(x)\n"
        "def safe(conn, x):\n    return conn.execute(\"SELECT a FROM t WHERE id = ?\", (x,))\n"
        "def prose(n):\n    return f\"We will select from {n} options\"\n")
    found = sorted(s["line"] for s in signals(inventory(str(tmp_path)), "sql_string_building"))
    assert found == [2, 5, 7]          # no duplicates, no hit on parametrized SQL or ordinary prose
