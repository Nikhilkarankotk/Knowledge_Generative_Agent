"""Tests for the GitHub allowlist settings (``GITHUB_ALLOWED_REPOSITORIES``)."""

from __future__ import annotations

from app.core.config import Settings


def test_allowlist_defaults_to_empty() -> None:
    settings = Settings(github_allowed_repositories="")
    assert settings.github_allowed_repositories == ""
    assert settings.github_allowed_repository_list == []


def test_allowlist_parses_comma_separated_and_trims() -> None:
    settings = Settings(
        github_allowed_repositories=(
            " nikhilkarankotk/E-Commerce_Application , "
            "https://github.com/Acme/repo.git, acme/repo, , aB/c "
        )
    )
    assert settings.github_allowed_repository_list == [
        "nikhilkarankotk/E-Commerce_Application",
        "Acme/repo",
        "aB/c",
    ]


def test_allowlist_deduplicates_case_insensitively() -> None:
    settings = Settings(github_allowed_repositories="Owner/Repo, owner/repo,OWNER/REPO")
    assert settings.github_allowed_repository_list == ["Owner/Repo"]


def test_allowlist_ignores_empty_entries() -> None:
    settings = Settings(github_allowed_repositories=" , a/b ,, c/d  ,")
    assert settings.github_allowed_repository_list == ["a/b", "c/d"]


def test_allowlist_normalizes_git_and_url_forms() -> None:
    settings = Settings(
        github_allowed_repositories="owner/repo.git,https://github.com/owner/repo2/"
    )
    assert settings.github_allowed_repository_list == ["owner/repo", "owner/repo2"]


def test_allowlist_does_not_collapse_punctuation() -> None:
    settings = Settings(github_allowed_repositories="nikhilkarankotk/E-Commerce_Application")
    parsed = settings.github_allowed_repository_list
    assert parsed == ["nikhilkarankotk/E-Commerce_Application"]
    # Hyphen variant must NOT be collapsed to underscore:
    # GitHub treats these as distinct identifiers (wrong name = 404).
    assert "nikhilkarankotk/E-Commerce_Application" != "nikhilkarankotk/E-commerce-Application"
    assert parsed[0].lower() != "nikhilkarankotk/E-commerce-Application".lower()
