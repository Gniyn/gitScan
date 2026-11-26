import pytest

from app.github import GitHubError, parse_repository


def test_parse_repository():
    repo = parse_repository("https://github.com/openai/openai-python.git")
    assert repo.owner == "openai"
    assert repo.name == "openai-python"
    assert repo.url == "https://github.com/openai/openai-python"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "https://github.com/a/b/tree/main",
        "https://github.com/a",
        "https://github.com/a/b?token=x",
        "https://user@github.com/a/b",
    ],
)
def test_reject_non_repository_urls(url):
    with pytest.raises(GitHubError):
        parse_repository(url)
