"""Exercise real pip resolution and hash-locked installation against a fixture registry."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path
from typing import Any

import pytest

from .. import package_setup


def test_resolve_and_install_exact_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve and import the same wheel through pip without an external registry.

    Args:
        tmp_path: Isolated guest-like workspace.
        monkeypatch: Fixture replacing only the parent transport.
    """
    archive = io.BytesIO()
    name = "skynet_package_fixture"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(f"{name}/__init__.py", "VALUE = 42\n")
        wheel.writestr(f"{name}-1.2.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.2\n")
        wheel.writestr(f"{name}-1.2.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        wheel.writestr(f"{name}-1.2.dist-info/RECORD", "")
    data = archive.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    artifact = {
        "filename": f"{name}-1.2-py3-none-any.whl",
        "sha256": digest,
        "url": f"https://registry.example/{name}.whl",
        "requires_python": ">=3.11",
    }

    def request(route: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Emulate the scoped parent registry protocol.

        Args:
            route: Opaque test capability.
            body: Requested package operation.

        Returns:
            Fixture index metadata or exact wheel bytes.
        """
        if body["action"] == "index":
            assert body["project"].replace("-", "_") == name
            return {"artifacts": [artifact]}
        assert body["sha256"] == digest
        return {"data": base64.b64encode(data[body["offset"] :]).decode(), "size": len(data)}

    monkeypatch.setattr(package_setup, "_request", request)
    code = f"import {name}\ndef score(candidate): return {name}.VALUE"
    lock = package_setup.resolve(code, [], {}, tmp_path)
    assert lock["artifacts"][0]["version"] == "1.2"
    assert lock["artifacts"][0]["sha256"] == digest
    assert lock["artifacts"][0]["url"] == artifact["url"]
    package_setup.install(lock, {}, tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", f"import {name}; print({name}.VALUE)"],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "site")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "42"
    assert "--hash=sha256:" + digest in (tmp_path / "locked.txt").read_text()


def _wheel(name: str, version: str, requires: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Build a pure-Python fixture wheel and the index entry advertising it.

    Args:
        name: Distribution and import name.
        version: Distribution version.
        requires: Optional ``Requires-Dist`` line.

    Returns:
        The wheel bytes and its index artifact.
    """
    archive = io.BytesIO()
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if requires:
        metadata += f"Requires-Dist: {requires}\n"
    with zipfile.ZipFile(archive, "w") as wheel:
        wheel.writestr(f"{name}/__init__.py", f"VERSION = {version!r}\n")
        wheel.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
        wheel.writestr(
            f"{name}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        wheel.writestr(f"{name}-{version}.dist-info/RECORD", "")
    data = archive.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    artifact = {
        "filename": f"{name}-{version}-py3-none-any.whl",
        "sha256": digest,
        "url": f"https://registry.example/{name}-{version}.whl",
        "requires_python": ">=3.11",
    }
    return data, artifact


def test_extend_installs_only_what_a_later_candidate_adds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate's new import installs its wheel while shared dependencies keep the locked version.

    Args:
        tmp_path: Isolated guest-like workspace.
        monkeypatch: Fixture replacing only the parent transport.
    """
    base, extra = "skynet_base_fixture", "skynet_extra_fixture"
    wheels = {
        name: _wheel(*spec)
        for name, spec in {
            "base-1.0": (base, "1.0"),
            "base-2.0": (base, "2.0"),
            "extra-1.0": (extra, "1.0", f"{base}>=1.0"),
        }.items()
    }
    by_digest = {artifact["sha256"]: data for data, artifact in wheels.values()}
    index = {
        base: [wheels["base-1.0"][1], wheels["base-2.0"][1]],
        extra: [wheels["extra-1.0"][1]],
    }

    def request(route: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Emulate the scoped parent registry protocol.

        Args:
            route: Opaque test capability.
            body: Requested package operation.

        Returns:
            Fixture index metadata or exact wheel bytes.
        """
        if body["action"] == "index":
            return {"artifacts": index[body["project"].replace("-", "_")]}
        data = by_digest[body["sha256"]]
        return {"data": base64.b64encode(data[body["offset"] :]).decode(), "size": len(data)}

    monkeypatch.setattr(package_setup, "_request", request)
    lock = package_setup.resolve(f"import {base}\ndef score(c): return 1", [f"{base}==1.0"], {}, tmp_path)
    assert [(a["name"], a["version"]) for a in lock["artifacts"]] == [(base.replace("_", "-"), "1.0")]
    package_setup.install(lock, {}, tmp_path)

    added = package_setup.extend(lock, f"import json\nimport {base}\nimport {extra}\n", {}, tmp_path)

    assert added["imports"] == ["json", extra]
    assert [(a["name"], a["version"]) for a in added["artifacts"]] == [(extra.replace("_", "-"), "1.0")]
    installed = (tmp_path / "locked.txt").read_text()
    assert extra.replace("_", "-") in installed
    assert base not in installed
    result = subprocess.run(
        [sys.executable, "-c", f"import {base}, {extra}; print({base}.VERSION, {extra}.VERSION)"],
        env={**os.environ, "PYTHONPATH": str(tmp_path / "site")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["1.0", "1.0"]
    assert package_setup.extend(lock, f"import {extra}\n", {}, tmp_path) == {"imports": [extra], "artifacts": []}


def test_inference_ignores_strings_stdlib_and_injected_helpers() -> None:
    """Infer executable imports while leaving image-provided libraries untouched."""
    requirements, imports = package_setup.infer_requirements(
        'import json\nfrom openascent import llm\ntext="import absent"', []
    )
    assert requirements == []
    assert imports == ["json", "openascent"]
    with pytest.raises(ValueError, match="direct URLs"):
        package_setup.infer_requirements("import json", ["package @ https://elsewhere.invalid/pkg.whl"])


def test_inference_folds_seed_candidate_imports() -> None:
    """Fold a seed candidate's third-party imports into the scorer's requirements."""
    requirements, imports = package_setup.infer_requirements(
        "import json\ndef score(candidate): ...\n",
        [],
        candidate="import skynet_absent_candidate_pkg\nimport json",
    )
    assert requirements == ["skynet_absent_candidate_pkg"]
    assert imports == ["json", "skynet_absent_candidate_pkg"]


def test_inference_scans_dict_candidate_and_ignores_non_python() -> None:
    """Scan every named candidate part while ignoring parts that are not Python code."""
    requirements, imports = package_setup.infer_requirements(
        "def score(candidate): ...\n",
        [],
        candidate={
            "solver": "import skynet_absent_candidate_pkg",
            "prompt": "You are a helpful assistant. Respond in JSON.",
        },
    )
    assert requirements == ["skynet_absent_candidate_pkg"]
    assert imports == ["skynet_absent_candidate_pkg"]


def test_overrides_bypass_seed_candidate_scan() -> None:
    """Honor explicit overrides verbatim without folding in candidate imports."""
    requirements, imports = package_setup.infer_requirements(
        "def score(candidate): ...\n",
        ["explicit-package==1.0"],
        candidate="import skynet_absent_candidate_pkg",
    )
    assert requirements == ["explicit-package==1.0"]
    assert imports == ["skynet_absent_candidate_pkg"]


def test_resolution_preserves_budget_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop resolution with the parent's budget signal instead of a missing-package error.

    Args:
        tmp_path: Isolated resolution workspace.
        monkeypatch: Fixture injecting a denied parent operation.
    """

    def request(route: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        """Reject the registry operation at the budget boundary.

        Args:
            route: Package-only capability.
            body: Attempted registry operation.

        Raises:
            PackageSetupStoppedError: The owning budget is exhausted.
        """
        raise package_setup.PackageSetupStoppedError("budget_reached", "No remaining budget")

    monkeypatch.setattr(package_setup, "_request", request)
    with pytest.raises(package_setup.PackageSetupStoppedError, match="No remaining budget"):
        package_setup.resolve("import skynet_absent_fixture", [], {}, tmp_path)


def test_setup_bootstraps_without_installed_packaging(tmp_path: Path) -> None:
    """Run the uploaded setup script in a pip-only sandbox environment.

    Args:
        tmp_path: Isolated environment and uploaded request directory.
    """
    environment = tmp_path / "guest"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    subprocess.run(
        [str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('packaging') is None"],
        capture_output=True,
        text=True,
        check=True,
    )
    setup = tmp_path / "setup.py"
    setup.write_text(Path(package_setup.__file__).read_text())
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"action": "resolve", "code": "import json", "requirements": [], "route": {}}))
    completed = subprocess.run(
        [str(python), "-I", str(setup), str(request)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["ok"] is True
    assert result["result"]["artifacts"] == []
    assert result["result"]["imports"] == ["json"]

    request.write_text(
        json.dumps(
            {
                "action": "resolve",
                "code": "import json",
                "requirements": ["package @ https://elsewhere.invalid/pkg.whl"],
                "route": {},
            }
        )
    )
    subprocess.run(
        [str(python), "-I", str(setup), str(request)], capture_output=True, text=True, timeout=30, check=True
    )
    rejected = json.loads((tmp_path / "result.json").read_text())
    assert rejected["ok"] is False
    assert "direct URLs" in rejected["error"]
