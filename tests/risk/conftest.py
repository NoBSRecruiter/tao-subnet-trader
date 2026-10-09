"""WP8 risk tests: the shared builders of tests/risk/wp8_kit.py as the `kit` fixture (importlib mode: test modules
cannot import each other)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

KIT_PATH = Path(__file__).resolve().with_name("wp8_kit.py")


def load_kit() -> ModuleType:
    name = "wp8_kit"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(name, KIT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def kit() -> ModuleType:
    return load_kit()
