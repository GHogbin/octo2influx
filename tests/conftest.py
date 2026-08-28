import sys
from pathlib import Path

import pytest


SRC_DIR = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC_DIR))

from octo2influx import cfg


@pytest.fixture(autouse=True)
def clear_config():
    cfg.clear()
    yield
    cfg.clear()


@pytest.fixture
def load_example_config():
    cfg.set_file(str(SRC_DIR / 'config.example.yaml'))
