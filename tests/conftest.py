import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("obol", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["obol"] = mod
    spec.loader.exec_module(mod)
    return mod


obol_pkg = _load()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def make(tmp_path):
    def make(settings=None, now=None, secret=None):
        cfg = dict(settings or {})
        clock = Clock(now or 1_762_340_400.0)   # a Wednesday, on the hour
        o = obol_pkg.Obol(data_dir=tmp_path, get=lambda k, d=None: cfg.get(k, d),
                          set_=cfg.__setitem__, secret=lambda n: secret, clock=clock)
        o.cfg, o.clock_obj = cfg, clock
        return o
    return make
