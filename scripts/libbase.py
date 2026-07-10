#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, TypeVar

ROOT = Path(os.environ.get("ROOT_DIR", Path(__file__).resolve().parents[1]))
WORK = ROOT / "work"
LOGS = ROOT / "logs"
WORK.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

T = TypeVar("T")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    line = f"[{utc_now_iso()}] {message}"
    print(line, flush=True)
    with (LOGS / "base-image.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run(command: list[str | Path], *, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    argv = [str(item) for item in command]
    log("exec: " + " ".join(argv))
    return subprocess.run(argv, check=check, text=True, **kwargs)


def cfg(name: str) -> dict:
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def parse_qemu_size(value: str) -> int:
    text = str(value).strip().upper()
    match = re.fullmatch(r"([1-9][0-9]*)([KMGTPE]?)B?", text)
    if not match:
        raise ValueError(f"invalid QEMU disk size: {value!r}; use values such as 220G")

    number = int(match.group(1))
    unit = match.group(2)
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}[unit]
    return number * (1024 ** power)



def storage_provider() -> str:
    storage = cfg("storage.json")
    provider = os.environ.get("STORAGE_PROVIDER", storage.get("default_provider", "backblaze")).strip().lower()
    if provider not in storage.get("providers", {}):
        supported = ", ".join(sorted(storage.get("providers", {})))
        raise RuntimeError(f"unsupported STORAGE_PROVIDER={provider!r}; supported providers: {supported}")
    return provider


def storage_context() -> dict[str, str]:
    storage = cfg("storage.json")
    provider = storage_provider()
    spec = storage["providers"][provider]

    def required_env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"required environment variable is missing: {name}")
        return value

    bucket = required_env(spec["bucket_env"])
    access_key = required_env(spec["access_key_env"])
    secret_key = required_env(spec["secret_key_env"])

    if provider == "oracle":
        region = required_env(spec["region_env"])
        namespace = required_env(spec["namespace_env"])
        endpoint = os.environ.get(spec["endpoint_env"], "").strip()
        if not endpoint:
            endpoint = f"https://{namespace}.compat.objectstorage.{region}.oci.customer-oci.com"
    else:
        endpoint = required_env(spec["endpoint_env"])
        configured_region = os.environ.get(spec.get("region_env", ""), "").strip()
        if configured_region:
            region = configured_region
        else:
            host = endpoint.split("://", 1)[-1].split("/", 1)[0]
            region = host[3:].split(".backblazeb2.com", 1)[0] if host.startswith("s3.") and ".backblazeb2.com" in host else spec.get("default_region", "us-east-1")

    if not endpoint.startswith(("https://", "http://")):
        raise RuntimeError(f"storage endpoint must include http:// or https://: {endpoint}")

    return {
        "provider": provider,
        "bucket": bucket,
        "endpoint": endpoint.rstrip("/"),
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
    }

def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def retry(
    max_attempts: int,
    initial_delay: int,
    max_delay: int,
    function: Callable[..., T],
    *args: object,
) -> T:
    attempt = 1
    delay = initial_delay
    while True:
        try:
            return function(*args)
        except Exception as exc:
            if attempt >= max_attempts:
                raise
            log(f"retry {attempt}/{max_attempts} after error: {exc}")
            time.sleep(delay)
            attempt += 1
            delay = min(delay * 2, max_delay)


def aws_env() -> dict[str, str]:
    context = storage_context()
    environment = os.environ.copy()
    environment["AWS_ACCESS_KEY_ID"] = context["access_key"]
    environment["AWS_SECRET_ACCESS_KEY"] = context["secret_key"]
    environment["AWS_DEFAULT_REGION"] = context["region"]
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    return environment


def endpoint() -> str:
    return storage_context()["endpoint"]


def s3_uri(object_name: str) -> str:
    storage = cfg("storage.json")
    context = storage_context()
    prefix = storage["remote_prefix"].strip("/")
    object_name = object_name.lstrip("/")
    return f"s3://{context['bucket']}/{prefix}/{object_name}" if prefix else f"s3://{context['bucket']}/{object_name}"


