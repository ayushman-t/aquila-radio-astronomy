"""A config saved by Notepad or Windows PowerShell starts with a byte order mark."""
from aquila import config


def test_bom_config_loads(tmp_path):
    p = tmp_path / "config.json"
    p.write_bytes(b'\xef\xbb\xbf{"name": "bom-node"}')
    assert config.load(str(p))["name"] == "bom-node"
