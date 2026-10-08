from flagbox import __version__
from flagbox.cli import main


def test_version_command_prints_version(capsys):
    exit_code = main(["version"])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == f"flagbox {__version__}"
