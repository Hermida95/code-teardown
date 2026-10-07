import functools
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from builders import DEFAULT_CONFIG, make_docker_tar
from inspect_docker_image import (DirStore, TarStore, inspect, main, normalize_instruction, redact)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inspect_docker_image.py"


def run(tar_path, workdir=None):
    return inspect(TarStore(tar_path), str(tar_path), workdir)


def kinds(report):
    return {s["kind"] for s in report["signals"]}


def config(**inner):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["config"].update(inner)
    return cfg


@pytest.mark.parametrize("layout", ["classic", "oci"])
def test_basic_parse_both_layouts(tmp_path, layout):
    report = run(make_docker_tar(tmp_path / "i.tar", layout))
    assert report["source"]["layout"] == layout
    assert report["image"]["tags"] == ["demo:latest"]
    assert report["image"]["architecture"] == "amd64"
    assert report["totals"]["layers"] == 2
    assert [h["layer_index"] for h in report["history"]] == [0, 1, None]
    assert report["layers"][0]["created_by"] == "COPY app.py /app.py"
    assert report["config"]["entrypoint"] == ["python", "app.py"]
    assert report["config"]["exposed_ports"] == ["8000/tcp"]
    assert report["layers"][0]["digest"].startswith("sha256:")


def test_env_secrets_are_masked(tmp_path):
    report = run(make_docker_tar(tmp_path / "i.tar"))
    env = {e["name"]: e for e in report["config"]["env"]}
    assert env["API_KEY"]["value"] == "<masked>" and env["PATH"]["value"].startswith("/usr")
    assert "sk-test-123" not in json.dumps(report)
    assert "secret_in_env" in kinds(report)


def test_config_and_instruction_signals(tmp_path):
    report = run(make_docker_tar(tmp_path / "i.tar"))
    found = kinds(report)
    assert {"runs_as_root", "no_healthcheck", "apt_no_cleanup"} <= found
    apt = next(s for s in report["signals"] if s["kind"] == "apt_no_cleanup")
    assert apt["ref"] == "history[1]"


def test_non_root_and_healthcheck_clear_signals(tmp_path):
    cfg = config(User="app", Healthcheck={"Test": ["CMD", "true"]}, Env=[])
    report = run(make_docker_tar(tmp_path / "i.tar", config=cfg))
    assert not {"runs_as_root", "no_healthcheck", "secret_in_env"} & kinds(report)


@pytest.mark.parametrize("raw,expected", [
    ("/bin/sh -c #(nop)  ENV PATH=/x", "ENV PATH=/x"),
    ("/bin/sh -c apt-get update", "RUN apt-get update"),
    ("RUN /bin/sh -c pip install x # buildkit", "RUN pip install x"),
    ("|1 TOKEN=abc /bin/sh -c make build # buildkit", "RUN make build"),
    ("COPY . /app # buildkit", "COPY . /app"),
])
def test_instruction_normalization(raw, expected):
    assert normalize_instruction(raw) == expected


def test_instruction_signals_catalog(tmp_path):
    history = [{"created_by": c, "empty_layer": True} for c in (
        "RUN curl -sSL https://x.sh | sh",
        "RUN apk add curl",
        "RUN pip install flask",
        "RUN chmod -R 777 /app",
        "ADD https://example.com/a.tgz /opt/",
        "COPY . /app",
        "RUN apt-get update",
        "ENV DB_PASSWORD=supersecretvalue",
        "RUN apt-get install -y x && rm -rf /var/lib/apt/lists/*",
    )]
    cfg = config()
    cfg["history"] = history
    report = run(make_docker_tar(tmp_path / "i.tar", config=cfg, layers=[{"a": "x"}]))
    found = {s["kind"] for s in report["signals"] if s["ref"].startswith("history")}
    assert {"curl_pipe_shell", "apk_no_cache", "pip_cache", "chmod_777", "add_remote_url",
            "copy_whole_context", "apt_update_alone", "secret_in_instruction"} <= found
    clean = [s for s in report["signals"] if s["ref"] == "history[8]"]
    assert clean == []                                  # cleaned-up apt install is not flagged
    assert "supersecretvalue" not in json.dumps(report)


