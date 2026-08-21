import os


def test_controller_is_explicitly_forwarded():
    """Integration must never fall back to the operator's current controller."""
    assert os.environ.get("JUJU_CONTROLLER"), "JUJU_CONTROLLER was not forwarded into the integration environment"
