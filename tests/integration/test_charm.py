#!/usr/bin/env python3
# Copyright 2024 Jakob Ersson
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
BITCOIN_VERSION = "31.0"


@pytest.mark.abort_on_fail
async def test_deploy_blocks_without_version(ops_test: OpsTest):
    """Build and deploy the charm; without `version` set, the unit must block.

    Covers charm packaging and the install hook (apt/pip dependencies, service
    units) without downloading Bitcoin Core.
    """
    charm = await ops_test.build_charm(".")
    await ops_test.model.deploy(charm, application_name=APP_NAME)
    await ops_test.model.wait_for_idle(apps=[APP_NAME], status="blocked", timeout=600)
    unit = ops_test.model.applications[APP_NAME].units[0]
    assert unit.workload_status_message == "bitcoind not installed; set version"


@pytest.mark.abort_on_fail
async def test_version_config_activates_node(ops_test: OpsTest):
    """Setting `version` installs bitcoind and brings node, monitor and proxy up.

    Covers the two release risk areas: the binary download/extraction path
    (utils.install_bitcoin) and service-arg handling (utils.update_service_args).
    """
    await ops_test.model.applications[APP_NAME].set_config(
        {
            "version": BITCOIN_VERSION,
            "rpc-user": "test",
            "rpc-password": "test",
            # -networkactive=0: RPC serves right after start, with no P2P
            # traffic or initial block download on the test machine.
            "service-args": "-chain=main -server=1 -networkactive=0",
        }
    )
    # The config-changed hook downloads and extracts the Bitcoin Core release
    # inline (~50 MB), so allow generously for slow networks.
    await ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", timeout=1800)
    unit = ops_test.model.applications[APP_NAME].units[0]
    assert unit.workload_status_message == "Node: up, monitor: up, proxy: up"


async def test_get_node_info_action(ops_test: OpsTest):
    """get-node-info reports the installed version, running process, and hardened args."""
    unit = ops_test.model.applications[APP_NAME].units[0]
    action = await unit.run_action("get-node-info")
    action = await action.wait()
    assert action.status == "completed"
    results = action.results
    assert BITCOIN_VERSION in results["client-version"]
    assert results["client-proc-cmdline"] != "process not found"
    # harden_service_args must have pinned bitcoind's RPC to loopback.
    assert "-rpcbind=127.0.0.1" in results["client-service-args"]
    assert str(results["rpc-proxy-installed"]) == "True"
