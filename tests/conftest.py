import os
import pytest

@pytest.fixture(autouse=True)
def clean_finance_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("FINANCE_"):
            monkeypatch.delenv(name)
