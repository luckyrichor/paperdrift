from .base import Candidate, Fetcher
from .wallhaven import WallhavenFetcher
from .danbooru import DanbooruFetcher


def make_fetchers(settings) -> list[Fetcher]:
    return [
        WallhavenFetcher(api_key=settings.wallhaven_key),
        DanbooruFetcher(login=settings.danbooru_login, api_key=settings.danbooru_key),
    ]


__all__ = ["Candidate", "Fetcher", "make_fetchers"]
