"""Validate live or archived E0 evidence before starting the D9 methods run."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import tarfile


# User-approved continuation for this exact run, whose original pass flag is false.
APPROVED_ARCHIVE_SHA256 = "6104f73638302e63476ce8d15601d981047a7d4659ca46bbbd5214da8207872b"


def validate_gate(manifest, live, archive):
    expected = sha256(manifest.read_bytes()).hexdigest()
    if (live / "direct_e0_manifest.sha256").is_file() and (live / "exit.txt").is_file():
        if ((live / "direct_e0_manifest.sha256").read_text().strip() == expected
                and (live / "exit.txt").read_text().strip() == "0"
                and json.loads((live / "gate.json").read_bytes()).get("pass") is True):
            return "Passed live direct E0 gate."
    if not archive.is_file() or sha256(archive.read_bytes()).hexdigest() != APPROVED_ARCHIVE_SHA256:
        raise ValueError("A passing live gate or the approved, unchanged gate archive is required.")
    with tarfile.open(archive, "r:gz") as tar:
        def read(name):
            with tar.extractfile("gate/" + name) as handle:
                return handle.read()
        actual = read("direct_e0_manifest.sha256").decode().strip()
        exit_code = read("exit.txt").strip()
        result = json.loads(read("gate.json"))
    if actual != expected or exit_code != b"0" or result.get("pass") is not False:
        raise ValueError("Approved archive does not match the current frozen dataset and expected original result.")
    return "Approved post-gate continuation (128/196 conversion); original gate remains false."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("live", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        print(validate_gate(args.manifest, args.live, args.archive))
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
