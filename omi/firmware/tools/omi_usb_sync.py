#!/usr/bin/env python3
"""Pull Omi DK2 SD-card audio over the firmware USB bulk sync protocol.

The firmware sends raw 440-byte storage blocks. This tool converts those blocks
to the same length-prefixed WAL .bin chunks that the app uploads to the backend.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import select
import struct
import sys
import termios
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only nicety
    fcntl = None


DEFAULT_OUTPUT_DIR = Path("local-notes/dk2-usb-pulls")
DEFAULT_USB_CHUNK_BYTES = 440 * 297
STORAGE_BLOCK_SIZE = 440
DEFAULT_FPS = 100
DEFAULT_FRAME_SIZE = 160
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_MIN_CHUNK_SECONDS = 10
VALID_OPUS_TOC = {0xB8, 0xB0, 0xBC, 0xF8, 0xFC, 0x78, 0x7C}


@dataclass(frozen=True)
class DeviceStat:
    total: int
    offset: int
    file_num: int
    block_size: int

    @property
    def available(self) -> int:
        return max(0, self.total - self.offset)


class UsbProtocolError(RuntimeError):
    pass


class UsbSerial:
    def __init__(self, port: str, timeout: float = 5.0, baud: int = 921600):
        self.port = port
        self.timeout = timeout
        self.baud = baud
        self.fd: int | None = None
        self.rx_buffer = bytearray()

    def __enter__(self) -> "UsbSerial":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[3] = 0
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        baud_const = self._baud_const(self.baud)
        attrs[4] = baud_const
        attrs[5] = baud_const
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self._assert_modem_lines()

    @staticmethod
    def _baud_const(baud: int) -> int:
        const = getattr(termios, f"B{baud}", None)
        if const is None:
            raise ValueError(f"unsupported baud rate on this system: {baud}")
        return const

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _assert_modem_lines(self) -> None:
        if fcntl is None or self.fd is None:
            return
        if not all(hasattr(termios, name) for name in ("TIOCMBIS", "TIOCM_DTR", "TIOCM_RTS")):
            return
        bits = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
        try:
            fcntl.ioctl(self.fd, termios.TIOCMBIS, bits)
        except OSError:
            pass

    def write_line(self, line: str) -> None:
        if self.fd is None:
            raise UsbProtocolError("USB serial port is not open")
        payload = (line.rstrip("\n") + "\n").encode("ascii")
        os.write(self.fd, payload)

    def read_line(self, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            newline_index = self.rx_buffer.find(b"\n")
            if newline_index >= 0:
                line = bytes(self.rx_buffer[:newline_index])
                del self.rx_buffer[: newline_index + 1]
                return line.decode("ascii", errors="replace").strip()
            chunk = self._read_available(deadline)
            if chunk:
                self.rx_buffer.extend(chunk)
        raise TimeoutError(f"timed out waiting for a line from {self.port}")

    def read_exact(self, size: int, timeout: float | None = None) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while len(data) < size and time.monotonic() < deadline:
            if self.rx_buffer:
                take = min(size - len(data), len(self.rx_buffer))
                data.extend(self.rx_buffer[:take])
                del self.rx_buffer[:take]
                continue
            chunk = self._read_available(deadline, size - len(data))
            if chunk:
                data.extend(chunk)
        if len(data) != size:
            raise TimeoutError(f"timed out reading {size} bytes from {self.port}; got {len(data)}")
        return bytes(data)

    def _read_available(self, deadline: float, max_bytes: int = 4096) -> bytes:
        if self.fd is None:
            raise UsbProtocolError("USB serial port is not open")
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([self.fd], [], [], min(remaining, 0.1))
        if not readable:
            return b""
        try:
            return os.read(self.fd, max_bytes)
        except BlockingIOError:
            return b""


class OmiUsbDevice:
    def __init__(self, serial: UsbSerial):
        self.serial = serial

    def ping(self) -> str:
        self.serial.write_line("PING")
        line = self.serial.read_line()
        if not line.startswith("OK OMIUSB "):
            raise UsbProtocolError(f"unexpected PING response: {line}")
        return line

    def stat(self) -> DeviceStat:
        self.serial.write_line("STAT")
        line = self.serial.read_line()
        parts = line.split()
        if len(parts) != 5 or parts[0] != "STAT":
            raise UsbProtocolError(f"unexpected STAT response: {line}")
        return DeviceStat(total=int(parts[1]), offset=int(parts[2]), file_num=int(parts[3]), block_size=int(parts[4]))

    def read(self, offset: int, length: int) -> bytes:
        self.serial.write_line(f"READ {offset} {length}")
        header = self.serial.read_line()
        parts = header.split()
        if len(parts) != 3 or parts[0] != "DATA":
            raise UsbProtocolError(f"unexpected READ response: {header}")
        response_offset = int(parts[1])
        response_length = int(parts[2])
        if response_offset != offset:
            raise UsbProtocolError(f"device returned offset {response_offset}, expected {offset}")
        data = self.serial.read_exact(response_length, timeout=max(5.0, response_length / 2000.0))
        trailer = self.serial.read_line(timeout=5.0)
        if trailer != "OK":
            raise UsbProtocolError(f"device returned READ trailer: {trailer}")
        return data

    def mark(self, offset: int) -> str:
        self.serial.write_line(f"MARK {offset}")
        line = self.serial.read_line()
        if not line.startswith("OK MARK "):
            raise UsbProtocolError(f"unexpected MARK response: {line}")
        return line

    def clear(self) -> str:
        self.serial.write_line("CLEAR")
        line = self.serial.read_line(timeout=10.0)
        if line != "OK CLEAR":
            raise UsbProtocolError(f"unexpected CLEAR response: {line}")
        return line


class Dk2StorageParser:
    def __init__(self, raw_offset: int = 0, validate_toc: bool = True, parse_markers: bool = False):
        self.buffer = bytearray()
        self.raw_position = raw_offset
        self.validate_toc = validate_toc
        self.parse_markers = parse_markers
        self.frames = 0
        self.markers = 0
        self.invalid_entries = 0
        self.zero_entries = 0
        self.padding_skips = 0

    def feed(self, data: bytes) -> list[tuple[str, int | bytes]]:
        self.buffer.extend(data)
        events: list[tuple[str, int | bytes]] = []
        consumed = 0

        while consumed < len(self.buffer):
            available = len(self.buffer) - consumed
            global_position = self.raw_position + consumed
            pos_in_block = global_position % STORAGE_BLOCK_SIZE
            remaining_in_block = STORAGE_BLOCK_SIZE - pos_in_block
            package_size = self.buffer[consumed]

            if package_size == 0:
                consumed += 1
                self.zero_entries += 1
                continue

            if package_size == 0xFF and self.parse_markers:
                if available < 5:
                    break
                epoch = (
                    self.buffer[consumed + 1]
                    | (self.buffer[consumed + 2] << 8)
                    | (self.buffer[consumed + 3] << 16)
                    | (self.buffer[consumed + 4] << 24)
                )
                consumed += 5
                if epoch > 0:
                    self.markers += 1
                    events.append(("marker", epoch))
                continue

            if pos_in_block > 0 and remaining_in_block < 12:
                if available < remaining_in_block:
                    break
                consumed += remaining_in_block
                self.padding_skips += 1
                continue

            if pos_in_block > 0 and package_size + 1 > remaining_in_block:
                if available < remaining_in_block:
                    break
                consumed += remaining_in_block
                self.padding_skips += 1
                continue

            if package_size < 10 or package_size > 160:
                self.invalid_entries += 1
                if pos_in_block == 0:
                    consumed += 1
                elif available >= remaining_in_block:
                    consumed += remaining_in_block
                    self.padding_skips += 1
                else:
                    break
                continue

            if available < package_size + 1:
                break

            frame = bytes(self.buffer[consumed + 1 : consumed + 1 + package_size])
            if self.validate_toc and frame and frame[0] not in VALID_OPUS_TOC:
                self.invalid_entries += 1
                if pos_in_block > 0 and available >= remaining_in_block:
                    consumed += remaining_in_block
                    self.padding_skips += 1
                else:
                    consumed += package_size + 1
                continue

            events.append(("frame", frame))
            self.frames += 1
            consumed += package_size + 1

        if consumed:
            del self.buffer[:consumed]
            self.raw_position += consumed

        return events


class WalChunkWriter:
    def __init__(
        self,
        output_dir: Path,
        device: str,
        start_timestamp: int,
        chunk_seconds: int,
        min_chunk_seconds: int,
        fps: int = DEFAULT_FPS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        frame_size: int = DEFAULT_FRAME_SIZE,
    ):
        self.output_dir = output_dir
        self.device = device
        self.timestamp = start_timestamp
        self.chunk_seconds = chunk_seconds
        self.min_chunk_seconds = min_chunk_seconds
        self.fps = fps
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self.frames_per_chunk = max(1, chunk_seconds * fps)
        self.min_frames = max(0, min_chunk_seconds * fps)
        self.current_frames: list[bytes] = []
        self.files: list[Path] = []
        self.skipped_short_chunks = 0
        self.chunk_index = 0
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def add_frame(self, frame: bytes) -> None:
        self.current_frames.append(frame)
        if len(self.current_frames) >= self.frames_per_chunk:
            self.flush()

    def apply_marker(self, epoch: int) -> None:
        self.flush(force=True)
        self.timestamp = epoch

    def flush(self, force: bool = False) -> None:
        if not self.current_frames:
            return
        if not force and self.min_frames and len(self.current_frames) < self.min_frames:
            self.skipped_short_chunks += 1
            self.current_frames = []
            return

        self.chunk_index += 1
        filename = (
            f"audio_{self.device}_opus_{self.sample_rate}_{self.channels}_"
            f"fs{self.frame_size}_r{self.chunk_index:04d}_{self.timestamp}.bin"
        )
        path = self.output_dir / filename
        with path.open("wb") as out:
            for frame in self.current_frames:
                out.write(struct.pack("<I", len(frame)))
                out.write(frame)

        self.files.append(path)
        self.timestamp += max(1, len(self.current_frames) // self.fps)
        self.current_frames = []


def find_default_port() -> str:
    candidates = []
    for pattern in (
        "/dev/serial/by-id/*OMI*",
        "/dev/serial/by-id/*Omi*",
        "/dev/serial/by-id/*ZEPHYR*",
        "/dev/serial/by-id/*USB-DEV*",
        "/dev/ttyACM*",
    ):
        candidates.extend(glob.glob(pattern))
    if not candidates:
        raise SystemExit("No USB CDC serial device found. Pass --port /dev/ttyACM0 explicitly.")
    return sorted(dict.fromkeys(candidates))[0]


def estimate_start_timestamp(stat: DeviceStat, fps: int) -> int:
    frames_per_block = 5
    bytes_per_second = STORAGE_BLOCK_SIZE * (fps / frames_per_block)
    estimated_seconds = int(stat.available / bytes_per_second) if bytes_per_second else 0
    return int(time.time()) - max(0, estimated_seconds)


def pull_storage(
    args: argparse.Namespace, device: OmiUsbDevice
) -> tuple[list[Path], DeviceStat, DeviceStat, Dk2StorageParser]:
    initial_stat = device.stat()
    if initial_stat.available <= 0:
        print(f"No unsynced bytes. total={initial_stat.total} offset={initial_stat.offset}")
        return [], initial_stat, initial_stat, Dk2StorageParser(initial_stat.offset)

    start_timestamp = args.start_timestamp or estimate_start_timestamp(initial_stat, args.frames_per_second)
    parser = Dk2StorageParser(
        raw_offset=initial_stat.offset,
        validate_toc=not args.no_toc_check,
        parse_markers=args.use_storage_markers,
    )
    writer = WalChunkWriter(
        output_dir=args.out,
        device=args.device,
        start_timestamp=start_timestamp,
        chunk_seconds=args.chunk_seconds,
        min_chunk_seconds=args.min_chunk_seconds,
        fps=args.frames_per_second,
        sample_rate=args.sample_rate,
        channels=args.channels,
        frame_size=args.frame_size,
    )

    start_offset = initial_stat.offset
    target_offset = initial_stat.total
    current_offset = start_offset
    started = time.monotonic()
    last_progress = 0.0

    print(
        f"Pulling {target_offset - start_offset} bytes from {args.port} " f"(offset {start_offset} -> {target_offset})"
    )

    while current_offset < target_offset:
        remaining = target_offset - current_offset
        chunk_len = min(args.usb_chunk_bytes, remaining)
        raw = device.read(current_offset, chunk_len)
        if not raw:
            raise UsbProtocolError(f"device returned no data at offset {current_offset}")
        current_offset += len(raw)

        for kind, value in parser.feed(raw):
            if kind == "frame":
                writer.add_frame(value)  # type: ignore[arg-type]
            elif kind == "marker":
                writer.apply_marker(int(value))

        now = time.monotonic()
        if now - last_progress >= 2.0 or current_offset >= target_offset:
            elapsed = max(0.001, now - started)
            pulled = current_offset - start_offset
            speed_kbps = pulled / 1024 / elapsed
            percent = pulled / max(1, target_offset - start_offset) * 100
            print(f"{percent:5.1f}% {pulled}/{target_offset - start_offset} bytes at {speed_kbps:.1f} KB/s")
            last_progress = now

    writer.flush()
    final_stat = device.stat()

    print(
        f"Wrote {len(writer.files)} WAL chunk(s), frames={parser.frames}, "
        f"markers={parser.markers}, skipped_short_chunks={writer.skipped_short_chunks}"
    )
    if parser.invalid_entries:
        print(f"Parser skipped {parser.invalid_entries} invalid storage entries")

    return writer.files, initial_stat, final_stat, parser


def build_multipart(files: list[Path], boundary: str) -> bytes:
    parts = bytearray()
    for path in files:
        parts.extend(f"--{boundary}\r\n".encode("ascii"))
        parts.extend(
            (
                f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("ascii")
        )
        parts.extend(path.read_bytes())
        parts.extend(b"\r\n")
    parts.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(parts)


def http_json(url: str, token: str | None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def upload_batch(files: list[Path], upload_url: str, token: str | None) -> bool:
    boundary = f"omi-usb-sync-{int(time.time() * 1000)}"
    body = build_multipart(files, boundary)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(upload_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"upload failed: HTTP {exc.code}: {detail}") from exc

    if status in (200, 207):
        return status == 200

    if status == 202:
        started = json.loads(payload)
        job_id = started.get("job_id")
        poll_after_ms = int(started.get("poll_after_ms", 3000))
        if not job_id:
            raise RuntimeError(f"upload returned 202 without job_id: {payload}")
        poll_url = f"{upload_url.rstrip('/')}/{job_id}"
        for _ in range(120):
            time.sleep(max(0.5, poll_after_ms / 1000))
            status_payload = http_json(poll_url, token)
            state = status_payload.get("status")
            print(f"job {job_id}: {state}")
            if state == "completed":
                return True
            if state in {"failed", "partial_failure"}:
                return False
        raise RuntimeError(f"job {job_id} did not finish before polling timeout")

    raise RuntimeError(f"unexpected upload HTTP status: {status}")


def upload_files(files: list[Path], args: argparse.Namespace) -> bool:
    if not files:
        return True
    upload_url = args.upload_url
    if args.api_base_url:
        upload_url = args.api_base_url.rstrip("/") + "/v2/sync-local-files"
    if not upload_url:
        return True

    token = args.auth_token or os.environ.get("OMI_AUTH_TOKEN")
    all_ok = True
    for start in range(0, len(files), args.upload_batch_size):
        batch = files[start : start + args.upload_batch_size]
        print(f"Uploading {len(batch)} WAL chunk(s) to {upload_url}")
        all_ok = upload_batch(batch, upload_url, token) and all_ok
    return all_ok


def resolve_upload_paths(args: argparse.Namespace) -> list[Path]:
    if args.paths:
        files = [Path(path) for path in args.paths]
    else:
        files = sorted(args.dir.glob("*.bin"))
    return [path for path in files if path.is_file()]


def iter_paths(paths: Iterable[Path]) -> str:
    return "\n".join(str(path) for path in paths)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", default=None, help="USB CDC serial device, e.g. /dev/ttyACM0")
    parser.add_argument("--timeout", type=float, default=5.0, help="Serial line timeout in seconds")
    parser.add_argument("--baud", type=int, default=921600, help="CDC line speed requested from the device")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stat = sub.add_parser("stat", help="Print pending DK2 storage byte counts")
    add_common_args(stat)

    pull = sub.add_parser("pull", help="Pull pending DK2 storage into WAL .bin chunks")
    add_common_args(pull)
    pull.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory for WAL chunks")
    pull.add_argument("--device", default="dk2", help="Device slug used in output filenames")
    pull.add_argument("--usb-chunk-bytes", type=int, default=DEFAULT_USB_CHUNK_BYTES)
    pull.add_argument("--chunk-seconds", type=int, default=60)
    pull.add_argument("--min-chunk-seconds", type=int, default=DEFAULT_MIN_CHUNK_SECONDS)
    pull.add_argument("--start-timestamp", type=int, default=None, help="Unix seconds for first output chunk")
    pull.add_argument("--frames-per-second", type=int, default=DEFAULT_FPS)
    pull.add_argument("--frame-size", type=int, default=DEFAULT_FRAME_SIZE)
    pull.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    pull.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    pull.add_argument("--no-toc-check", action="store_true", help="Do not validate Opus TOC bytes while parsing")
    pull.add_argument(
        "--use-storage-markers",
        action="store_true",
        help="Treat 0xFF entries as timestamp markers. DK2 storage does not use these by default.",
    )
    pull.add_argument("--api-base-url", default=None, help="Backend base URL; uploads to <base>/v2/sync-local-files")
    pull.add_argument("--upload-url", default=None, help="Full sync upload URL")
    pull.add_argument("--auth-token", default=None, help="Bearer token. Defaults to OMI_AUTH_TOKEN.")
    pull.add_argument("--upload-batch-size", type=int, default=10)
    pull.add_argument("--mark-after-pull", action="store_true", help="Save device offset after a successful pull")
    pull.add_argument("--mark-after-upload", action="store_true", help="Save device offset after successful upload")
    pull.add_argument(
        "--clear-after-pull", action="store_true", help="Clear DK2 audio file after successful pull/upload"
    )

    upload = sub.add_parser("upload", help="Upload existing WAL .bin chunks to the backend")
    upload.add_argument("paths", nargs="*", help="WAL .bin files to upload. Defaults to --dir/*.bin.")
    upload.add_argument("--dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory of WAL chunks")
    upload.add_argument("--api-base-url", default=None, help="Backend base URL; uploads to <base>/v2/sync-local-files")
    upload.add_argument("--upload-url", default=None, help="Full sync upload URL")
    upload.add_argument("--auth-token", default=None, help="Bearer token. Defaults to OMI_AUTH_TOKEN.")
    upload.add_argument("--upload-batch-size", type=int, default=10)

    mark = sub.add_parser("mark", help="Set the DK2 saved sync offset")
    add_common_args(mark)
    mark.add_argument("offset", type=int)

    clear = sub.add_parser("clear", help="Clear the DK2 audio file and reset sync offset")
    add_common_args(clear)

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "upload":
        files = resolve_upload_paths(args)
        if not files:
            print("No WAL files found to upload.", file=sys.stderr)
            return 2
        uploaded_ok = upload_files(files, args)
        return 0 if uploaded_ok else 3

    if args.port is None:
        args.port = find_default_port()

    with UsbSerial(args.port, timeout=args.timeout, baud=args.baud) as serial:
        device = OmiUsbDevice(serial)
        hello = device.ping()
        print(f"{args.port}: {hello}")

        if args.command == "stat":
            stat = device.stat()
            print(
                f"total={stat.total} offset={stat.offset} available={stat.available} "
                f"file={stat.file_num} block={stat.block_size}"
            )
            return 0

        if args.command == "mark":
            print(device.mark(args.offset))
            return 0

        if args.command == "clear":
            print(device.clear())
            return 0

        files, initial_stat, _final_stat, parser = pull_storage(args, device)
        if initial_stat.available > 0 and parser.frames == 0:
            print("No valid audio frames were parsed; not marking or clearing device storage.", file=sys.stderr)
            return 2

        uploaded_ok = upload_files(files, args)
        should_mark = args.mark_after_pull or (args.mark_after_upload and uploaded_ok)

        if args.clear_after_pull and uploaded_ok:
            print(device.clear())
        elif should_mark and uploaded_ok:
            print(device.mark(initial_stat.total))
        elif args.mark_after_upload and not uploaded_ok:
            print("Upload did not fully succeed; leaving device offset unchanged.", file=sys.stderr)

        if files:
            print("Output files:")
            print(iter_paths(files))
        return 0 if uploaded_ok else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, TimeoutError, UsbProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
