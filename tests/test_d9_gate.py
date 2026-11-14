import io
import tarfile
from hashlib import sha256

import pytest

from tcrl.eval import d9_gate


def test_d9_approved_archive_requires_exact_manifest_and_archive(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"{}")
    live = tmp_path / "missing"
    archive = tmp_path / "gate.tar.gz"
    members = {
        "gate.json": b'{"pass": false}',
        "exit.txt": b"0\n",
        "direct_e0_manifest.sha256": sha256(manifest.read_bytes()).hexdigest().encode(),
    }
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(f"gate/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    monkeypatch.setattr(d9_gate, "APPROVED_ARCHIVE_SHA256", sha256(archive.read_bytes()).hexdigest())
    assert "original gate remains false" in d9_gate.validate_gate(manifest, live, archive)

    manifest.write_bytes(b'{"changed": true}')
    with pytest.raises(ValueError, match="current frozen dataset"):
        d9_gate.validate_gate(manifest, live, archive)

    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="unchanged gate archive"):
        d9_gate.validate_gate(manifest, live, archive)


def test_d9_passing_live_gate_needs_no_archive(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"{}")
    (tmp_path / "direct_e0_manifest.sha256").write_text(sha256(manifest.read_bytes()).hexdigest())
    (tmp_path / "exit.txt").write_text("0")
    (tmp_path / "gate.json").write_text('{"pass": true}')
    assert "Passed live" in d9_gate.validate_gate(manifest, tmp_path, tmp_path / "missing")