def test_deleted_secret_still_in_earlier_layer(tmp_path):
    layers = [{"app/.env": "SECRET=1\n", "app/main.py": "x"}, {"app/.wh..env": ""}]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    signal = next(s for s in report["signals"] if s["kind"] == "deleted_secret_still_in_layer")
    assert signal["ref"] == "layer 0" and signal["path"] == "app/.env"
    assert report["layers"][0]["wasted_bytes"] == len("SECRET=1\n")
    assert report["layers"][1]["whiteouts"] == 1
    assert "sensitive_file" not in kinds(report)       # gone from the final image: only the precise signal remains


def test_secret_present_in_final_image(tmp_path):
    layers = [{"root/.ssh/id_rsa": "-----BEGIN " + "PRIVATE KEY-----", "etc/ssl/certs/ca.pem": "ok"}]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    sensitive = [s for s in report["signals"] if s["kind"] == "sensitive_file"]
    assert [s["path"] for s in sensitive] == ["root/.ssh/id_rsa"]        # CA bundles are not noise
    assert "BEGIN PRIVATE KEY" not in json.dumps(report)                  # content never read


def test_opaque_whiteout_and_overwrite_accounting(tmp_path):
    layers = [{"data/a.bin": "A" * 100, "data/b.bin": "B" * 50, "keep/c": "C" * 10},
              {"data/.wh..wh..opq": "", "data/new": "N", "keep/c": "D" * 10}]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    assert report["layers"][0]["wasted_bytes"] == 100 + 50 + 10
    assert report["filesystem"]["bytes"] == 1 + 10


def test_wasted_space_signal(tmp_path):
    layers = [{"big.bin": "x" * 2_000_000}, {"big.bin": "y"}]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    assert "wasted_space" in kinds(report)
    assert report["totals"]["wasted_bytes"] == 2_000_000


def test_setuid_and_os_release_and_runtimes(tmp_path):
    layers = [{"usr/bin/su": ("x", 0o4755), "etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 12"\nID=debian\nVERSION_ID="12"\n',
               "usr/local/bin/python3": "x", "usr/bin/gcc": "x"}]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    assert report["filesystem"]["setuid_files"] == [{"path": "usr/bin/su", "layer": 0}]
    assert report["base_os"]["name"] == "Debian GNU/Linux 12" and report["base_os"]["ref"] == "layer 0: etc/os-release"
    assert report["filesystem"]["runtimes"] == ["python"]
    assert "build_tools_in_final_image" in kinds(report)


def test_app_files_extracted_safely(tmp_path):
    layers = [{"app/main.py": "print(1)\n", "app/node_modules/x/index.js": "x", "usr/lib/foo.py": "x",
               "app/../../escape.py": "evil", "app/blob.bin": b"\0\0\0\0", "app/gone.py": "bye"},
              {"app/.wh.gone.py": ""}]
    work = tmp_path / "work"
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers), work)
    assert report["extracted"]["files"] == ["app/main.py"]
    assert (work / "image-files" / "app" / "main.py").read_text() == "print(1)\n"
    assert not (work / "image-files" / "app" / "gone.py").exists()
    assert not (tmp_path / "escape.py").exists() and not (work / "escape.py").exists()


def test_no_extraction_when_workdir_is_none(tmp_path):
    report = run(make_docker_tar(tmp_path / "i.tar", layers=[{"app/main.py": "x"}]))
    assert report["extracted"] == {"dir": None, "files": []}


def test_unreadable_layer_is_declared_not_fatal(tmp_path):
    layers = [{"app/a.py": "x"}, b"this is not a tar archive at all" * 40]
    report = run(make_docker_tar(tmp_path / "i.tar", layers=layers))
    assert "error" in report["layers"][1]
    assert any("layer 1 could not be read" in item for item in report["limits"])
    assert report["layers"][0]["files"] == 1


def test_extracted_directory_input(tmp_path):
    tar_path = make_docker_tar(tmp_path / "i.tar", "oci")
    out = tmp_path / "unpacked"
    with tarfile.open(tar_path) as tf:
        tf.extractall(out, filter="data")
    report = inspect(DirStore(out), str(out), None)
    assert report["source"]["layout"] == "oci" and report["totals"]["layers"] == 2


