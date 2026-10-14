"""Sofascore requests and domain loading."""
from .sofaScoreClient import SofaScoreClient, SofaScoreError, SofaScoreRequestError
from .sofaScoreLoader import SofaScoreLoader
from .fotMobClient import FotMobClient
from .fotMobLoader import FotMobLoader
from .whoScoredClient import WhoScoredClient
from .whoScoredLoader import WhoScoredLoader
from .httpClient import ProviderError, ProviderRequestError, RequestGate

