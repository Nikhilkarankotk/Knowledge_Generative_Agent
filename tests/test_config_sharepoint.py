"""Tests for :mod:`app.core.config` SharePoint-related settings."""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_sharepoint_settings_defaults() -> None:
    fields = Settings.model_fields
    assert fields["sharepoint_enabled"].default is False
    assert fields["sharepoint_tenant_wide"].default is False
    assert fields["sharepoint_search_region"].default == ""
    assert fields["sharepoint_tenant_id"].default == ""
    assert fields["sharepoint_client_id"].default == ""
    assert fields["sharepoint_client_secret"].default == ""
    assert fields["sharepoint_graph_base_url"].default == "https://graph.microsoft.com/v1.0"
    assert fields["sharepoint_site_hostname"].default == ""
    assert fields["sharepoint_site_relative_path"].default == ""
    assert fields["sharepoint_allowed_sites"].default == ""
    assert fields["sharepoint_allowed_folders"].default == ""
    assert fields["sharepoint_limit"].default == 10
    assert fields["sharepoint_timeout_seconds"].default == 30.0
    assert fields["sharepoint_content_char_limit"].default == 20000


def _complete_env() -> dict[str, str]:
    return {
        "SHAREPOINT_ENABLED": "true",
        "SHAREPOINT_TENANT_ID": "tenant-123",
        "SHAREPOINT_CLIENT_ID": "client-456",
        "SHAREPOINT_CLIENT_SECRET": "secret-789",
        "SHAREPOINT_GRAPH_BASE_URL": "https://example.microsoft.com/v2.0",
        "SHAREPOINT_SITE_HOSTNAME": "knowledgegenagent.sharepoint.com",
        "SHAREPOINT_SITE_RELATIVE_PATH": "/sites/KnowledgeGenAgent",
        "SHAREPOINT_ALLOWED_SITES": "a.com,site1,web1 ; a.com,site2,web2",
        "SHAREPOINT_ALLOWED_FOLDERS": "sharepoint-rag-knowledge-base",
        "SHAREPOINT_LIMIT": "5",
        "SHAREPOINT_TIMEOUT_SECONDS": "45.0",
        "SHAREPOINT_CONTENT_CHAR_LIMIT": "10000",
    }


def test_sharepoint_settings_from_env(monkeypatch) -> None:
    for k, v in _complete_env().items():
        monkeypatch.setenv(k, v)
    settings = Settings()
    assert settings.sharepoint_enabled is True
    assert settings.sharepoint_tenant_id == "tenant-123"
    assert settings.sharepoint_client_id == "client-456"
    assert settings.sharepoint_client_secret == "secret-789"
    assert settings.sharepoint_graph_base_url == "https://example.microsoft.com/v2.0"
    assert settings.sharepoint_site_hostname == "knowledgegenagent.sharepoint.com"
    assert settings.sharepoint_site_relative_path == "/sites/KnowledgeGenAgent"
    assert settings.sharepoint_allowed_sites == "a.com,site1,web1 ; a.com,site2,web2"
    assert settings.sharepoint_allowed_sites_list == ["a.com,site1,web1", "a.com,site2,web2"]
    assert settings.sharepoint_allowed_folders_list == ["sharepoint-rag-knowledge-base"]
    assert settings.sharepoint_limit == 5
    assert settings.sharepoint_timeout_seconds == 45.0
    assert settings.sharepoint_content_char_limit == 10000


def test_sharepoint_allowed_sites_list_deduplicates_and_strips() -> None:
    settings = Settings(
        sharepoint_allowed_sites="site-a ; site-A ; site-b; ; site-B ",
    )
    assert settings.sharepoint_allowed_sites_list == ["site-a", "site-b"]


def test_sharepoint_allowed_sites_list_keeps_commas_inside_site_ids() -> None:
    settings = Settings(
        sharepoint_allowed_sites="host.sharepoint.com,1111,2222;host2.sharepoint.com,3333,4444",
    )
    assert settings.sharepoint_allowed_sites_list == [
        "host.sharepoint.com,1111,2222",
        "host2.sharepoint.com,3333,4444",
    ]


def test_sharepoint_allowed_sites_list_empty_string() -> None:
    settings = Settings(sharepoint_enabled=False, sharepoint_allowed_sites="")
    assert settings.sharepoint_allowed_sites_list == []


def test_sharepoint_allowed_folders_list_parses_and_normalizes() -> None:
    settings = Settings(
        sharepoint_allowed_folders=" sharepoint-rag-knowledge-base ; docs/ops ; ",
    )
    assert settings.sharepoint_allowed_folders_list == [
        "sharepoint-rag-knowledge-base",
        "docs/ops",
    ]


def test_sharepoint_allowed_folders_list_drops_unsafe_entries() -> None:
    settings = Settings(
        sharepoint_allowed_folders="safe-folder;../escape;\\windows;https://evil/x;/abs/root;bad@token",
    )
    assert settings.sharepoint_allowed_folders_list == ["safe-folder"]


def test_sharepoint_allowed_folders_list_empty_string() -> None:
    settings = Settings(sharepoint_enabled=False, sharepoint_allowed_folders="")
    assert settings.sharepoint_allowed_folders_list == []


