"""Deterministic coverage for provider enable/disable (the Models grid)."""

from __future__ import annotations

import os
from pathlib import Path

from zhiyan.loop.models import PROVIDERS

from zhiyan import integrations
from zhiyan.config import Settings


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHIYAN_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZHIYAN_DISABLED_PROVIDERS", raising=False)
    monkeypatch.delenv("ZHIYAN_PROVIDER", raising=False)


def test_disabled_providers_parsing(monkeypatch):
    monkeypatch.setenv("ZHIYAN_DISABLED_PROVIDERS", "openai, minimax,,xai")
    assert Settings().disabled_providers == frozenset({"openai", "minimax", "xai"})
    monkeypatch.delenv("ZHIYAN_DISABLED_PROVIDERS")
    assert Settings().disabled_providers == frozenset()


def test_toggle_persists_and_roundtrips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert integrations.apply_provider_disabled("openai", disabled=True).ok
    assert os.environ["ZHIYAN_DISABLED_PROVIDERS"] == "openai"
    # dotenv.set_key quotes the value — verify it roundtrips correctly instead
    # of asserting the exact format.
    assert "ZHIYAN_DISABLED_PROVIDERS" in (tmp_path / ".env").read_text(encoding="utf-8")
    from dotenv import dotenv_values
    assert dotenv_values(tmp_path / ".env")["ZHIYAN_DISABLED_PROVIDERS"] == "openai"
    assert integrations.apply_provider_disabled("openai", disabled=False).ok
    assert os.environ["ZHIYAN_DISABLED_PROVIDERS"] == ""


def test_toggle_rejects_unknown_and_current(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert not integrations.apply_provider_disabled("nope", disabled=True).ok
    monkeypatch.setenv("ZHIYAN_PROVIDER", "anthropic")
    result = integrations.apply_provider_disabled("anthropic", disabled=True)
    assert not result.ok and "current" in result.error
    assert os.environ.get("ZHIYAN_DISABLED_PROVIDERS", "") == ""
    # Enabling (or disabling a non-current provider) is still fine.
    assert integrations.apply_provider_disabled("anthropic", disabled=False).ok
    assert integrations.apply_provider_disabled("openai", disabled=True).ok


def test_settings_info_reports_disabled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ZHIYAN_DISABLED_PROVIDERS", "glm")
    from zhiyan.ops.settings_api import settings_info

    assert settings_info()["disabled_providers"] == ["glm"]


def test_every_provider_has_a_logo():
    logos = Path(__file__).resolve().parents[2] / "zhiyan" / "ops" / "static" / "logos"
    missing = [name for name in PROVIDERS if not (logos / f"{name}.svg").is_file()]
    assert not missing, f"providers without a logo: {missing}"
