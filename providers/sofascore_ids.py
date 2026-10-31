"""Sofascore league IDs checked against ScraperFC's reference.

Domestic cup IDs are verified against provider pages in docs/FIXTURES.md.

Season IDs are resolved from the provider's season index, never from the year.
Match and player IDs come from fixture and lineup responses respectively.
"""

REFERENCE_PACKAGE = 'ScraperFC'
REFERENCE_REVISION = '50f5df9fae4141f91174debdb14cf87fb8ed810a'
REFERENCE_URL = (
    f'https://github.com/oseymour/ScraperFC/blob/{REFERENCE_REVISION}/src/ScraperFC/comps.yaml'
)

SOFASCORE_COMPETITION_IDS = {
    'Premier League': 17,
    'Championship': 18,
    'La Liga': 8,
    'La Liga 2': 54,
    'Bundesliga': 35,
    '2. Bundesliga': 44,
    'Serie A': 23,
    'Serie B': 53,
    'Ligue 1': 34,
    'Ligue 2': 182,
    'Champions League': 7,
    'Europa League': 679,
    'Conference League': 17015,
    # Domestic cup IDs verified against the provider's tournament pages.
    'FA Cup': 19,
    'EFL Cup': 21,
    'Community Shield': 346,
    'Copa del Rey': 329,
    'Supercopa de España': 213,
    'DFB-Pokal': 217,
    'DFL-Supercup': 799,
    'Coppa Italia': 328,
    'Supercoppa Italiana': 341,
    'Coupe de France': 335,
    'Coupe de la Ligue': 333,
    'Trophée des Champions': 339,
}