def test_disabled_sharepoint_allows_incomplete_config() -> None:
    settings = Settings(
        sharepoint_enabled=False,
        sharepoint_tenant_id="",
        sharepoint_client_id="",
        sharepoint_client_secret="",
    )
    assert settings.sharepoint_enabled is False


def test_enabled_sharepoint_with_missing_required_fails_clearly() -> None:
    # sharepoint_allowed_folders is left empty despite SHAREPOINT_ENABLED=true
    # (scoped mode: tenant_wide must be off to require the full allowlist).
    with pytest.raises(ValueError, match="SHAREPOINT_ENABLED=true"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=False,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="s",
            sharepoint_site_hostname="x.sharepoint.com",
            sharepoint_site_relative_path="/sites/KnowledgeGenAgent",
            sharepoint_allowed_sites="x.sharepoint.com,1,2",
            sharepoint_allowed_folders="",
        )
    with pytest.raises(ValueError, match="SHAREPOINT_ALLOWED_FOLDERS"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=False,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="s",
            sharepoint_site_hostname="x.sharepoint.com",
            sharepoint_site_relative_path="/sites/KnowledgeGenAgent",
            sharepoint_allowed_sites="x.sharepoint.com,1,2",
            sharepoint_allowed_folders="",
        )


def test_enabled_sharepoint_missing_client_secret_fails_clearly() -> None:
    with pytest.raises(ValueError, match="SHAREPOINT_CLIENT_SECRET"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=False,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="",
            sharepoint_site_hostname="x",
            sharepoint_site_relative_path="/sites/KB",
            sharepoint_allowed_sites="x.com,1,2",
            sharepoint_allowed_folders="kb",
        )


def test_enabled_sharepoint_with_complete_config_ok() -> None:
    settings = Settings(
        sharepoint_enabled=True,
        sharepoint_tenant_id="t",
        sharepoint_client_id="c",
        sharepoint_client_secret="s",
        sharepoint_site_hostname="x.sharepoint.com",
        sharepoint_site_relative_path="/sites/KnowledgeGenAgent",
        sharepoint_allowed_sites="x.sharepoint.com,1,2",
        sharepoint_allowed_folders="sharepoint-rag-knowledge-base",
    )
    assert settings.sharepoint_enabled is True
    assert settings.sharepoint_allowed_folders_list == ["sharepoint-rag-knowledge-base"]


def test_tenant_wide_sharepoint_requires_only_credentials() -> None:
    # SHAREPOINT_TENANT_WIDE=true skips the site hostname/path and allowlist
    # requirements: only tenant, client, secret and search region are mandatory.
    settings = Settings(
        sharepoint_enabled=True,
        sharepoint_tenant_wide=True,
        sharepoint_tenant_id="t",
        sharepoint_client_id="c",
        sharepoint_client_secret="s",
        sharepoint_search_region="IND",
        sharepoint_allowed_sites="",
        sharepoint_allowed_folders="",
    )
    assert settings.sharepoint_tenant_wide is True
    assert settings.sharepoint_search_region == "IND"
    assert settings.sharepoint_allowed_sites == ""
    assert settings.sharepoint_allowed_folders == ""


def test_tenant_wide_sharepoint_requires_search_region() -> None:
    # Tenant-wide uses the Microsoft Graph Search API, which requires the region
    # for application-permission requests.
    with pytest.raises(ValueError, match="SHAREPOINT_SEARCH_REGION"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=True,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="s",
            sharepoint_search_region="",
            sharepoint_allowed_sites="",
            sharepoint_allowed_folders="",
        )


def test_tenant_wide_sharepoint_still_requires_credentials() -> None:
    with pytest.raises(ValueError, match="SHAREPOINT_CLIENT_SECRET"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=True,
            sharepoint_tenant_id="t",
            sharepoint_client_id="c",
            sharepoint_client_secret="",
            sharepoint_search_region="IND",
        )
    with pytest.raises(ValueError, match="SHAREPOINT_TENANT_ID"):
        Settings(
            sharepoint_enabled=True,
            sharepoint_tenant_wide=True,
            sharepoint_tenant_id="",
            sharepoint_client_id="c",
            sharepoint_client_secret="s",
            sharepoint_search_region="IND",
        )


def test_tenant_wide_sharepoint_from_env(monkeypatch) -> None:
    env = {
        "SHAREPOINT_ENABLED": "true",
        "SHAREPOINT_TENANT_WIDE": "true",
        "SHAREPOINT_SEARCH_REGION": "IND",
        "SHAREPOINT_TENANT_ID": "tenant-t",
        "SHAREPOINT_CLIENT_ID": "client-c",
        "SHAREPOINT_CLIENT_SECRET": "secret-s",
        # Scoped fields intentionally absent: must not be required in tenant-wide.
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    settings = Settings()
    assert settings.sharepoint_tenant_wide is True
    assert settings.sharepoint_search_region == "IND"
    assert settings.sharepoint_enabled is True


def test_sharepoint_sk_agent_timeout_seconds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SK_AGENT_TIMEOUT_SECONDS", "120")
    settings = Settings()
    assert settings.sk_agent_timeout_seconds == 120.0
