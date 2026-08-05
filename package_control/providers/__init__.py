from .bitbucket_repository_provider import BitBucketRepositoryProvider
from .github_repository_provider import GitHubRepositoryProvider
from .github_user_provider import GitHubUserProvider
from .gitlab_repository_provider import GitLabRepositoryProvider
from .gitlab_user_provider import GitLabUserProvider
from .json_repository_provider import JsonRepositoryProvider

from .channel_provider import ChannelProvider


REPOSITORY_PROVIDERS = [
    BitBucketRepositoryProvider,
    GitHubRepositoryProvider,
    GitHubUserProvider,
    GitLabRepositoryProvider,
    GitLabUserProvider,
    JsonRepositoryProvider
]

CHANNEL_PROVIDERS = [ChannelProvider]


def channel_provider_for(url, settings):
    for provider_class in CHANNEL_PROVIDERS:
        if provider_class.match_url(url):
            return provider_class(url, settings)
    return None


def repo_provider_for(url, settings):
    for provider_class in REPOSITORY_PROVIDERS:
        if provider_class.match_url(url):
            return provider_class(url, settings)
    return None
