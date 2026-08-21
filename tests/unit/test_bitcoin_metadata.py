from types import SimpleNamespace
from unittest.mock import call

from charms.dwellir.blockchain_common.v1.metadata.errors import MetadataValidationError

import bitcoin_metadata as metadata


class _Endpoint:
    def __init__(self, name: str) -> None:
        self.name = name


def test_model_view_recursively_redacts_config_and_relation_values():
    secret = "never-publish-this"
    unit = _Endpoint("remote/0")
    app = _Endpoint("remote")
    relation = SimpleNamespace(
        id=7,
        app=app,
        units=[unit],
        data={
            app: {"endpoint": f"https://alice:{secret}@rpc.example"},
            unit: {"nested": {"authorization": f"Bearer {secret}"}},
        },
    )
    model = SimpleNamespace(
        name="model",
        uuid="model-uuid",
        config={
            "collector-s3-credentials": "secret:collector-reference",
            "rpc-password": secret,
            "service-args": f"-rpcpassword={secret} -txindex=1",
        },
        relations={"rpc": [relation]},
    )

    view = metadata.RedactedModelView(model)

    serialized = repr({"config": view.config, "relations": view.relations})
    assert secret not in serialized
    assert "secret:collector-reference" not in serialized
    assert view.config["rpc-password"] == "REDACTED"
    assert "-txindex=1" in view.config["service-args"]


def test_recursive_redaction_covers_api_and_access_key_variants_in_sequences():
    secrets = {
        "api-key": "api-hyphen-secret",
        "x_api_key": "x-api-secret",
        "API_KEY": "api-env-secret",
        "AWS_ACCESS_KEY_ID": "aws-access-secret",
        "access.key": "access-dot-secret",
    }
    value = {
        "entries": [
            secrets,
            ("safe", {"nested-access_key": "nested-access-secret"}),
        ]
    }

    redacted = metadata._redact_value(value)

    serialized = repr(redacted)
    for secret in (*secrets.values(), "nested-access-secret"):
        assert secret not in serialized
    assert redacted["entries"][0] == dict.fromkeys(secrets, "REDACTED")


def test_runtime_redaction_covers_env_flags_headers_and_url_queries():
    value = (
        "API_KEY=api-env-secret X_API_KEY='x-api-secret' "
        "AWS_ACCESS_KEY_ID=aws-access-secret access_key=access-secret "
        "--api-key=api-flag-secret --x-api-key x-api-flag-secret "
        "x-api-key: header-secret "
        "https://rpc.example/path?api_key=query-secret&safe=visible&access-key=access-query-secret"
    )

    redacted = metadata.redact_runtime_value(value)

    for secret in (
        "api-env-secret",
        "x-api-secret",
        "aws-access-secret",
        "access-secret",
        "api-flag-secret",
        "x-api-flag-secret",
        "header-secret",
        "query-secret",
        "access-query-secret",
    ):
        assert secret not in redacted
    assert "https://rpc.example/path?api_key=REDACTED" in redacted
    assert "safe=visible" in redacted


def test_runtime_redaction_recurses_through_serialized_json_and_python_values():
    value = (
        'relation={"safe":"visible","nested":{"api_key":"json-secret"}} '
        "python={'credentials': {'token': 'python-secret'}, 'safe': 'kept'} "
        'quoted="{\\"password\\":\\"quoted-secret\\",\\"safe\\":\\"inside\\"}"'
    )

    redacted = metadata.redact_runtime_value(value)

    for secret in ("json-secret", "python-secret", "quoted-secret"):
        assert secret not in redacted
    assert "visible" in redacted
    assert "kept" in redacted
    assert "inside" in redacted


def test_runtime_redaction_covers_multi_word_api_key_header():
    value = "X-API-Key: three word secret\nX-Safe-Header: visible"

    redacted = metadata.redact_runtime_value(value)

    assert "three word secret" not in redacted
    assert "X-Safe-Header: visible" in redacted


def test_runtime_redaction_fails_closed_on_malformed_serialized_value():
    value = 'relation={"safe":"visible","api_key":"unterminated-secret"'

    redacted = metadata.redact_runtime_value(value)

    assert redacted == "REDACTED"
    assert "unterminated-secret" not in redacted


def test_runtime_redaction_does_not_treat_ipv6_or_help_brackets_as_serialized_values():
    value = "bitcoind -rpcbind=[::1]\nUsage: bitcoind [options]"

    assert metadata.redact_runtime_value(value) == value


