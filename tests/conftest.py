from contextlib import closing
from pathlib import Path

import pytest
from helpers import CONFIG_PATH, build_snapshot, scenario_releases

from flagbox.server import db
from flagbox.server.config import Config, load_config
from flagbox.server.indicators import Indicator, discover


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return build_snapshot(tmp_path, scenario_releases())


@pytest.fixture
def cfg() -> Config:
    return load_config(CONFIG_PATH)


@pytest.fixture
def indicators() -> dict[str, Indicator]:
    return discover()


@pytest.fixture
def conn(data_dir: Path):
    with closing(db.connect_readonly(data_dir / "flagbox.sqlite")) as connection:
        yield connection
