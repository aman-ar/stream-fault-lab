"""Download a pinned official release into this checkout; no global install."""
import hashlib
import io
import os
from pathlib import Path
import platform
import subprocess
import tarfile

VERSION = "v1.21.2"
# Digests published by the official GitHub release API, verified 2026-10-09.
DIGESTS = {
    ("Darwin", "arm64"): "7d0d0c5c11527cbd10bde1c3d728864eb0334a9b653566cd73c0259750cbf093",
    ("Darwin", "amd64"): "87a00062b6a588aa1158f875504a3b56c80d62e2611dc450b83d0534e1b49198",
    ("Linux", "arm64"): "180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f",
    ("Linux", "amd64"): "121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27",
}


def main():
    system = platform.system()
    machine = platform.machine().lower()
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(machine, machine)
    expected = DIGESTS.get((system, arch))
    if not expected:
        raise SystemExit(f"Unsupported platform: {system}/{machine}. Use macOS arm64 or amd64.")
    root = Path(__file__).resolve().parents[1]
    target = root / ".runtime" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    binary = target / "mediamtx"
    stamp = target / "version"
    if binary.is_file() and stamp.is_file() and stamp.read_text().strip() == VERSION:
        return
    name = f"mediamtx_{VERSION}_{system.lower()}_{arch}.tar.gz"
    url = f"https://github.com/bluenviron/mediamtx/releases/download/{VERSION}/{name}"
    print(f"Downloading MediaMTX {VERSION} for {system}/{arch}…", flush=True)
    # macOS curl uses the system trust configuration (including corporate CAs).
    # TLS verification stays enabled; the pinned digest is a second check.
    data = subprocess.check_output(["curl", "-fLsS", "--retry", "2", "--max-time", "120", url])
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("MediaMTX checksum did not match; binary was not installed.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        # Read only these files; never extract arbitrary archive paths.
        for name in ("mediamtx", "LICENSE", "mediamtx.yml"):
            member = archive.getmember(name)
            if not member.isfile():
                raise SystemExit(f"Unexpected archive member: {name}")
            stream = archive.extractfile(member)
            assert stream is not None
            destination = target / name
            temporary = target / f"{name}.tmp"
            temporary.write_bytes(stream.read())
            temporary.replace(destination)
    os.chmod(binary, 0o755)
    stamp.write_text(VERSION + "\n")


if __name__ == "__main__":
    main()
