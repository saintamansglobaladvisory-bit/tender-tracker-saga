import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper

TEST_PASSWORD = 'code-de-test'


@pytest.fixture(autouse=True)
def data_encryption(monkeypatch):
    """Code d'accès de test et dérivation de clé allégée pour garder des tests rapides."""
    monkeypatch.setenv(scraper.DATA_PASSWORD_ENV, TEST_PASSWORD)
    monkeypatch.setattr(scraper, 'KDF_ITERATIONS', 1000)
