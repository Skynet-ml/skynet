"""Tests for metric model discovery and pre-protection resolution."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.billing.byok_bridge import inject_byok_connections
from core.billing.byok_vault import _PROVIDER_PROBES
from core.config import settings
from core.i18n_en import ENGLISH_MESSAGES
from core.models.unified import UnifiedModels
from core.provider_registry import BYOK_TO_LITELLM_PROVIDER
from core.service_gateway.optimization.unified.metric_model import (
    resolve_metric_model,
    resolve_metric_model_dict,
    scan_metric_llm,
)
from core.worker.unified_aliases import alias_unified_model_roles


def test_injected_llm_uses_reflection_on_worker() -> None:
    """Recognize an unimported injected helper and resolve its model."""
    code = "def score(c, case=None):\n    return llm('x')"
    assert scan_metric_llm(code).uses_llm


@pytest.mark.parametrize("prefix", ["", "from skynet import llm\n"])
def test_scoped_shadow_does_not_hide_global_helper(prefix: str) -> None:
    """Keep parameter bindings local to their function."""
    scan = scan_metric_llm(prefix + "def helper(llm): return llm\ndef score(): return llm('x', model='openai/a')")
    assert (scan.uses_llm, scan.names, scan.non_literal) == (True, {"openai/a"}, False)


@pytest.mark.parametrize(
    "local", ["def helper():\n llm = other\n return llm('local')", "helper = lambda llm: llm('local')"]
)
def test_local_shadow_does_not_hide_global_helper(local: str) -> None:
    """Ignore local calls while scanning a global helper in another scope."""
    scan = scan_metric_llm(local + "\ndef score(): return llm('x', model='openai/a')")
    assert (scan.uses_llm, scan.names, scan.non_literal) == (True, {"openai/a"}, False)


def test_positional_model_name_is_not_keyword() -> None:
    """Ignore positional arguments named model."""
    scan = scan_metric_llm("llm(model, model='anthropic/expensive')")
    assert (scan.names, scan.non_literal) == ({"anthropic/expensive"}, False)


@pytest.mark.parametrize(
    "code",
    [
        "import skynet\nf(skynet=1)\nllm(model='a')",
        "from skynet import llm\ndef f():\n global llm\n return llm(model='a')",
    ],
)
def test_keyword_and_global_declaration_are_not_value_uses(code: str) -> None:
    """Ignore keyword names and global declarations."""
    scan = scan_metric_llm(code)
    assert (scan.uses_llm, scan.names, scan.non_literal) == (True, {"a"}, False)


def test_global_helper_rebinding_is_ambiguous() -> None:
    """Reject rebinding a module helper through a global declaration."""
    scan = scan_metric_llm("from skynet import llm\ndef f():\n global llm\n llm = partial(llm)\nllm(model='a')")
    assert scan.uses_llm
    assert scan.non_literal


@pytest.mark.parametrize("suffix", ["llm('again')", "llm('again', model=None)"])
def test_omitted_and_distinct_model_are_ambiguous(suffix: str) -> None:
    """Count the reflection model for omitted calls."""
    code = "llm('judge', model='anthropic/a')\n" + suffix
    scan = scan_metric_llm(code)
    assert scan.omitted
    models = UnifiedModels.model_validate({"reflection_model": {"name": "openai/r"}})
    assert resolve_metric_model(models, code) == "contract.metric_model_declaration"
    models.reflection_model.name = "anthropic/a"
    assert resolve_metric_model(models, code) is None


@pytest.mark.parametrize(
    "expr",
    [
        "getattr(skynet, 'llm')",
        "getattr(x, 'llm')",
        "globals()['llm']",
        "vars(skynet)['llm']",
        "skynet.__dict__['llm']",
        "skynet",
    ],
)
def test_dynamic_helper_access_is_ambiguous(expr: str) -> None:
    """Reject dynamic access that can hide a helper call."""
    scan = scan_metric_llm("import skynet\nx = " + expr)
    assert scan.uses_llm
    assert scan.non_literal


def test_llm_keyword_name_is_not_helper_use() -> None:
    """Ignore an unrelated keyword named llm."""
    scan = scan_metric_llm(
        "from skynet import llm\ndef score(c):\n f(llm=1)\n return llm('x', model='openai/gpt-4.1-mini')"
    )
    assert (scan.uses_llm, scan.names, scan.non_literal) == (True, {"openai/gpt-4.1-mini"}, False)


@pytest.mark.parametrize(
    "code",
    [
        "from skynet import llm\nllm = partial(llm, model='a')\nllm('x')",
        "from skynet import llm as judge\njudge = partial(judge, model='a')\njudge('x')",
        "from skynet import *\nllm = partial(llm, model='a')\nllm('x')",
        "llm = partial(llm, model='a')\nllm('x')",
        "from skynet import llm as judge\njudge = other\njudge('x')",
    ],
)
def test_rebound_helper_is_ambiguous(code: str) -> None:
    """Reject helper rebinding rather than losing its model declaration."""
    scan = scan_metric_llm(code)
    assert scan.uses_llm
    assert scan.non_literal
    models = UnifiedModels.model_validate({"reflection_model": {"name": "r"}})
    assert resolve_metric_model(models, code) == "contract.metric_model_declaration"


def test_worker_aliases_injected_llm_to_reflection() -> None:
    """Route an injected helper through the reflection model on the worker."""
    code = "def score(c, case=None):\n    return llm('x')"
    payload = {"models": {"reflection_model": {"name": "openai/reflection"}}, "workspace": {"metric_code": code}}
    alias_unified_model_roles(payload)
    assert payload["models"]["metric_model"]["name"] == "openai/reflection"
    assert payload["scorer"]["model"] == payload["models"]["metric_model"]


@pytest.mark.parametrize(
    ("code", "uses", "names", "non_literal"),
    [
        ("pass", False, set(), False),
        ("def broken(", False, set(), False),
        ("from skynet import llm as ask", True, set(), False),
        ("import skynet\nskynet.llm('x', model='openai/a')", True, {"openai/a"}, False),
        ("from skynet import llm\nllm('x'); llm('y', model='openai/a')", True, {"openai/a"}, False),
        ("from skynet import llm\nllm(model='a'); llm(model='b')", True, {"a", "b"}, False),
        ("from skynet import llm\nllm(model=name)", True, set(), True),
        ("from skynet import llm\nllm(model=f'{name}')", True, set(), True),
        ("from skynet import llm\nllm(**options)", True, set(), True),
    ],
)
def test_scan(code: str, uses: bool, names: set[str], non_literal: bool) -> None:
    """Find helper use and model declarations without executing code."""
    scan = scan_metric_llm(code)
    assert (scan.uses_llm, scan.names, scan.non_literal) == (uses, names, non_literal)


@pytest.mark.parametrize(
    "code",
    [
        "llm = 'judge'",
        "output.llm",
        "output.llm(model=choice)",
        "from other import llm as judge\njudge(model=choice)",
        "llm = 'judge'\nllm(model=choice)",
        "def llm(x): return x\nllm(model=choice)",
        "def score(llm): return llm(model=choice)",
        "from other import helper as llm\nllm(model=choice)",
        "import other as llm\nllm(model=choice)",
        "for llm in items: pass\nllm(model=choice)",
        "match x:\n    case llm: pass\nllm(model=choice)",
    ],
)
def test_unrelated_llm_names_do_not_declare_a_model(code: str) -> None:
    """Ignore unrelated names and attributes."""
    scan = scan_metric_llm(code)
    assert (scan.uses_llm, scan.names, scan.non_literal) == (False, set(), False)


@pytest.mark.parametrize(
    "code",
    [
        "from skynet import *\nllm(model='openai/judge')",
        "import skynet as s\ns.llm(model='openai/judge')",
    ],
)
def test_imported_helper_bindings_are_scanned(code: str) -> None:
    """Find direct helper calls through module and star imports."""
    assert scan_metric_llm(code).names == {"openai/judge"}


def test_client_base_url_does_not_change_declared_provider() -> None:
    """Ignore an untrusted endpoint when deciding the metric connection."""
    providers = []
    for base_url in (None, "https://x"):
        models = UnifiedModels.model_validate(
            {
                "reflection_model": {
                    "name": "anthropic/r",
                    "token_source": "byok",
                    "byok_provider": "anthropic",
                    "base_url": base_url,
                }
            }
        )
        assert resolve_metric_model(models, "from skynet import llm\nllm(model='openai/judge')") is None
        providers.append(models.metric_model.byok_provider)
    assert providers[0] == providers[1]


def test_english_declaration_message_matches_catalog() -> None:
    """Keep the English fallback in sync with the generated catalog."""
    catalog = json.loads((Path(__file__).resolve().parents[4] / "i18n_locales" / "en.json").read_text())
    key = "contract.metric_model_declaration"
    assert ENGLISH_MESSAGES[key] == catalog["messages"][key]


@pytest.mark.parametrize("preserve", [True, False])
def test_declared_subscription_model_keeps_cli_on_worker_and_setup(
    monkeypatch: pytest.MonkeyPatch, preserve: bool
) -> None:
    """Resolve a local subscription declaration on both execution paths."""
    monkeypatch.setattr(settings, "local_mode", True)
    payload = {
        "models": {
            "reflection_model": {
                "name": "subscription/claude/opus",
                "token_source": "subscription",
                "cli_provider": "claude",
            },
            "metric_model": {
                "name": "subscription/codex/gpt-5",
                "token_source": "subscription",
                "cli_provider": "codex",
            },
        },
        "workspace": {"metric_code": "from skynet import llm\nllm(model='subscription/codex/gpt-5')"},
    }
    assert resolve_metric_model_dict(payload, preserve_metric_model=preserve) is None
    assert payload["models"]["metric_model"]["token_source"] == "subscription"
    assert payload["models"]["metric_model"]["cli_provider"] == "codex"


def test_subscription_declaration_is_not_normalized_in_hosted_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only local mode recognizes a CLI subscription model identifier."""
    monkeypatch.setattr(settings, "local_mode", False)
    models = UnifiedModels.model_validate({"reflection_model": {"name": "r", "token_source": "managed"}})
    assert resolve_metric_model(models, "from skynet import llm\nllm(model='subscription/codex/gpt-5')") is None
    assert models.metric_model.token_source == "managed"
    assert models.metric_model.cli_provider is None


