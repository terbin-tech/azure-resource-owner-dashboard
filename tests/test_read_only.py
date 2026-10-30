"""Static checks that src/ stays read-only toward Azure.

The transport refuses writes at run time; these checks keep the code from asking for one.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "dashboard"
WRITE_PREFIXES = ("begin_", "create", "update", "delete", "put")
NETWORK_MODULES = (
    "subprocess",
    "socket",
    "requests",
    "urllib.request",
    "http.client",
    "azure.core.rest",
    # A transport sends whatever request it is given, so only clients.py, which wraps every one
    # in its guard, may build one.
    "azure.core.pipeline.transport",
)
ALLOWED_NETWORK = {("src/dashboard/clients.py", "azure.core.pipeline.transport")}


def write_calls(source: str) -> list[str]:
    return [
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith(WRITE_PREFIXES)
    ]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_import_nodes(source: str) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement in source order, skipping the bodies of `if TYPE_CHECKING:`."""
    nodes: list[ast.Import | ast.ImportFrom] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                walk(child)
            return
        if isinstance(node, ast.Import | ast.ImportFrom):
            nodes.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(ast.parse(source))
    return nodes


def runtime_imports(source: str) -> list[str]:
    modules: list[str] = []
    for node in _runtime_import_nodes(source):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        else:
            modules.append("." * node.level + (node.module or ""))
    return modules


def network_imports(source: str) -> list[str]:
    """The banned network modules source imports, also as `from X import submodule`."""
    found: list[str] = []
    for node in _runtime_import_nodes(source):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue  # a relative import stays inside the package
        found.extend(
            name
            for name in names
            if any(name == m or name.startswith(m + ".") for m in NETWORK_MODULES)
        )
    return found


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _key(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _is_client_module(module: str) -> bool:
    """An azure.mgmt module other than a models module: where the client classes live."""
    parts = module.split(".")
    return parts[:2] == ["azure", "mgmt"] and "models" not in parts


def test_the_checker_catches_a_planted_write():
    assert write_calls("c.resource_groups.begin_delete('x')\nd.update(e)\n") == [
        "begin_delete",
        "update",
    ]


def test_the_import_walk_skips_type_checking_blocks():
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from azure.mgmt.monitor import MonitorManagementClient\n"
        "import socket\n"
    )
    assert runtime_imports(source) == ["typing", "socket"]


def test_no_write_calls_in_src():
    files = _src_files()
    assert "clients.py" in [f.name for f in files]
    for path in files:
        assert write_calls(path.read_text(encoding="utf-8")) == [], path.name


def test_only_clients_py_builds_azure_clients():
    # Request and model classes come from azure.mgmt.*.models anywhere; a client type used
    # only in annotations is imported under `if TYPE_CHECKING:`, which runtime_imports skips.
    found = {
        path.relative_to(SRC).as_posix(): [
            m for m in runtime_imports(path.read_text(encoding="utf-8")) if _is_client_module(m)
        ]
        for path in _src_files()
    }
    assert found.pop("clients.py"), "the check no longer sees clients.py build the clients"
    assert {name: modules for name, modules in found.items() if modules} == {}


def test_the_network_check_catches_planted_imports():
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import socket\n"
        "from azure.core import rest\n"
        "from urllib import request, parse\n"
        "from urllib.parse import urlsplit\n"
        "import http.client\n"
        "from azure.core.pipeline.transport import RequestsTransport\n"
    )
    assert network_imports(source) == [
        "azure.core.rest",
        "urllib.request",
        "http.client",
        "azure.core.pipeline.transport",
        "azure.core.pipeline.transport.RequestsTransport",
    ]


def _allowed(key: str, name: str) -> bool:
    return any(key == k and (name == m or name.startswith(m + ".")) for k, m in ALLOWED_NETWORK)


def test_no_other_network_path():
    found = {
        (_key(path), name)
        for path in _src_files()
        for name in network_imports(path.read_text(encoding="utf-8"))
    }
    assert any(_allowed(*f) for f in found), "the check no longer sees clients.py's transport"
    assert {f for f in found if not _allowed(*f)} == set()
