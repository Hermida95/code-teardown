"""Builders for small synthetic artifacts used across the test suite.

Everything is generated at test time so no binary fixtures live in git and no
Docker daemon is needed.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path


def make_zip(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _layer_bytes(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            raw = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def _add(tf: tarfile.TarFile, name: str, raw: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    tf.addfile(info, io.BytesIO(raw))


DEFAULT_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {
        "User": "",
        "Env": ["PATH=/usr/local/bin:/usr/bin", "API_KEY=sk-test-123"],
        "Entrypoint": ["python", "app.py"],
        "Cmd": None,
        "ExposedPorts": {"8000/tcp": {}},
    },
    "history": [
        {"created_by": "COPY app.py /app.py", "empty_layer": False},
        {"created_by": "RUN apt-get update && apt-get install -y curl", "empty_layer": False},
        {"created_by": "ENTRYPOINT [\"python\" \"app.py\"]", "empty_layer": True},
    ],
}


def make_docker_tar(path: Path, layout: str = "classic", layers: list[dict] | None = None,
                    config: dict | None = None, tag: str = "demo:latest") -> Path:
    """Build a docker-save style tar. layout is 'classic' or 'oci'."""
    layers = layers or [
        {"app.py": "print('hi')\n"},
        {"var/lib/apt/lists/x": "cache" * 100},
    ]
    config = dict(config or DEFAULT_CONFIG)
    layer_blobs = [_layer_bytes(files) for files in layers]
    diff_ids = ["sha256:" + hashlib.sha256(b).hexdigest() for b in layer_blobs]
    config["rootfs"] = {"type": "layers", "diff_ids": diff_ids}
    config_raw = json.dumps(config).encode()
    config_hash = hashlib.sha256(config_raw).hexdigest()

    with tarfile.open(path, "w") as tf:
        if layout == "classic":
            layer_names = []
            for index, blob in enumerate(layer_blobs):
                name = f"{index:02d}deadbeef/layer.tar"
                layer_names.append(name)
                _add(tf, name, blob)
            _add(tf, f"{config_hash}.json", config_raw)
            manifest = [{"Config": f"{config_hash}.json", "RepoTags": [tag], "Layers": layer_names}]
            _add(tf, "manifest.json", json.dumps(manifest).encode())
        else:
            layer_descs = []
            for blob in layer_blobs:
                digest = hashlib.sha256(blob).hexdigest()
                _add(tf, f"blobs/sha256/{digest}", blob)
                layer_descs.append({"mediaType": "application/vnd.oci.image.layer.v1.tar",
                                    "digest": f"sha256:{digest}", "size": len(blob)})
            _add(tf, f"blobs/sha256/{config_hash}", config_raw)
            manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                   "digest": f"sha256:{config_hash}", "size": len(config_raw)},
                        "layers": layer_descs}
            manifest_raw = json.dumps(manifest).encode()
            manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
            _add(tf, f"blobs/sha256/{manifest_hash}", manifest_raw)
            index = {"schemaVersion": 2, "manifests": [{
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_hash}", "size": len(manifest_raw),
                "annotations": {"io.containerd.image.name": tag}}]}
            _add(tf, "index.json", json.dumps(index).encode())
            _add(tf, "oci-layout", b'{"imageLayoutVersion": "1.0.0"}')
    return path
