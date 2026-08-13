"""Bitcoin Core metadata collection and redaction helpers."""

import ast
import json
import re
import shlex
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import requests
from charms.dwellir.blockchain_common.v1.metadata import (
    MetadataUploadError,
    MetadataValidationError,
    collect_and_upload,
    parse_credentials_secret_id,
)

import constants as c
import utils

_SENSITIVE_KEY_PARTS = {
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "privatekey",
    "rpcauth",
    "rpcpassword",
    "rpcuser",
    "secret",
    "sessiontoken",
    "token",
}

_NETWORKS = {
    "main": {"name": "Bitcoin mainnet", "magic": "f9beb4d9", "p2p": 8333, "rpc": 8332},
    "test": {"name": "Bitcoin testnet3", "magic": "0b110907", "p2p": 18333, "rpc": 18332},
    "testnet4": {"name": "Bitcoin testnet4", "magic": "1c163f28", "p2p": 48333, "rpc": 48332},
    "signet": {"name": "Bitcoin signet", "magic": "0a03cf40", "p2p": 38333, "rpc": 38332},
    "regtest": {"name": "Bitcoin regtest", "magic": "fabfb5da", "p2p": 18444, "rpc": 18443},
}

METADATA_DIR = Path("/tmp/dwellir-metadata-uploader")


def _key_is_sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _parse_serialized(value: str) -> Any | None:
    """Parse nested JSON or Python literals without executing input."""
    candidate: Any = value
    for _ in range(3):
        if not isinstance(candidate, str):
            break
        stripped = candidate.strip()
        try:
            candidate = json.loads(stripped)
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            candidate = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return None
    return candidate if isinstance(candidate, (Mapping, list, tuple)) else None


def _quoted_char_state(char: str, quote: str, escaped: bool) -> tuple[str, bool, bool]:
    """Return updated quote state and whether the character was consumed."""
    if escaped:
        return quote, False, True
    if char == "\\":
        return quote, True, True
    if quote:
        return ("" if char == quote else quote), False, True
    if char in "'\"":
        return char, False, True
    return quote, False, False


def _container_end(value: str, start: int) -> int | None:
    """Find one serialized container's end while respecting quoted content."""
    pairs = {"{": "}", "[": "]"}
    stack = [pairs[value[start]]]
    quote = ""
    escaped = False
    for index in range(start + 1, len(value)):
        char = value[index]
        quote, escaped, consumed = _quoted_char_state(char, quote, escaped)
        if consumed:
            continue
        if char in pairs:
            stack.append(pairs[char])
            continue
        if char not in "}]":
            continue
        if char != stack[-1]:
            return None
        stack.pop()
        if not stack:
            return index
    return None


def _looks_serialized_start(value: str, start: int) -> bool:
    """Distinguish data containers from IPv6 and human help notation."""
    remainder = value[start + 1 :].lstrip()
    if not remainder:
        return True
    first = remainder[0]
    if value[start] == "{":
        return first in "'\"\\}"
    return first in "'\"\\[{]-0123456789" or remainder.startswith(("true", "false", "null", "True", "False", "None"))


def _redact_serialized_fragments(value: str) -> str | None:
    """Redact serialized containers embedded in runtime strings."""
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        starts = [position for position in (value.find("{", cursor), value.find("[", cursor)) if position >= 0]
        if not starts:
            output.append(value[cursor:])
            break
        start = min(starts)
        if not _looks_serialized_start(value, start):
            output.append(value[cursor : start + 1])
            cursor = start + 1
            continue
        output.append(value[cursor:start])
        end = _container_end(value, start)
        if end is None:
            return None

        parse_start, parse_end = start, end + 1
        if start > 0 and end + 1 < len(value) and value[start - 1] in "'\"" and value[end + 1] == value[start - 1]:
            parse_start -= 1
            parse_end += 1
        parsed = _parse_serialized(value[parse_start:parse_end])
        if parsed is None:
            return None
        output.append(json.dumps(_redact_value(parsed), separators=(",", ":"), sort_keys=True))
        cursor = parse_end
    return "".join(output)