def test_not_an_image_tar(tmp_path):
    bad = tmp_path / "x.tar"
    with tarfile.open(bad, "w") as tf:
        info = tarfile.TarInfo("hello.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    proc = subprocess.run([sys.executable, str(SCRIPT), str(bad), "--no-extract"], capture_output=True, text=True)
    assert proc.returncode == 3 and "not a docker save archive" in proc.stderr


def fake_docker(bin_dir: Path, source_tar: Path | None, exit_code: int = 0) -> None:
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "docker"
    if source_tar:
        body = f'#!/bin/sh\n[ "$1" = save ] || exit 9\ncp {source_tar} "$3"\n'
    else:
        body = f'#!/bin/sh\necho "Cannot connect to the Docker daemon" >&2\nexit {exit_code}\n'
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def test_image_reference_uses_docker_save_only(tmp_path, monkeypatch, capsys):
    tar_path = make_docker_tar(tmp_path / "src.tar")
    fake_docker(tmp_path / "bin", tar_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert main(["x", "demo:latest", "--no-extract"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"]["saved_with_docker"] is True and out["image"]["tags"] == ["demo:latest"]


def test_docker_daemon_down_is_a_clean_error(tmp_path, monkeypatch, capsys):
    fake_docker(tmp_path / "bin", None, exit_code=1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert main(["x", "demo:latest", "--no-extract"]) == 3
    assert "docker save failed" in capsys.readouterr().err


def test_docker_missing_is_a_clean_error(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert main(["x", "demo:latest", "--no-extract"]) == 3
    assert "docker CLI not found" in capsys.readouterr().err


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:        # a daemon that is starting or stopping can hang `docker info`; never wait for it
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except subprocess.TimeoutExpired:
        return False


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_real_image_via_docker_import(tmp_path):
    """Builds an image with `docker import` (no container is ever started)."""
    rootfs = tmp_path / "rootfs.tar"
    with tarfile.open(rootfs, "w") as tf:
        data = b"print('hello')\n"
        info = tarfile.TarInfo("app/hello.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    tag = "code-teardown-test:latest"
    subprocess.run(["docker", "import", str(rootfs), tag], check=True, capture_output=True)
    try:
        proc = subprocess.run([sys.executable, str(SCRIPT), tag, "--out", str(tmp_path / "work")],
                              capture_output=True, text=True, check=True)
    finally:
        subprocess.run(["docker", "rmi", tag], capture_output=True)
    report = json.loads(proc.stdout)
    assert report["source"]["saved_with_docker"] is True
    assert report["extracted"]["files"] == ["app/hello.py"]


@pytest.mark.parametrize("raw,leaked", [
    ("RUN mysql --password=hunter2hunter2 -e 'select 1'", "hunter2hunter2"),
    ("ENV API_TOKEN=\"abc def ghi\"", "abc def ghi"),
    ("RUN git clone https://ghp_abcdefghij1234567890@github.com/x/y", "ghp_abcdefghij1234567890"),
    ("ARG SECRET_KEY: s3cr3tvalue99", "s3cr3tvalue99"),
])
def test_redact_hides_values_but_keeps_names(raw, leaked):
    out = redact(raw)
    assert leaked not in out and "<redacted>" in out


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_real_multilayer_build_history_and_secret_redaction(tmp_path):
    """A real `docker build` (COPY/ENV/USER only, so nothing is executed) checks Docker's own history format."""
    ctx = tmp_path / "ctx"
    (ctx / "app").mkdir(parents=True)
    (ctx / "app" / "main.py").write_text("print('hi')\n")
    (ctx / "requirements.txt").write_text("requests\n")
    (ctx / "Dockerfile").write_text(
        "FROM scratch\nCOPY app/ /app/\nCOPY requirements.txt /app/requirements.txt\n"
        "ENV SERVICE_TOKEN=fake-token-value-123456\nUSER 1000\nENTRYPOINT [\"python\", \"app/main.py\"]\n")
    tag = "code-teardown-test-multilayer:latest"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(ctx)], check=True, capture_output=True)
    try:
        proc = subprocess.run([sys.executable, str(SCRIPT), tag, "--out", str(tmp_path / "work")],
                              capture_output=True, text=True, check=True)
    finally:
        subprocess.run(["docker", "rmi", tag], capture_output=True)
    report = json.loads(proc.stdout)
    assert report["totals"]["layers"] == 2
    instructions = [h["instruction"] for h in report["history"]]
    assert "COPY app/ /app/" in instructions
    assert any(i.startswith("ENV SERVICE_TOKEN=<redacted>") for i in instructions)
    assert "fake-token-value-123456" not in proc.stdout
    kinds = {s["kind"] for s in report["signals"]}
    assert {"secret_in_env", "secret_in_instruction"} <= kinds and "runs_as_root" not in kinds
    assert report["config"]["user"] == "1000"
    assert "app/main.py" in report["extracted"]["files"]
