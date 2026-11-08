"""Tests for the harness catalog: per-harness launches, overrides and output parsers."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import (
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_CUSTOM,
    BLACKBOX_HARNESS_OPENCODE,
    BLACKBOX_HARNESS_PI,
    BLACKBOX_HARNESS_PRIME,
    BLACKBOX_TARGET_AGENT,
    BlackboxTarget,
)

from ..harness import (
    ANSWER_FILE,
    PRIME_AGENT_IMAGE_KERNEL,
    PRIME_AGENT_TARBALL,
    PRIME_AGENT_VERSION,
    PROMPT_FILE,
    GatewayConfig,
    build_launch,
    launch_payload,
    pi_editor_launch,
)
from ..harness_bridge import (
    parse_codex_output,
    parse_opencode_output,
    parse_pi_output,
    parse_plain_output,
)
from ..harnesses import REGISTRY

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")


def _target(harness: str, **overrides: object) -> BlackboxTarget:
    """Build an agent target for ``harness`` with the given field overrides.

    Args:
        harness: Harness id.
        **overrides: Extra target fields.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=harness, model="target-model", **overrides)


def test_pi_launch_wires_the_gateway_provider_and_json_stream() -> None:
    """Pi writes AGENTS.md, a models.json pointed at the gateway, and runs in JSON mode."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PI), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    models = json.loads(launch.files[".app/pi/models.json"])
    provider = models["providers"]["app"]
    assert provider["baseUrl"] == _GATEWAY.url
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "!printenv APP_API_KEY"
    assert provider["models"][0]["id"] == "target-model"
    assert 'cp .app/pi/models.json "$HOME/.pi/agent/models.json"' in launch.run_command
    assert "pi --mode json --no-session --provider app --model" in launch.run_command
    assert launch.parse_output is parse_pi_output


def test_pi_launch_bounds_reasoning_behind_openrouter() -> None:
    """Behind OpenRouter the model is declared reasoning-capable and every call carries an explicit low effort."""
    gateway = GatewayConfig(url="https://openrouter.ai/api/v1", api_key="secret-key")

    launch = build_launch(_target(BLACKBOX_HARNESS_PI), gateway)

    entry = json.loads(launch.files[".app/pi/models.json"])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["compat"] == {"thinkingFormat": "openrouter"}
    assert '--model "$APP_MODEL:low"' in launch.run_command


def test_pi_launch_leaves_reasoning_alone_behind_other_gateways() -> None:
    """A generic OpenAI-compatible gateway may reject OpenRouter's reasoning field, so pi never sends it."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PI), _GATEWAY)

    entry = json.loads(launch.files[".app/pi/models.json"])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is False
    assert "compat" not in entry
    assert '--model "$APP_MODEL" ' in launch.run_command


def test_pi_launch_pins_the_harness_version_ahead_of_the_image_copy() -> None:
    """The install pins pi, prefers the pinned copy on PATH and logs the version that runs."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PI), _GATEWAY)

    assert launch.install_command is not None
    assert "@earendil-works/pi-coding-agent@0.87.1" in launch.install_command
    assert '[ "$(pi --version 2>/dev/null)" = "0.87.1" ]' in launch.install_command
    assert launch.install_command.endswith("&& pi --version")
    for command in (launch.install_command, launch.run_command):
        assert command.startswith('export PATH="$HOME/.app/pi/bin:$PATH";')


def test_build_launch_drops_the_openrouter_routing_prefix() -> None:
    """The gateway fronts OpenRouter itself, so the model id it sees has no ``openrouter/`` prefix."""
    target = BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_PI, model="openrouter/acme/agent-1")

    launch = build_launch(target, _GATEWAY)

    models = json.loads(launch.files[".app/pi/models.json"])
    assert models["providers"]["app"]["models"][0]["id"] == "acme/agent-1"
    assert launch.env["APP_MODEL"] == "acme/agent-1"


def test_pi_launch_carries_the_base_environment() -> None:
    """Every APP_* var, its legacy SKYNET_* twin, and the CI/NO_COLOR pair reach the sandbox."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PI), _GATEWAY)

    app_env = {
        "APP_MODEL": "target-model",
        "APP_GATEWAY_URL": _GATEWAY.url,
        "APP_API_KEY": "secret-key",
        "APP_PROMPT_FILE": PROMPT_FILE,
        "APP_ANSWER_FILE": ANSWER_FILE,
    }
    legacy_env = {"SKYNET_" + name.removeprefix("APP_"): value for name, value in app_env.items()}
    assert launch.env == {**app_env, **legacy_env, "CI": "1", "NO_COLOR": "1"}


