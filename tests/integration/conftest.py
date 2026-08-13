import os
import pathlib
import sys
import time

import jubilant
import pytest


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Create a disposable model and retain diagnostics on failure."""
    controller = os.getenv("JUJU_CONTROLLER")
    with jubilant.temp_model(controller=controller) as client:
        yield client
        if request.session.testsfailed:
            time.sleep(0.5)
            print(client.debug_log(limit=1000), end="", file=sys.stderr)


@pytest.fixture(scope="session")
def charm() -> pathlib.Path:
    """Return the exact charm artifact supplied by the build gate."""
    raw = os.getenv("CHARM_PATH")
    if not raw:
        pytest.fail("CHARM_PATH must identify the exact built charm artifact")
    path = pathlib.Path(raw).resolve()
    if not path.is_file():
        pytest.fail(f"CHARM_PATH does not exist: {path}")
    return path