def test_build_runtime_metadata_uses_rpc_identity_and_effective_process_flags(monkeypatch):
    responses = {
        "getnetworkinfo": {"subversion": "/Satoshi:31.1.0/", "protocolversion": 70016},
        "getblockchaininfo": {
            "chain": "main",
            "blocks": 962232,
            "headers": 962232,
            "bestblockhash": "00000000000000000001ea61",
            "initialblockdownload": False,
            "pruned": False,
        },
        "getblockhash": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
        "getindexinfo": {
            "txindex": {"synced": True, "best_block_height": 962232},
            "basic block filter index": {"synced": True, "best_block_height": 962232},
        },
    }
    calls = []

    def rpc_call(_config, method, params=None):
        calls.append(call(method, params or []))
        return responses[method]

    monkeypatch.setattr(metadata, "rpc_call", rpc_call)
    monkeypatch.setattr(
        metadata.utils,
        "get_client_proc_cmdline",
        lambda: (
            "/home/bitcoin/bitcoind -chain=main -server=1 -prune=0 -txindex=1 "
            "-blockfilterindex=1 -port=18333 -rpcport=8332 -zmqpubrawblock=tcp://127.0.0.1:28332 "
            "-rpcpassword=never-publish-this"
        ),
    )
    monkeypatch.setattr(
        metadata.utils,
        "get_systemd_limits",
        lambda _service: {"memory_max_bytes": 8589934592, "cpu_quota_percent": 400.0},
    )

    blockchain, sections = metadata.build_runtime_metadata(
        {"rpc-user": "alice", "rpc-password": "never-publish-this", "rpc-proxy-listen": "0.0.0.0:8331"}
    )

    assert calls == [
        call("getnetworkinfo", []),
        call("getblockchaininfo", []),
        call("getblockhash", [0]),
        call("getindexinfo", []),
    ]
    assert blockchain == {
        "blockchain_ecosystem": "bitcoin",
        "blockchain_network_name": "Bitcoin mainnet",
        "client_name": "bitcoin-core",
        "client_version": "31.1.0",
        "cmdline": (
            "/home/bitcoin/bitcoind -chain=main -server=1 -prune=0 -txindex=1 "
            "-blockfilterindex=1 -port=18333 -rpcport=8332 -zmqpubrawblock=tcp://127.0.0.1:28332 "
            "-rpcpassword=REDACTED"
        ),
        "binary_path": "/home/bitcoin/bitcoind",
        "genesis_hash": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    }
    bitcoin = sections["bitcoin"]
    assert bitcoin["network_magic"] == "f9beb4d9"
    assert bitcoin["effective_flags"]["txindex"] == "1"
    assert "rpcpassword" not in bitcoin["effective_flags"]
    assert bitcoin["pruning"] == {"enabled": False, "automatic": False, "target_bytes": None, "height": None}
    assert bitcoin["indexes"]["txindex"]["synced"] is True
    assert bitcoin["ports"] == {
        "p2p": 18333,
        "rpc_internal": 8332,
        "rpc_proxy": 8331,
        "zmq": [28332],
    }
    assert sections["resource_limits"]["memory_max_bytes"] == 8589934592


def test_collect_metadata_writes_redacted_payload_without_upload(monkeypatch, tmp_path):
    secret = "never-publish-this"
    charm = SimpleNamespace(
        model=SimpleNamespace(
            name="bitcoin-model",
            uuid="model-uuid",
            config={"collector-s3-credentials": "", "rpc-password": secret},
            relations={},
            get_secret=lambda **_kwargs: None,
        ),
        app=SimpleNamespace(name="bitcoin-rpc"),
        unit=SimpleNamespace(name="bitcoin-rpc/0"),
        meta=SimpleNamespace(name="bitcoin-rpc", relations={}),
        config={"rpc-password": secret},
    )
    blockchain = {
        "blockchain_ecosystem": "bitcoin",
        "blockchain_network_name": "Bitcoin mainnet",
        "client_name": "bitcoin-core",
        "client_version": "31.1.0",
        "cmdline": f"bitcoind -rpcpassword={secret}",
        "binary_path": "/home/bitcoin/bitcoind",
        "genesis_hash": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    }
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path)
    monkeypatch.setattr(
        metadata,
        "build_runtime_metadata",
        lambda _config: (
            blockchain,
            {"bitcoin": {"relation-data": f'relation={{"api_key":"{secret}","safe":"visible"}}'}},
        ),
    )

    error = metadata.collect_upload_metadata(charm)

    assert error is None
    payload = (tmp_path / "bitcoin-rpc-0.json").read_text()
    assert secret not in payload
    assert "collector-s3-credentials" not in payload
    assert '"client_version": "31.1.0"' in payload
    assert "visible" in payload


def test_collect_metadata_uses_secret_credentials_for_upload(monkeypatch, tmp_path):
    credentials = object()
    calls = []
    charm = SimpleNamespace(
        model=SimpleNamespace(
            name="bitcoin-model",
            uuid="model-uuid",
            config={"collector-s3-credentials": "secret:collector"},
            relations={},
        ),
        app=SimpleNamespace(name="bitcoin-rpc"),
        unit=SimpleNamespace(name="bitcoin-rpc/0"),
        meta=SimpleNamespace(name="bitcoin-rpc", relations={}),
        config={},
    )
    monkeypatch.setattr(metadata, "METADATA_DIR", tmp_path)
    monkeypatch.setattr(
        metadata,
        "build_runtime_metadata",
        lambda _config: (
            {
                "blockchain_ecosystem": "bitcoin",
                "blockchain_network_name": "Bitcoin mainnet",
                "client_name": "bitcoin-core",
                "client_version": "31.1.0",
                "cmdline": "bitcoind -chain=main",
                "binary_path": "/home/bitcoin/bitcoind",
                "genesis_hash": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
            },
            {"bitcoin": {}},
        ),
    )
    monkeypatch.setattr(
        metadata,
        "parse_credentials_secret_id",
        lambda model, secret_id: credentials if model is charm.model and secret_id == "secret:collector" else None,
    )
    monkeypatch.setattr(metadata, "collect_and_upload", lambda **kwargs: calls.append(kwargs))

    assert metadata.collect_upload_metadata(charm) is None
    assert len(calls) == 1
    assert calls[0]["credentials"] is credentials
    assert calls[0]["no_upload"] is False
    assert "collector-s3-credentials" not in calls[0]["model"].config


def test_collect_metadata_returns_safe_validation_error(monkeypatch):
    secret = "never-publish-this"
    charm = SimpleNamespace(
        model=SimpleNamespace(config={"collector-s3-credentials": "secret:collector"}),
        config={},
    )
    monkeypatch.setattr(
        metadata,
        "parse_credentials_secret_id",
        lambda *_args: (_ for _ in ()).throw(MetadataValidationError(f"invalid token {secret}")),
    )

    error = metadata.collect_upload_metadata(charm)

    assert error == "invalid collector-s3-credentials: invalid token REDACTED"
    assert secret not in error