def test_codex_launch_writes_a_gateway_config_toml() -> None:
    """Codex routes through the gateway via CODEX_HOME config and the chat wire API."""
    launch = build_launch(_target(BLACKBOX_HARNESS_CODEX), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    config = launch.files[".app/codex/config.toml"]
    assert 'env_key = "APP_API_KEY"' in config
    assert 'wire_api = "responses"' in config
    assert "request_max_retries = 0" in config
    assert "stream_max_retries = 0" in config
    assert json.dumps(_GATEWAY.url) in config
    assert 'CODEX_HOME="$PWD/.app/codex" codex exec' in launch.run_command
    assert launch.parse_output is parse_codex_output


def test_opencode_launch_names_the_gateway_model() -> None:
    """OpenCode writes opencode.json and runs the skynet/<model> selection."""
    launch = build_launch(_target(BLACKBOX_HARNESS_OPENCODE), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    config = json.loads(launch.files["opencode.json"])
    assert config["model"] == "app/target-model"
    assert config["provider"]["app"]["options"]["apiKey"] == "{env:APP_API_KEY}"
    assert 'opencode run --format json --model "app/$APP_MODEL"' in launch.run_command
    assert launch.parse_output is parse_opencode_output


@pytest.mark.parametrize(("effort", "expected"), [("high", "high"), ("none", "none"), ("max", "max")])
def test_codex_launch_sets_the_chosen_reasoning_effort(effort: str, expected: str) -> None:
    """Codex reads the effort from its config unchanged; Codex 0.153 knows every Skynet level.

    Args:
        effort: The optimization model's effort level.
        expected: The level Codex receives.
    """
    launch = build_launch(_target(BLACKBOX_HARNESS_CODEX), _GATEWAY, reasoning_effort=effort)

    config = tomllib.loads(launch.files[".app/codex/config.toml"])
    assert config["model_reasoning_effort"] == expected
    assert config["model_providers"]["app"]["env_key"] == "APP_API_KEY"


def test_codex_launch_keeps_its_default_effort_without_a_choice() -> None:
    """With no effort chosen the config names none, so Codex keeps its own default."""
    launch = build_launch(_target(BLACKBOX_HARNESS_CODEX), _GATEWAY)

    assert "model_reasoning_effort" not in tomllib.loads(launch.files[".app/codex/config.toml"])


@pytest.mark.parametrize(
    ("harness", "models_file"),
    [(BLACKBOX_HARNESS_PI, ".app/pi/models.json"), (BLACKBOX_HARNESS_PRIME, ".app/prime/models.json")],
)
@pytest.mark.parametrize(
    ("url", "effort", "suffix"),
    [
        ("https://gw.example/v1", "high", ":high"),
        ("https://gw.example/v1", "none", ":off"),
        ("https://gw.example/v1", "xhigh", ":xhigh"),
        ("https://gw.example/v1", "max", ":max"),
        ("https://openrouter.ai/api/v1", "medium", ":medium"),
        ("https://openrouter.ai/api/v1", "none", ":off"),
    ],
)
def test_pi_family_launch_selects_the_chosen_thinking_level(
    harness: str, models_file: str, url: str, effort: str, suffix: str
) -> None:
    """A chosen effort marks the model reasoning-capable, replaces the default level and maps it to the wire value.

    Args:
        harness: Pi or Prime Agent.
        models_file: Where the harness reads its models.json.
        url: Gateway URL.
        effort: The optimization model's effort level.
        suffix: The thinking suffix the model argument carries.
    """
    launch = build_launch(_target(harness), GatewayConfig(url=url, api_key="k"), reasoning_effort=effort)

    entry = json.loads(launch.files[models_file])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["thinkingLevelMap"] == {suffix[1:]: effort}
    assert f'--model "$APP_MODEL{suffix}"' in launch.run_command


def test_opencode_launch_sets_the_chosen_reasoning_effort() -> None:
    """OpenCode's gateway model carries the effort as a model option; the command is unchanged."""
    default = build_launch(_target(BLACKBOX_HARNESS_OPENCODE), _GATEWAY)
    chosen = build_launch(_target(BLACKBOX_HARNESS_OPENCODE), _GATEWAY, reasoning_effort="high")

    model = json.loads(chosen.files["opencode.json"])["provider"]["app"]["models"]["target-model"]
    assert model == {"name": "target-model", "options": {"reasoningEffort": "high"}}
    assert chosen.run_command == default.run_command


def test_opencode_launch_keeps_its_default_effort_without_a_choice() -> None:
    """With no effort chosen the OpenCode model entry names only the model."""
    launch = build_launch(_target(BLACKBOX_HARNESS_OPENCODE), _GATEWAY)

    model = json.loads(launch.files["opencode.json"])["provider"]["app"]["models"]["target-model"]
    assert model == {"name": "target-model"}


def test_custom_launch_exposes_the_chosen_effort_only_as_an_environment_variable() -> None:
    """A custom harness reads the effort from its environment; the command text never carries it."""
    target = _target(BLACKBOX_HARNESS_CUSTOM, run_command="agent {model}")

    chosen = build_launch(target, _GATEWAY, reasoning_effort="high")
    default = build_launch(target, _GATEWAY)

    assert chosen.env["APP_REASONING_EFFORT"] == chosen.env["SKYNET_REASONING_EFFORT"] == "high"
    assert chosen.run_command == default.run_command == "agent target-model"
    assert "APP_REASONING_EFFORT" not in default.env
    assert "SKYNET_REASONING_EFFORT" not in default.env


@pytest.mark.parametrize(("effort", "suffix"), [("high", ":high"), ("none", ":off")])
def test_pi_editor_launch_selects_the_chosen_thinking_level(effort: str, suffix: str) -> None:
    """ShinkaEvolve's Pi editor applies the effort the way Pi's catalog launch does.

    Args:
        effort: The optimization model's effort level.
        suffix: The thinking suffix the model argument carries.
    """
    launch = pi_editor_launch("m", _GATEWAY, reasoning_effort=effort)

    entry = json.loads(launch.files[".app/pi/models.json"])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["thinkingLevelMap"] == {suffix[1:]: effort}
    assert f'--model "$APP_MODEL{suffix}"' in launch.run_command


def test_pi_editor_launch_keeps_the_default_without_a_choice() -> None:
    """With no effort chosen the Pi editor launch is the one it was before efforts existed."""
    launch = pi_editor_launch("m", _GATEWAY)

    entry = json.loads(launch.files[".app/pi/models.json"])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is False
    assert "thinkingLevelMap" not in entry
    assert '--model "$APP_MODEL"' in launch.run_command


@pytest.mark.parametrize("harness", list(REGISTRY))
def test_protected_harness_uses_offline_dependencies_and_local_relay(tmp_path: Path, harness: str) -> None:
    """Execute generated config substitution without invoking a CLI or contacting a provider."""
    launch = build_launch(_target(harness, run_command="true"), _GATEWAY, protected=True)
    assert "npm install" not in launch.install_command
    assert "--version" in launch.install_command
    for name, content in launch.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        launch.run_command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.defpath, "APP_BUDGET_RELAY_URL": "http://127.0.0.1:3210/v1"},
        timeout=10,
        check=True,
    )
    for name in launch.files:
        content = (tmp_path / name).read_text()
        assert _GATEWAY.url not in content
        assert "http://127.0.0.1:3210/v1" in content


