from src.brti.feeds.base import BaseFeed
from src.brti.feeds.bitstamp import BitstampFeed
from src.brti.feeds.coinbase import CoinbaseFeed
from src.brti.feeds.gemini import GeminiFeed
from src.brti.feeds.kraken import KrakenFeed

FEEDS: dict[str, type[BaseFeed]] = {
    "coinbase": CoinbaseFeed,
    "kraken": KrakenFeed,
    "bitstamp": BitstampFeed,
    "gemini": GeminiFeed,
}

__all__ = ["BaseFeed", "CoinbaseFeed", "KrakenFeed", "BitstampFeed", "GeminiFeed", "FEEDS"]