def redact_runtime_value(value: str) -> str:
    """Redact credentials from command lines, environment values, and URLs."""
    structured = _redact_serialized_fragments(value)
    if structured is None:
        return "REDACTED"
    redacted = re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1REDACTED@", structured)
    redacted = re.sub(
        r"(?im)^(\s*([a-z][a-z0-9_.-]*)\s*:\s*)([^\r\n]*)$",
        lambda match: f"{match.group(1)}REDACTED" if _key_is_sensitive(match.group(2)) else match.group(0),
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\bauthorization\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+(?:\s+[^\s,}]+)?)",
        r"\1REDACTED",
        redacted,
    )
    value_pattern = r'(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;}&#]+)'

    def redact_flag(match: re.Match[str]) -> str:
        if _key_is_sensitive(match.group(2)):
            return f"{match.group(1)}{match.group(2)}{match.group(3)}REDACTED"
        return match.group(0)

    redacted = re.sub(
        rf"(?i)(?<![a-z0-9_.-])(--?)([a-z0-9_.-]+)(=|\s+)({value_pattern})",
        redact_flag,
        redacted,
    )

    def redact_query(match: re.Match[str]) -> str:
        if _key_is_sensitive(match.group(2)):
            return f"{match.group(1)}{match.group(2)}{match.group(3)}REDACTED"
        return match.group(0)

    redacted = re.sub(r"(?i)([?&])([a-z0-9_.-]+)(=)([^&#\s]*)", redact_query, redacted)

    def redact_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}REDACTED" if _key_is_sensitive(match.group(1)) else match.group(0)

    redacted = re.sub(
        rf"(?i)(?<![a-z0-9_.-])([a-z][a-z0-9_.-]*)(\s*[:=]\s*)({value_pattern})",
        redact_assignment,
        redacted,
    )
    redacted = re.sub(
        r"(?i)((?<![a-z0-9_.-])\b(?:password|secret|token|credential|cookie|rpcauth|rpcuser|private key)\b\s*[:=]?\s+)([^\s,;}]+)",
        r"\1REDACTED",
        redacted,
    )
    return redacted