def aws_cp(source: str, destination: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["aws", "--endpoint-url", endpoint(), "s3", "cp", source, destination, "--only-show-errors"],
        env=aws_env(),
    )


def download(url: str, destination: str | Path) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and destination.stat().st_size > 0:
        log(f"using cached {destination}")
        return

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    log(f"downloading {url}")

    request = urllib.request.Request(url, headers={"User-Agent": "persistent-windows-github-vm/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def wait_for_poweroff(pidfile: str | Path, timeout_seconds: int) -> None:
    pidfile = Path(pidfile)
    if not pidfile.exists():
        raise RuntimeError(f"QEMU pid file not found: {pidfile}")

    pid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + timeout_seconds
    last_progress = 0.0

    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            log("QEMU exited after guest shutdown")
            return
        except PermissionError as exc:
            raise RuntimeError(f"cannot inspect QEMU process {pid}: {exc}") from exc

        elapsed = timeout_seconds - max(0, int(deadline - time.monotonic()))
        if elapsed - last_progress >= 300:
            log(f"Windows installer VM still running; elapsed={elapsed // 60} minutes")
            last_progress = float(elapsed)

        time.sleep(5)

    raise TimeoutError(f"Windows installer VM did not shut down within {timeout_seconds // 60} minutes")


def build(args: argparse.Namespace) -> None:
    vm = cfg("vm.json")
    checkpoint = cfg("checkpoint.json")

    requested_disk_bytes = parse_qemu_size(args.disk_size)
    if requested_disk_bytes < 81 * (1024 ** 3):
        raise SystemExit(
            f"disk size must be above 80G; received {args.disk_size}"
        )

    log(f"requested Windows virtual disk size: {args.disk_size}")

    windows_iso = WORK / "windows.iso"
    virtio_iso = WORK / "virtio-win.iso"
    base = ROOT / vm["base_image"]

    retry(
        checkpoint["download_retries"],
        checkpoint["retry_initial_seconds"],
        checkpoint["retry_max_seconds"],
        download,
        args.windows_iso_url,
        windows_iso,
    )
    retry(
        checkpoint["download_retries"],
        checkpoint["retry_initial_seconds"],
        checkpoint["retry_max_seconds"],
        download,
        args.virtio_iso_url,
        virtio_iso,
    )

    if args.windows_iso_sha256:
        actual = sha256(windows_iso)
        if actual.lower() != args.windows_iso_sha256.lower():
            raise SystemExit(f"Windows ISO checksum mismatch: expected={args.windows_iso_sha256} actual={actual}")

    if args.virtio_iso_sha256:
        actual = sha256(virtio_iso)
        if actual.lower() != args.virtio_iso_sha256.lower():
            raise SystemExit(f"VirtIO ISO checksum mismatch: expected={args.virtio_iso_sha256} actual={actual}")

    uefi_vars_source = Path(vm["uefi_vars_template"])
    uefi_vars_destination = ROOT / vm["uefi_vars"]
    uefi_vars_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(uefi_vars_source, uefi_vars_destination)

    base.parent.mkdir(parents=True, exist_ok=True)
    base.unlink(missing_ok=True)

    usage = shutil.disk_usage(base.parent)
    log(
        "host storage before image creation: "
        f"total={usage.total // (1024**3)}GiB "
        f"used={usage.used // (1024**3)}GiB "
        f"free={usage.free // (1024**3)}GiB"
    )

    run(["qemu-img", "create", "-f", "qcow2", base, args.disk_size])

    qmp_socket = ROOT / vm["qmp_socket"]
    pidfile = ROOT / vm["pid_file"]
    qmp_socket.unlink(missing_ok=True)
    pidfile.unlink(missing_ok=True)

    qemu_command: list[str | Path] = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-machine", "q35,accel=kvm",
        "-m", str(args.memory_mb),
        "-smp", str(args.cpu_cores),
        "-cpu", "host",
        "-drive", f"if=pflash,format=raw,readonly=on,file={vm['uefi_code']}",
        "-drive", f"if=pflash,format=raw,file={uefi_vars_destination}",
        "-drive", f"file={base},if=virtio,format=qcow2,cache=writeback,discard=unmap",
        "-cdrom", windows_iso,
        "-drive", f"if=none,id=virtiocd,file={virtio_iso},format=raw,media=cdrom,readonly=on",
        "-device", "ide-cd,drive=virtiocd",
        "-boot", "order=d,menu=on",
        "-netdev", "user,id=n0",
        "-device", "virtio-net-pci,netdev=n0",
        "-qmp", f"unix:{qmp_socket},server=on,wait=off",
        "-pidfile", pidfile,
        "-daemonize",
        "-vnc", "0.0.0.0:0",
        "-serial", f"file:{ROOT / vm['monitor_log']}",
    ]

    run(qemu_command)
    log("base image installer VM started")
    log("VNC is listening on TCP port 5900; install Windows manually and shut it down normally when finished")

    wait_for_poweroff(pidfile, args.install_timeout_minutes * 60)

    run(["qemu-img", "check", "-r", "leaks", base], check=False)

    compacted = base.with_suffix(".compact.qcow2")
    compacted.unlink(missing_ok=True)
    run(["qemu-img", "convert", "-p", "-O", "qcow2", "-c", base, compacted])
    compacted.replace(base)

    digest = sha256(base)
    (WORK / "base.sha256").write_text(f"{digest} {base.name}\n", encoding="utf-8")
    (WORK / "base-manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "created_utc": utc_now_iso(),
                "image": base.name,
                "sha256": digest,
                "disk_size": args.disk_size,
                "source": "github-actions-qemu-manual-vnc",
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    log(f"base image ready: {base} sha256={digest}")


def upload(_: argparse.Namespace) -> None:
    vm = cfg("vm.json")
    storage = cfg("storage.json")
    checkpoint = cfg("checkpoint.json")

    base = ROOT / vm["base_image"]
    checksum_file = WORK / "base.sha256"
    manifest = WORK / "base-manifest.json"

    if not base.exists():
        raise SystemExit(f"missing base image: {base}")

    digest = sha256(base)
    checksum_file.write_text(f"{digest} {base.name}\n", encoding="utf-8")

    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "created_utc": utc_now_iso(),
                    "image": base.name,
                    "sha256": digest,
                    "source": "github-actions-qemu-manual-vnc",
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    objects = [
        (storage["base_object"], base),
        ("base.sha256", checksum_file),
        ("base-manifest.json", manifest),
    ]

    for object_name, path in objects:
        retry(
            checkpoint["upload_retries"],
            checkpoint["retry_initial_seconds"],
            checkpoint["retry_max_seconds"],
            aws_cp,
            str(path),
            s3_uri(object_name),
        )

    verification_copy = WORK / "base.verify.qcow2"
    verification_copy.unlink(missing_ok=True)
    retry(
        checkpoint["download_retries"],
        checkpoint["retry_initial_seconds"],
        checkpoint["retry_max_seconds"],
        aws_cp,
        s3_uri(storage["base_object"]),
        str(verification_copy),
    )

    verified_digest = sha256(verification_copy)
    if verified_digest != digest:
        raise SystemExit(f"uploaded base verification failed: expected={digest} actual={verified_digest}")

    verification_copy.unlink(missing_ok=True)
    log(f"base image uploaded and verified using {storage_provider()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--windows-iso-url", required=True)
    build_parser.add_argument("--virtio-iso-url", required=True)
    build_parser.add_argument("--windows-iso-sha256", default="")
    build_parser.add_argument("--virtio-iso-sha256", default="")
    build_parser.add_argument("--disk-size", default="80G")
    build_parser.add_argument("--memory-mb", type=int, default=8192)
    build_parser.add_argument("--cpu-cores", type=int, default=4)
    build_parser.add_argument("--install-timeout-minutes", type=int, default=180)

    subparsers.add_parser("upload")

    args = parser.parse_args()
    if args.cmd == "build":
        build(args)
    else:
        upload(args)


if __name__ == "__main__":
    main()
