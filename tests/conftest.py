import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.storage import Storage  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data = copy.deepcopy(data)
    data["paths"] = {"output_dir": str(tmp_path / "out"), "database": str(tmp_path / "out" / "test.db")}
    Path(data["paths"]["output_dir"]).mkdir(parents=True)
    data["secrets"] = {"smtp_from": "Aman <aman@sender.example.test>", "smtp_host": "", "smtp_port": 465,
                       "smtp_user": "", "smtp_password": "", "youtube_api_key": "x", "groq_api_key": "x"}
    data["enrichment"]["website_email_lookup"] = False  # no network in tests
    data["sending"]["delay_seconds"] = 0
    return data


@pytest.fixture
def storage(cfg):
    s = Storage(cfg["paths"]["database"])
    yield s
    s.close()
