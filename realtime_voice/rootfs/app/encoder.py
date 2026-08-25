"""PCM16 to MP3 encoding using the add-on's ffmpeg binary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

PCM_BYTES_PER_SECOND = 24_000 * 2


async def encode_mp3(pcm: bytes) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        "24000",
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-b:a",
        "64k",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(pcm)
    if process.returncode:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[:500]}")
    return stdout


class ProgressiveMp3Encoder:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.input_bytes = 0

    @property
    def duration_seconds(self) -> float:
        return self.input_bytes / PCM_BYTES_PER_SECOND

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-b:a",
            "64k",
            "-flush_packets",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def write(self, chunk: bytes) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(chunk)
        await self.process.stdin.drain()
        self.input_bytes += len(chunk)

    async def chunks(self) -> AsyncIterator[bytes]:
        assert self.process and self.process.stdout
        while chunk := await self.process.stdout.read(4096):
            yield chunk

    async def finish(self) -> None:
        if not self.process:
            return
        assert self.process.stdin
        self.process.stdin.close()
        await self.process.wait()

    async def cancel(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