def test_resolution_ignores_client_model_and_copies_reflection() -> None:
    """Use reflection by default and discard a submitted metric model."""
    models = UnifiedModels.model_validate(
        {"reflection_model": {"name": "openrouter/r", "temperature": 0.2}, "metric_model": {"name": "wrong"}}
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm('x')") is None
    assert models.metric_model == models.reflection_model
    assert models.metric_model is not models.reflection_model
    assert resolve_metric_model(models, "pass") is None
    assert models.metric_model is None
    assert (
        resolve_metric_model(models, "from skynet import llm\nllm(model='a'); llm(model='b')")
        == "contract.metric_model_declaration"
    )
    assert models.metric_model is None


def test_declared_model_uses_custom_reflection_connection() -> None:
    """Keep a custom BYOK connection for a distinct declared model."""
    models = UnifiedModels.model_validate(
        {"reflection_model": {"name": "openai/r", "token_source": "byok", "byok_provider": "custom"}}
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm('x', model='openai/judge')") is None
    assert models.metric_model is not None
    assert models.metric_model.byok_provider == "custom"


def test_declared_model_inherits_custom_connection_on_native_provider() -> None:
    """Preserve a custom endpoint selected under a native provider slug."""
    models = UnifiedModels.model_validate(
        {"reflection_model": {"name": "openai/r", "token_source": "byok", "byok_provider": "google"}}
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm(model='openai/judge')") is None
    assert models.metric_model is not None
    assert models.metric_model.byok_provider == "google"


def test_no_declaration_reuses_reflection_even_when_task_name_matches() -> None:
    """The no-model fallback uses reflection even if the task has the same name."""
    models = UnifiedModels.model_validate(
        {
            "task_model": {"name": "openai/a", "temperature": 0.1},
            "reflection_model": {"name": "openai/a", "temperature": 0.9},
        }
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm('x')") is None
    assert models.metric_model == models.reflection_model


@pytest.mark.parametrize("provider", ["custom", "openrouter"])
def test_cross_provider_reflection_connection_matches_picked_model(provider: str) -> None:
    """Use the declared model's provider when the reflection cannot serve it."""
    models = UnifiedModels.model_validate(
        {"reflection_model": {"name": "openai/reflection", "token_source": "byok", "byok_provider": provider}}
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm(model='anthropic/judge')") is None
    assert models.metric_model is not None
    picked = {"name": "anthropic/judge", "token_source": "byok"}
    payload = {"task_model_config": picked, "scorer": {"model": models.metric_model.model_dump()}}
    vault = Mock()
    vault.resolve_connection.return_value = SimpleNamespace(secret="test-key", api_base=None, params={})
    inject_byok_connections(payload, username="test", vault=vault)
    assert [call.args[1] for call in vault.resolve_connection.call_args_list] == ["anthropic", "anthropic"]


def test_declared_task_model_reuses_task_config() -> None:
    """Reuse the task role when the declaration names the task model."""
    models = UnifiedModels.model_validate(
        {
            "task_model": {"name": "openai/judge", "token_source": "byok", "byok_provider": "custom"},
            "reflection_model": {"name": "openai/r", "token_source": "managed"},
        }
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm(model='openai/judge')") is None
    assert models.metric_model == models.task_model


@pytest.mark.parametrize("provider", sorted(_PROVIDER_PROBES))
def test_distinct_declared_provider_uses_picked_provider(provider: str) -> None:
    """Derive credentials for a model not already present as a role."""
    name = f"{BYOK_TO_LITELLM_PROVIDER.get(provider, provider)}/judge"
    models = UnifiedModels.model_validate(
        {"reflection_model": {"name": "openrouter/reflection", "token_source": "byok", "byok_provider": "openrouter"}}
    )
    assert resolve_metric_model(models, f"from skynet import llm\nllm(model={name!r})") is None
    assert models.metric_model is not None
    payload = {
        "task_model_config": {"name": name, "token_source": "byok"},
        "scorer": {"model": models.metric_model.model_dump()},
    }
    vault = Mock()
    vault.resolve_connection.return_value = SimpleNamespace(secret="test-key", api_base=None, params={})
    inject_byok_connections(payload, username="test", vault=vault)
    assert [call.args[1] for call in vault.resolve_connection.call_args_list] == [provider, provider]


def test_matching_subscription_task_reuses_cli_provider() -> None:
    """Reuse the task's subscription source and CLI provider."""
    models = UnifiedModels.model_validate(
        {
            "task_model": {"name": "subscription/codex/model", "token_source": "subscription", "cli_provider": "codex"},
            "reflection_model": {"name": "openai/r", "token_source": "managed"},
        }
    )
    assert resolve_metric_model(models, "from skynet import llm\nllm(model='subscription/codex/model')") is None
    assert models.metric_model == models.task_model


@pytest.mark.parametrize(
    ("code", "names", "non_literal"),
    [
        ("from skynet import llm as judge\njudge('x', model='openai/judge')", {"openai/judge"}, False),
        ("from skynet import llm as judge\njudge('x', model=choice)", set(), True),
    ],
)
def test_scan_alias_declarations(code: str, names: set[str], non_literal: bool) -> None:
    """Inspect model arguments passed through imported aliases."""
    scan = scan_metric_llm(code)
    assert (scan.names, scan.non_literal) == (names, non_literal)


@pytest.mark.parametrize(
    "code",
    [
        "from skynet import llm\nx = llm",
        "from skynet import llm\npartial(llm, model='a')",
        "from skynet import llm\nf(llm)",
        "import skynet\nx = skynet.llm",
        "import skynet as s\nx = s.llm",
        "from skynet import *\nx = llm",
        "from skynet import llm as judge\nx = judge",
        "x = llm",
        "partial(llm, model='a')",
    ],
)
def test_llm_value_reference_is_rejected(code: str) -> None:
    """Reject helper references outside a direct call."""
    models = UnifiedModels.model_validate({"reflection_model": {"name": "r"}})
    assert resolve_metric_model(models, code) == "contract.metric_model_declaration"


@pytest.mark.parametrize("code", ["llm(model=choice)", "llm(**options)", "llm(model='a'); llm(model='b')"])
def test_injected_llm_rejects_unsafe_declarations(code: str) -> None:
    """Apply declaration restrictions to the injected helper."""
    models = UnifiedModels.model_validate({"reflection_model": {"name": "r"}})
    assert resolve_metric_model(models, code) == "contract.metric_model_declaration"


def test_none_model_uses_reflection_and_does_not_conflict() -> None:
    """Treat an explicit None like an omitted model."""
    models = UnifiedModels.model_validate({"reflection_model": {"name": "r"}})
    assert resolve_metric_model(models, "from skynet import llm\nllm(model=None)") is None
    assert models.metric_model == models.reflection_model
    assert resolve_metric_model(models, "from skynet import llm\nllm(model=None); llm(model='a')") == (
        "contract.metric_model_declaration"
    )


@pytest.mark.parametrize("provider", sorted(_PROVIDER_PROBES))
def test_declared_byok_model_uses_same_connection_as_picked_model(provider: str) -> None:
    """Resolve every provider through the same bridge for picked and declared roles."""
    model = f"{BYOK_TO_LITELLM_PROVIDER.get(provider, provider)}/example"
    payload = {
        "token_source": "byok",
        "models": {"task_model": {"name": model}, "reflection_model": {"name": "openrouter/reflection"}},
        "workspace": {"metric_code": f"from skynet import llm\nllm(model={model!r})"},
    }
    alias_unified_model_roles(payload)
    vault = Mock()
    vault.resolve_connection.return_value = SimpleNamespace(secret="test-key", api_base=None, params={})
    inject_byok_connections(payload, username="test", vault=vault, default_token_source="byok")
    assert payload["task_model_config"]["extra"] == payload["scorer"]["model"]["extra"]
    calls = vault.resolve_connection.call_args_list
    assert calls[0].args[1] == calls[-1].args[1] == provider


def test_alias_resolves_before_protection_and_is_idempotent() -> None:
    """Alias the derived model by identity for protection's nested updates."""
    payload = {
        "models": {"reflection_model": {"name": "r"}, "metric_model": {"name": "wrong"}},
        "workspace": {"metric_code": "from skynet import llm\nllm(model='openai/a')"},
    }
    alias_unified_model_roles(payload)
    first = payload["models"]["metric_model"]
    assert first["name"] == "openai/a"
    assert payload["scorer"]["model"] is first
    alias_unified_model_roles(payload)
    assert payload["models"]["metric_model"] == first
    assert payload["scorer"]["model"] is payload["models"]["metric_model"]
    payload["workspace"]["metric_code"] = "from skynet import llm\nllm(model=x)"
    with pytest.raises(ValueError, match="string literal"):
        alias_unified_model_roles(payload)


@pytest.mark.parametrize("code", ["from skynet import llm\nllm('x')", "pass"])
def test_worker_alias_keeps_legacy_model_only_when_enabled(code: str) -> None:
    """The execution path preserves old selections; setup derives reflection instead."""
    old = {"name": "anthropic/old", "token_source": "byok", "byok_provider": "anthropic"}
    payload = {
        "models": {"reflection_model": {"name": "openai/reflection"}, "metric_model": old},
        "workspace": {"metric_code": code},
    }
    alias_unified_model_roles(payload, preserve_metric_model=True)
    assert payload["models"]["metric_model"] == old
    assert payload["scorer"]["model"] is old
    alias_unified_model_roles(payload)
    if code == "pass":
        assert payload["models"]["metric_model"] is None
    else:
        assert payload["models"]["metric_model"]["name"] == "openai/reflection"


def test_worker_alias_rejects_unsafe_or_replaces_explicit_declarations() -> None:
    """Legacy preservation never overrides a declaration or accepts unknown options."""
    payload = {
        "models": {"reflection_model": {"name": "openai/reflection"}, "metric_model": {"name": "old"}},
        "workspace": {"metric_code": "from skynet import llm\nllm(model='new')"},
    }
    alias_unified_model_roles(payload, preserve_metric_model=True)
    assert payload["models"]["metric_model"]["name"] == "new"
    payload["workspace"]["metric_code"] = "from skynet import llm\nllm(model=choice)"
    with pytest.raises(ValueError, match="string literal"):
        alias_unified_model_roles(payload, preserve_metric_model=True)