def _redact_value(value: Any, key: str = "") -> Any:
    if _key_is_sensitive(key):
        return "REDACTED"
    if isinstance(value, Mapping):
        return {str(child_key): _redact_value(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_value(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(child) for child in value)
    if isinstance(value, str):
        return redact_runtime_value(value)
    return value


def redact_output(value: Any) -> Any:
    """Apply recursive redaction at a final payload or action boundary."""
    return _redact_value(value)


class _RedactedRelation:
    """Expose relation topology with recursively redacted databags."""

    def __init__(self, relation: Any) -> None:
        self.id = relation.id
        self.app = relation.app
        self.units = relation.units
        self.data = {endpoint: _redact_value(databag) for endpoint, databag in relation.data.items()}


class RedactedModelView:
    """Expose a metadata model view without mutating the Juju model."""

    def __init__(self, model: Any) -> None:
        self.name = model.name
        self.uuid = model.uuid
        self.config = {
            str(key): _redact_value(value, str(key))
            for key, value in dict(model.config).items()
            if key != "collector-s3-credentials"
        }
        self.relations = {
            name: [_RedactedRelation(relation) for relation in relations] for name, relations in model.relations.items()
        }


def rpc_call(config: Mapping[str, Any], method: str, params: list[Any] | None = None) -> Any:
    """Call Bitcoin Core over its loopback JSON-RPC endpoint."""
    response = requests.post(
        f"http://127.0.0.1:{c.BITCOIND_RPC_PORT}",
        auth=(str(config.get("rpc-user") or ""), str(config.get("rpc-password") or "")),
        json={"jsonrpc": "2.0", "id": "metadata", "method": method, "params": params or []},
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(redact_runtime_value(f"{method} failed: {body['error']}"))
    return body.get("result")


def _add_flag(flags: dict[str, Any], name: str, value: str) -> None:
    if _key_is_sensitive(name):
        return
    existing = flags.get(name)
    if existing is None:
        flags[name] = value
    elif isinstance(existing, list):
        existing.append(value)
    else:
        flags[name] = [existing, value]


def parse_effective_flags(cmdline: str) -> dict[str, Any]:
    """Parse effective Bitcoin Core flags while omitting credential options."""
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    flags: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            index += 1
            continue
        raw = token.lstrip("-")
        if "=" in raw:
            name, value = raw.split("=", 1)
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            name, value = raw, tokens[index + 1]
            index += 1
        else:
            name, value = raw, "1"
        _add_flag(flags, name.casefold(), value)
        index += 1
    return flags


def _first_flag(flags: Mapping[str, Any], name: str, default: Any = None) -> Any:
    value = flags.get(name, default)
    return value[-1] if isinstance(value, list) and value else value


def _int_flag(flags: Mapping[str, Any], name: str, default: int) -> int:
    try:
        return int(_first_flag(flags, name, default))
    except (TypeError, ValueError):
        return default


def _proxy_port(config: Mapping[str, Any]) -> int | None:
    listen = str(config.get("rpc-proxy-listen") or "")
    try:
        return int(listen.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _zmq_ports(flags: Mapping[str, Any]) -> list[int]:
    ports: set[int] = set()
    for name, raw in flags.items():
        if not name.startswith("zmqpub"):
            continue
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            try:
                port = urlparse(str(value)).port
            except ValueError:
                port = None
            if port is not None:
                ports.add(port)
    return sorted(ports)


def _client_version(subversion: str) -> str:
    match = re.search(r"Satoshi:([^/]+)/?", subversion)
    if match:
        return match.group(1)
    return utils.get_version().removeprefix("v") or "unknown"


def build_runtime_metadata(config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build Hedgehog metadata from Bitcoin Core RPC and effective runtime state."""
    network = rpc_call(config, "getnetworkinfo")
    chain = rpc_call(config, "getblockchaininfo")
    genesis_hash = rpc_call(config, "getblockhash", [0])
    indexes = rpc_call(config, "getindexinfo")
    if not isinstance(network, Mapping) or not isinstance(chain, Mapping) or not isinstance(indexes, Mapping):
        raise RuntimeError("Bitcoin Core returned invalid metadata response types")

    cmdline = utils.get_client_proc_cmdline() or utils.get_service_args()
    flags = parse_effective_flags(cmdline)
    chain_name = str(chain.get("chain") or "unknown")
    defaults = _NETWORKS.get(chain_name, {"name": f"Bitcoin {chain_name}", "magic": None, "p2p": 0, "rpc": 0})
    pruned = bool(chain.get("pruned", False))
    blockchain = {
        "blockchain_ecosystem": "bitcoin",
        "blockchain_network_name": defaults["name"],
        "client_name": "bitcoin-core",
        "client_version": _client_version(str(network.get("subversion") or "")),
        "cmdline": redact_runtime_value(cmdline),
        "binary_path": str(c.BINARY_PATH),
        "genesis_hash": str(genesis_hash),
    }
    sections = {
        "bitcoin": {
            "chain": chain_name,
            "network_magic": defaults["magic"],
            "protocol_version": network.get("protocolversion"),
            "blocks": chain.get("blocks"),
            "headers": chain.get("headers"),
            "best_block_hash": chain.get("bestblockhash"),
            "initial_block_download": chain.get("initialblockdownload"),
            "effective_flags": flags,
            "pruning": {
                "enabled": pruned,
                "automatic": bool(chain.get("automatic_pruning", False)) if pruned else False,
                "target_bytes": chain.get("prune_target_size") if pruned else None,
                "height": chain.get("pruneheight") if pruned else None,
            },
            "indexes": dict(indexes),
            "ports": {
                "p2p": _int_flag(flags, "port", int(defaults["p2p"])),
                "rpc_internal": _int_flag(flags, "rpcport", int(defaults["rpc"])),
                "rpc_proxy": _proxy_port(config),
                "zmq": _zmq_ports(flags),
            },
        },
        "resource_limits": utils.get_systemd_limits(c.SERVICE_NAME),
    }
    return _redact_value(blockchain), _redact_value(sections)


def collect_upload_metadata(charm: Any) -> str | None:
    """Write local Bitcoin metadata and optionally upload it through a Juju secret."""
    secret_id = charm.model.config.get("collector-s3-credentials")
    try:
        credentials = parse_credentials_secret_id(charm.model, str(secret_id)) if secret_id else None
        blockchain, sections = build_runtime_metadata(charm.config)
        collect_and_upload(
            model=RedactedModelView(charm.model),
            app=charm.app,
            unit=charm.unit,
            meta=charm.meta,
            base_dir=METADATA_DIR,
            blockchain=redact_output(blockchain),
            sections=redact_output(sections),
            credentials=credentials,
            no_upload=credentials is None,
        )
    except MetadataValidationError as exc:
        prefix = "invalid collector-s3-credentials" if secret_id else "invalid blockchain metadata"
        return f"{prefix}: {redact_runtime_value(str(exc))}"
    except MetadataUploadError as exc:
        return f"metadata upload failed: {redact_runtime_value(str(exc))}"
    except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
        return f"metadata collection failed: {redact_runtime_value(str(exc))}"
    return None