def test_prime_agent_launch_reuses_the_pi_gateway_config_in_its_own_agent_dir() -> None:
    """Prime Agent gets Pi's models.json under .app/prime, is pointed there, and streams Pi-shaped JSON."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PRIME), _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    models = json.loads(launch.files[".app/prime/models.json"])
    provider = models["providers"]["app"]
    assert provider["baseUrl"] == _GATEWAY.url
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "!printenv APP_API_KEY"
    assert provider["models"][0]["id"] == "target-model"
    assert launch.run_command.startswith('PRIME_AGENT_CODING_AGENT_DIR="$PWD/.app/prime" ')
    assert " prime-agent --mode json" in launch.run_command
    assert '--no-session --provider app --model "$APP_MODEL"' in launch.run_command
    assert launch.parse_output is parse_pi_output


def test_prime_agent_launch_pins_the_release_tarball_and_bootstraps_the_kernel() -> None:
    """The install pins the CDN tarball, builds the Python kernel up front, and never waits on a prompt."""
    launch = build_launch(_target(BLACKBOX_HARNESS_PRIME), _GATEWAY)

    assert launch.install_command is not None
    assert f'"$(prime-agent --version 2>/dev/null)" = "{PRIME_AGENT_VERSION}"' in launch.install_command
    assert "PRIME_AGENT_BOOTSTRAP_KERNEL_ON_INSTALL=1 npm install -g" in launch.install_command
    assert f'"{PRIME_AGENT_TARBALL}"' in launch.install_command
    assert launch.env["PRIME_AGENT_INSTALL_UV"] == "1"
    assert launch.env["PRIME_AGENT_TELEMETRY"] == "0"
    assert launch.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert launch.env["APP_MODEL"] == "target-model"


@pytest.mark.parametrize("image_kernel", [True, False])
def test_prime_agent_launch_uses_the_image_kernel_only_when_present(tmp_path: Path, image_kernel: bool) -> None:
    """The prebuilt kernel is selected when the image has it; otherwise the override is empty so Prime Agent bootstraps."""
    kernel = tmp_path / "python"
    if image_kernel:
        kernel.write_text("")
        kernel.chmod(0o755)
    launch = build_launch(_target(BLACKBOX_HARNESS_PRIME), _GATEWAY)
    command = launch.run_command.replace(PRIME_AGENT_IMAGE_KERNEL, str(kernel)).replace(
        "prime-agent --mode json", "printenv PRIME_AGENT_KERNEL_PYTHON #"
    )

    selected = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True).stdout.strip()

    assert selected == (str(kernel) if image_kernel else "")


def test_prime_agent_launch_bounds_reasoning_behind_openrouter() -> None:
    """Behind OpenRouter the Prime Agent entry carries Pi's thinking format and effort, like Pi's does."""
    gateway = GatewayConfig(url="https://openrouter.ai/api/v1", api_key="k")
    launch = build_launch(_target(BLACKBOX_HARNESS_PRIME), gateway)

    entry = json.loads(launch.files[".app/prime/models.json"])["providers"]["app"]["models"][0]
    assert entry["reasoning"] is True
    assert entry["compat"] == {"thinkingFormat": "openrouter"}
    assert '--model "$APP_MODEL:low"' in launch.run_command


def test_custom_launch_fills_placeholders() -> None:
    """A custom harness substitutes the model/gateway/file placeholders into its commands."""
    target = _target(
        BLACKBOX_HARNESS_CUSTOM,
        install_command="setup {model}",
        run_command="agent {model} {gateway_url} {api_key} {prompt_file} {answer_file}",
    )

    launch = build_launch(target, _GATEWAY)

    assert launch.instructions_file == "AGENTS.md"
    assert launch.install_command == "setup target-model"
    assert launch.run_command == f"agent target-model {_GATEWAY.url} secret-key {PROMPT_FILE} {ANSWER_FILE}"
    assert launch.parse_output is parse_plain_output


def test_catalog_overrides_replace_the_default_commands() -> None:
    """Target install/run commands override the catalog defaults but keep its files and parser."""
    target = _target(BLACKBOX_HARNESS_PI, install_command="my-install {model}", run_command="my-run {prompt_file}")

    launch = build_launch(target, _GATEWAY)

    assert launch.install_command == "my-install target-model"
    assert launch.run_command == f"my-run {PROMPT_FILE}"
    assert ".app/pi/models.json" in launch.files
    assert launch.parse_output is parse_pi_output


def test_protected_custom_gateway_placeholder_uses_guest_endpoint() -> None:
    """Keep the parent-only origin out of custom commands after mailbox isolation."""
    target = _target(BLACKBOX_HARNESS_CUSTOM, run_command='printf "%s" "{gateway_url}"')
    launch = build_launch(target, _GATEWAY, protected=True)
    result = subprocess.run(
        launch.run_command,
        shell=True,
        env={"PATH": os.defpath, "APP_GATEWAY_URL": "http://127.0.0.1:3210/v1"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout == "http://127.0.0.1:3210/v1"
    assert _GATEWAY.url not in launch.run_command


def test_protected_opencode_disables_startup_dependency_downloads() -> None:
    """Use the bundled provider without network-fetched metadata, plugins, or language servers."""
    launch = build_launch(_target(BLACKBOX_HARNESS_OPENCODE), _GATEWAY, protected=True)
    for flag in ("AUTOUPDATE", "MODELS_FETCH", "DEFAULT_PLUGINS", "LSP_DOWNLOAD"):
        assert launch.env[f"OPENCODE_DISABLE_{flag}"] == "true"


def test_custom_without_run_command_is_a_typed_error() -> None:
    """A custom harness missing its run command fails with a service error."""
    target = BlackboxTarget.model_construct(
        kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_CUSTOM, model="m", run_command=None, install_command=None
    )

    with pytest.raises(ServiceError, match="custom harness needs a run_command"):
        build_launch(target, _GATEWAY)


def test_unknown_harness_is_a_typed_error() -> None:
    """An unrecognized harness id is rejected."""
    target = BlackboxTarget.model_construct(kind=BLACKBOX_TARGET_AGENT, harness="ferrari", model="m")

    with pytest.raises(ServiceError, match="Unknown harness 'ferrari'"):
        build_launch(target, _GATEWAY)


def test_parse_plain_output_strips_and_reports_none_when_empty() -> None:
    """Plain output is the stripped text, or None when blank, with no usage."""
    assert parse_plain_output("  hello \n") == ("hello", {})
    assert parse_plain_output("   ") == (None, {})


def test_parse_pi_output_takes_the_last_assistant_message_and_sums_usage() -> None:
    """Pi parsing keeps the final assistant text, joins its parts, and sums usage across turns."""
    stdout = "\n".join(
        [
            "not json",
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first"}],
                        "usage": {"input": 10, "output": 5},
                    },
                }
            ),
            json.dumps({"type": "message_end", "message": {"role": "user", "content": []}}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "second"}, {"type": "text", "text": "line"}],
                        "usage": {"input": 3, "output": 7},
                    },
                }
            ),
        ]
    )

    text, usage = parse_pi_output(stdout)

    assert text == "second\nline"
    assert usage == {"input_tokens": 13, "output_tokens": 12}


def test_parse_codex_output_reads_agent_message_and_turn_usage() -> None:
    """Codex parsing keeps the final agent message and the turn's token usage."""
    stdout = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "the answer"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 4}}),
        ]
    )

    assert parse_codex_output(stdout) == ("the answer", {"input_tokens": 8, "output_tokens": 4})


def test_parse_opencode_output_reads_text_parts_and_step_usage() -> None:
    """The last completed text part is the answer; every step_finish adds its tokens."""
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "read"}}),
            json.dumps({"type": "step_finish", "part": {"tokens": {"input": 5, "output": 2, "reasoning": 0}}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "draft"}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": " the answer \n"}}),
            json.dumps({"type": "step_finish", "part": {"tokens": {"input": 3, "output": 2}}}),
            "not json",
        ]
    )
    assert parse_opencode_output(stdout) == ("the answer", {"input_tokens": 8, "output_tokens": 4})
    assert parse_opencode_output("") == (None, {})


@pytest.mark.parametrize(
    ("harness", "output_format"),
    [(spec.id, spec.output_format) for spec in REGISTRY.values()],
)
def test_launch_payload_names_the_parser_for_every_built_in_harness(harness: str, output_format: str) -> None:
    """Serialize a launch so the sandbox bridge can pick the right output parser by name."""
    payload = launch_payload(build_launch(_target(harness), _GATEWAY))
    assert payload["output_format"] == output_format
    assert payload["run_command"]
    assert payload["instructions_file"]
    assert set(payload) == {"instructions_file", "run_command", "install_command", "files", "env", "output_format"}
