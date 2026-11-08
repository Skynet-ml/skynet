"""Skynet CI checks: the ci.yml and i18n.yml jobs as Dagger checks.

Each check runs the same commands as its GitHub Actions job. GitHub Actions
calls ``dagger check <name>``; developers run the same command locally.
"""

from __future__ import annotations

from dagger import check, function, object_type
from dagger_clients.core import Container, Directory, File, Workspace, core

PYTHON_IMAGE = "ghcr.io/astral-sh/uv:0.9.13-python3.12-bookworm-slim"
NODE_IMAGE = "node:22-bookworm-slim"
POSTGRES_IMAGE = "postgres:16-alpine"
CORE_SHARD_COUNT = 6

SOURCE_EXCLUDE = [
    "**/.git",
    "**/node_modules",
    "**/.venv",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.ruff_cache",
    "**/.next",
    "**/dist",
    "**/out",
    "**/*.tsbuildinfo",
    ".dagger",
    ".claude",
    # Local secrets must not reach the engine. A test reads backend/.env.example.
    "**/.env",
    "**/.env.*",
    "!**/.env.example",
    "**/*.pem",
]


@object_type
class SkynetCi:
    source: Directory

    @classmethod
    def create(cls, ws: Workspace) -> SkynetCi:
        """Load the repository without dependency folders and build outputs.

        Args:
            ws: The Dagger workspace (the repository root).

        Returns:
            The module object that holds the filtered source.
        """
        return cls(source=ws.directory("/", exclude=SOURCE_EXCLUDE))

    def _backend(self) -> Container:
        """Return a Python 3.12 container with the backend synced by uv.

        The lock files go in first, so a source-only change reuses the
        dependency layer.
        """
        backend = self.source.directory("backend")
        return (
            core()
            .container()
            .from_(PYTHON_IMAGE)
            .with_mounted_cache("/var/cache/apt", core().cache_volume("skynet-apt"))
            .with_exec(["rm", "-f", "/etc/apt/apt.conf.d/docker-clean"])
            .with_exec(
                [
                    "sh",
                    "-c",
                    "apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl git jq procps",
                ]
            )
            # The GitHub runner has curl and /usr/bin/python3. Some core tests
            # call them with PATH=os.defpath (/bin:/usr/bin), so the image
            # needs both at the same place.
            .with_exec(
                [
                    "sh",
                    "-c",
                    'test -e /usr/bin/python3 || ln -s "$(command -v python3)" /usr/bin/python3',
                ]
            )
            # The hidden-path guard tests in test_harness_bridge.py run the Pi
            # and OpenCode guard scripts with node, and skip without it.
            .with_file(
                "/usr/local/bin/node",
                core().container().from_(NODE_IMAGE).file("/usr/local/bin/node"),
            )
            .with_mounted_cache("/root/.cache/uv", core().cache_volume("skynet-uv"))
            .with_env_variable("UV_LINK_MODE", "copy")
            .with_env_variable("UV_PYTHON_DOWNLOADS", "never")
            .with_env_variable("UV_PROJECT_ENVIRONMENT", "/opt/venv")
            .with_workdir("/src/backend")
            .with_file("/src/backend/pyproject.toml", backend.file("pyproject.toml"))
            .with_file("/src/backend/uv.lock", backend.file("uv.lock"))
            .with_exec(
                ["uv", "sync", "--extra", "dev", "--frozen", "--no-install-project"]
            )
            .with_directory("/src", self.source)
            .with_exec(["uv", "sync", "--extra", "dev", "--frozen"])
        )

    def _frontend(self) -> Container:
        """Return a Node 22 container with the frontend installed by npm ci."""
        frontend = self.source.directory("frontend")
        return (
            core()
            .container()
            .from_(NODE_IMAGE)
            .with_mounted_cache("/root/.npm", core().cache_volume("skynet-npm"))
            .with_workdir("/src/frontend")
            .with_file("/src/frontend/package.json", frontend.file("package.json"))
            .with_file(
                "/src/frontend/package-lock.json", frontend.file("package-lock.json")
            )
            .with_exec(["npm", "ci", "--no-audit", "--no-fund"])
            .with_directory("/src", self.source)
        )

    def _core_shard(self, shard: int) -> Container:
        """Run one shard of the credential-free core suite.

        Args:
            shard: The zero-based shard index.

        Returns:
            The container after pytest ran, with the dispatched nodeid list.
        """
        return (
            self._backend()
            .with_env_variable("SKYNET_TEST_SHARD", f"{shard}/{CORE_SHARD_COUNT}")
            .with_env_variable("SKYNET_TEST_DISPATCHED_OUT", f"dispatched-{shard}.txt")
            .with_exec(["uv", "run", "pytest", "core/", "-q", "-rs", "-n", "auto"])
        )

    @function
    def dispatched(self, shard: int) -> File:
        """Return the nodeids that one core shard dispatched.

        CI exports this file after ``dagger check`` ran the same shard, so the
        engine serves it from cache.

        Args:
            shard: The zero-based shard index.

        Returns:
            The ``dispatched-<shard>.txt`` file.
        """
        return self._core_shard(shard).file(f"/src/backend/dispatched-{shard}.txt")

    @function
    async def summary(self, shard: int) -> str:
        """Return the pytest output of one core shard, with the skip reasons.

        CI prints this after ``dagger check`` ran the same shard, so the
        engine serves it from cache and the log shows the pass and skip counts.

        Args:
            shard: The zero-based shard index.

        Returns:
            The pytest stdout of the shard.
        """
        return await self._core_shard(shard).stdout()

    @function
    @check
    async def backend_core_0(self) -> None:
        """Run core test shard 0 of 6."""
        await self._core_shard(0).sync()

    @function
    @check
    async def backend_core_1(self) -> None:
        """Run core test shard 1 of 6."""
        await self._core_shard(1).sync()

    @function
    @check
    async def backend_core_2(self) -> None:
        """Run core test shard 2 of 6."""
        await self._core_shard(2).sync()

    @function
    @check
    async def backend_core_3(self) -> None:
        """Run core test shard 3 of 6."""
        await self._core_shard(3).sync()

    @function
    @check
    async def backend_core_4(self) -> None:
        """Run core test shard 4 of 6."""
        await self._core_shard(4).sync()

    @function
    @check
    async def backend_core_5(self) -> None:
        """Run core test shard 5 of 6."""
        await self._core_shard(5).sync()

    @function
    @check
    async def backend_gates(self) -> None:
        """Run ruff, the terraform script tests, requirements sync, Postgres and SLO gates."""
        postgres = (
            core()
            .container()
            .from_(POSTGRES_IMAGE)
            .with_env_variable("POSTGRES_USER", "budget_test")
            .with_env_variable("POSTGRES_PASSWORD", "budget_test")
            .with_env_variable("POSTGRES_DB", "budget_test")
            .with_exposed_port(5432)
            .as_service(use_entrypoint=True)
        )
        # requirements.txt is the pip install path's copy of uv.lock. When only
        # one of the two moves, the pip-path image ships a dependency set that
        # CI never tested (starlette and stripe both drifted that way). Compare
        # pins only, so the hand-written header block is free to differ.
        requirements_sync = (
            "uv export --no-emit-project --no-hashes --no-header -o /tmp/req-expected.txt >/dev/null\n"
            "if ! diff -u <(grep -vE '^\\s*#|^\\s*$' requirements.txt) "
            "<(grep -vE '^\\s*#|^\\s*$' /tmp/req-expected.txt); then\n"
            "  echo 'requirements.txt is out of sync with uv.lock. Regenerate: uv export"
            " --no-emit-project --no-hashes --no-header -o requirements.txt"
            " (then restore the header block)'\n"
            "  exit 1\n"
            "fi\n"
        )
        await (
            self._backend()
            .with_exec(["uv", "run", "ruff", "check", "."])
            .with_exec(["bash", "../infra/terraform/scripts/tests/test-image-key.sh"])
            .with_exec(
                ["bash", "../infra/terraform/scripts/tests/test-lane-failures.sh"]
            )
            .with_exec(["bash", "-c", requirements_sync])
            .with_service_binding("postgres", postgres)
            .with_env_variable(
                "SKYNET_BUDGET_TEST_DB_URL",
                "postgresql+psycopg2://budget_test:budget_test@postgres:5432/budget_test",
            )
            .with_exec(
                [
                    "uv",
                    "run",
                    "pytest",
                    "core/billing/tests/test_budgets_postgres.py",
                    "core/api/tests/test_run_sync_postgres.py",
                    "-q",
                ]
            )
            .with_exec(
                ["uv", "run", "pytest", "tests/unit/test_load_test_slo.py", "-q"]
            )
            .sync()
        )

    def _collected(self) -> Container:
        """Collect the full core suite without a shard filter."""
        return (
            self._backend()
            .with_env_variable("SKYNET_TEST_COLLECTED_OUT", "collected.txt")
            .with_exec(
                [
                    "bash",
                    "-c",
                    "unset APP_TEST_SHARD SKYNET_TEST_SHARD\n"
                    "uv run pytest core/ --collect-only -q\n"
                    "test -s collected.txt\n"
                    'python3 -c \'from pathlib import Path; print("collected",'
                    ' len(Path("collected.txt").read_text().splitlines()), "core test nodeids")\'',
                ]
            )
        )

    @function
    def collected(self) -> File:
        """Return the full core collection that the shard coverage gate compares against."""
        return self._collected().file("/src/backend/collected.txt")

    @function
    @check
    async def backend_shard_coverage(self) -> None:
        """Collect the full core suite for the shard coverage gate."""
        await self._collected().sync()

    @function
    @check
    async def frontend_typecheck(self) -> None:
        """Run the frontend TypeScript check."""
        await self._frontend().with_exec(["npm", "run", "typecheck"]).sync()

    @function
    @check
    async def frontend_lint(self) -> None:
        """Run ESLint and the module-scope i18n check."""
        await self._frontend().with_exec(["npm", "run", "lint"]).sync()

    @function
    @check
    async def frontend_unit(self) -> None:
        """Run the frontend unit tests."""
        await self._frontend().with_exec(["npm", "run", "test:unit"]).sync()

    @function
    @check
    async def frontend_build(self) -> None:
        """Run the Next.js production build."""
        await self._frontend().with_exec(["npm", "run", "build"]).sync()

    @function
    @check
    async def locale_drift(self) -> None:
        """Verify that the generated i18n artefacts match the catalog."""
        await (
            core()
            .container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", core().cache_volume("skynet-pip"))
            .with_exec(["pip", "install", "--quiet", "jsonschema"])
            .with_workdir("/src")
            .with_directory("/src", self.source)
            .with_exec(["python", "scripts/generate_i18n.py", "--check"])
            .sync()
        )
