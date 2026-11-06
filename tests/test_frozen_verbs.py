"""The packaged app starts itself with a verb (curate, map3d, ...). Its entry script
must know every verb the command line has, or that child fails to start."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_packaged_entry_knows_every_verb():
    cli = open(os.path.join(ROOT, "aquila", "cli.py")).read()
    entry = open(os.path.join(ROOT, "packaging", "pyinstaller", "entry.py")).read()
    verbs = set(re.findall(r'add_parser\("(\w+)"', cli))
    known = set(re.findall(r'"(\w+)"', re.search(r"VERBS = \{([^}]*)\}", entry).group(1)))
    assert verbs and verbs == known
