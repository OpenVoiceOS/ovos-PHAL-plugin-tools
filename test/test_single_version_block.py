"""The version file has one block, and ``__version__`` is what that block says."""
import os
import re
import runpy

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ovos_phal_plugin_tools", "version.py")


def _block_fields(text):
    block = re.search(r"# START_VERSION_BLOCK\n(.*?)# END_VERSION_BLOCK", text, re.S)
    assert block, "no START/END version block"
    return dict(re.findall(r"^VERSION_(\w+) = (\d+)$", block.group(1), re.M))


def test_version_comes_from_the_single_bumped_block():
    text = open(PATH).read()
    assert text.count("VERSION_MAJOR = ") == 1, "one block only; a second block shadows the bumped one"
    f = _block_fields(text)
    expected = f"{f['MAJOR']}.{f['MINOR']}.{f['BUILD']}" + (f"a{f['ALPHA']}" if int(f["ALPHA"]) else "")
    assert runpy.run_path(PATH)["__version__"] == expected
