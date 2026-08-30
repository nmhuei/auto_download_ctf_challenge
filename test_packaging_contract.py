"""Packaging regression contracts for source distributions/wheels."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _setup_call():
    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup":
            return node
    raise AssertionError("setup.py does not call setup()")


def _keyword(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    raise AssertionError(f"setup() missing keyword {name!r}")


def test_setup_discovers_pep420_namespace_subpackages():
    call = _setup_call()
    packages = _keyword(call, "packages")
    assert isinstance(packages, ast.Call)
    assert getattr(packages.func, "id", None) == "find_namespace_packages"

    text = (ROOT / "setup.py").read_text(encoding="utf-8")
    # These directories intentionally have no __init__.py; find_packages()
    # silently omitted them from wheels before this regression guard existed.
    for package_dir in (
        "platforms", "utils", "downloaders", "generator", "extractors",
    ):
        assert (ROOT / "ctf_downloader" / package_dir).is_dir()
        assert not (ROOT / "ctf_downloader" / package_dir / "__init__.py").exists()
    assert 'include=["ctf_downloader*"]' in text


def test_pep517_build_backend_is_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[build-system]" in text
    assert 'build-backend = "setuptools.build_meta"' in text
    assert "setuptools>=64" in text


def test_setup_and_requirements_both_declare_gzctf_crypto_dependency():
    setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
    req_lines = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert '"cryptography>=41.0.0"' in setup_text
    assert "cryptography>=41.0.0" in req_lines


def test_setup_and_requirements_both_declare_cloudflare_transport_dependency():
    setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")
    req_lines = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert '"curl_cffi>=0.7.4"' in setup_text
    assert "curl_cffi>=0.7.4" in req_lines


def test_build_py_is_forced_to_refresh_stale_build_lib_modules():
    text = (ROOT / "setup.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    force_cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ForceBuildPy"
    )
    finalize = next(
        node for node in force_cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "finalize_options"
    )
    assigns_true = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "force"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        for node in ast.walk(finalize)
    )
    assert assigns_true

    call = _setup_call()
    cmdclass = _keyword(call, "cmdclass")
    assert isinstance(cmdclass, ast.Dict)
    mapping = {
        key.value: value.id
        for key, value in zip(cmdclass.keys, cmdclass.values)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Name)
    }
    assert mapping.get("build_py") == "ForceBuildPy"


def test_dev_dependencies_and_quality_gate_include_wheel_integrity_check():
    dev_lines = {
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    quality = (ROOT / "scripts" / "verify_quality.py").read_text(encoding="utf-8")
    checker = ROOT / "scripts" / "verify_wheel_contents.py"
    assert "build>=1.2" in dev_lines
    assert "setuptools>=64" in dev_lines
    assert "wheel>=0.41" in dev_lines
    assert checker.is_file()
    assert "verify_wheel_contents.py" in quality
