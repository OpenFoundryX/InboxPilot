"""Transcribing media a meeting bot never attended.

Recall transcribes the calls its own bots sit in. An uploaded file or a browser
recording has no bot behind it, so the audio has to be turned into text here.

The shape of the problem is set by what arrives: an hour of 4K video is a
gigabyte of pixels we are paying to move, and the transcription API takes 25 MB
per request. So everything is transcoded down to speech-sized mono audio first,
and only split into pieces when it is still too big after that.

Returns the same `Transcript` type the bot provider returns, so nothing
downstream can tell the two sources apart.

Blocking, CPU- and network-heavy; call from a worker.
"""

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from integrations.meetingbot.base import Transcript, TranscriptSegment

log = get_logger(__name__)


class TranscriptionError(RuntimeError):
    """Media could not be transcribed. The caller decides whether to retry."""


#: Speech-sized audio: mono, 16 kHz, 32 kbps MP3. Well above what a speech model
#: resolves and roughly 14 MB per hour, which puts most meetings inside a single
#: request. Anything richer is bytes spent on fidelity nobody transcribes.
_SAMPLE_RATE = 16_000
_BITRATE = "32k"

#: The API's hard ceiling is 25 MB. Splitting is decided against a lower number
#: so a chunk that transcodes slightly larger than predicted still fits.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: How long each piece is when a file has to be split. At the bitrate above this
#: is roughly 3.5 MB — comfortably inside the ceiling, and short enough that one
#: failed chunk is cheap to retry.
_CHUNK_SECONDS = 15 * 60

#: Below this a chunk is discarded rather than transcribed. Splitting on a
#: duration that isn't an exact multiple of the chunk length leaves a trailing
#: sliver — a 30.0001-second file cut at 10 seconds yields three chunks and 0.2
#: seconds of remainder — and sending that costs a request to transcribe noise.
#: At the bitrate above, one second is roughly 4 KB.
_MIN_CHUNK_BYTES = 8 * 1024

#: Long enough for a large download over a slow link, bounded so a hung
#: connection cannot occupy a worker forever.
_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=600.0)


def transcribe_url(url: str) -> tuple[Transcript, float]:
    """Download, transcode, and transcribe media. Returns the text and duration.

    The duration comes from the media itself rather than from anything the
    client said, because it is what meters against the user's plan.

    Everything happens inside one temporary directory that is always removed —
    a gigabyte of someone's meeting must not survive a failure on a worker's
    disk.
    """
    if not settings.OPENAI_API_KEY:
        raise TranscriptionError("OPENAI_API_KEY is not configured")
    _require_ffmpeg()

    with tempfile.TemporaryDirectory(prefix="meeting-media-") as tmp:
        workdir = Path(tmp)
        source = _download(url, workdir / "source")
        audio = _to_speech_audio(source, workdir / "audio.mp3")
        # The source can be large; releasing it before transcription halves peak
        # disk while the chunks are written.
        source.unlink(missing_ok=True)

        duration = _duration_seconds(audio)
        segments = _transcribe_file(audio, workdir, duration)

    return Transcript(segments=segments), duration


def _require_ffmpeg() -> None:
    """Fail with the actual problem, not with a FileNotFoundError from Popen."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise TranscriptionError(
            f"{' and '.join(missing)} not found on PATH — the image needs ffmpeg installed"
        )


def _download(url: str, dest: Path) -> Path:
    """Stream a presigned URL to disk.

    Streamed rather than read into memory: these are meeting recordings up to a
    gigabyte, and a worker that buffers one is a worker that dies on the second.
    """
    try:
        with httpx.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as resp:
            resp.raise_for_status()
            with dest.open("wb") as handle:
                for block in resp.iter_bytes(chunk_size=1024 * 1024):
                    handle.write(block)
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Could not download media: {exc}") from exc

    if dest.stat().st_size == 0:
        raise TranscriptionError("Downloaded media is empty")
    return dest


def _run(args: list[str], *, what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, check=True, text=True)
    except subprocess.CalledProcessError as exc:
        # ffmpeg's diagnosis is on the last line of stderr; the rest is banner.
        tail = (exc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise TranscriptionError(f"{what} failed: {tail[0]}") from exc


def _to_speech_audio(source: Path, dest: Path) -> Path:
    """Strip to mono speech-grade audio, discarding video entirely."""
    _run(
        [
            "ffmpeg", "-nostdin", "-y",
            "-i", str(source),
            "-vn",  # drop video: it is the bulk of the file and none of the words
            "-ac", "1",
            "-ar", str(_SAMPLE_RATE),
            "-b:a", _BITRATE,
            str(dest),
        ],
        what="Audio extraction",
    )
    if not dest.exists() or dest.stat().st_size == 0:
        raise TranscriptionError("That file has no audio track to transcribe")
    return dest


def _duration_seconds(audio: Path) -> float:
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(audio),
        ],
        what="Reading media duration",
    )
    try:
        value = json.loads(proc.stdout or "{}").get("format", {}).get("duration")
        return max(0.0, float(value))
    except (ValueError, TypeError):
        # A container without a duration header still transcribes fine; only
        # metering suffers, and charging zero is the safe direction to be wrong.
        log.warning("meetings.duration_unknown", path=audio.name)
        return 0.0


def _transcribe_file(audio: Path, workdir: Path, duration: float) -> list[TranscriptSegment]:
    """Transcribe, splitting into chunks only when the file is too large."""
    if audio.stat().st_size <= _MAX_UPLOAD_BYTES:
        text = _transcribe_one(audio)
        return [TranscriptSegment(speaker=None, text=text, start=0.0)] if text else []

    chunks = _split(audio, workdir, duration)
    segments: list[TranscriptSegment] = []
    for index, chunk in enumerate(chunks):
        text = _transcribe_one(chunk)
        # Offsets are the chunk boundaries, which is exactly true: each piece
        # starts where the last one ended.
        if text:
            segments.append(
                TranscriptSegment(speaker=None, text=text, start=index * float(_CHUNK_SECONDS))
            )
        chunk.unlink(missing_ok=True)
    return segments


def _split(audio: Path, workdir: Path, duration: float) -> list[Path]:
    """Cut into fixed-length pieces, re-encoding nothing.

    `-c copy` means this is a stream copy: the audio is already in the format we
    want, so splitting costs a read and a write rather than a second encode.
    """
    out = workdir / "chunks"
    out.mkdir(exist_ok=True)
    _run(
        [
            "ffmpeg", "-nostdin", "-y",
            "-i", str(audio),
            "-f", "segment",
            "-segment_time", str(_CHUNK_SECONDS),
            "-c", "copy",
            str(out / "part-%04d.mp3"),
        ],
        what="Splitting audio",
    )
    produced = sorted(out.glob("part-*.mp3"))
    chunks = []
    for chunk in produced:
        if chunk.stat().st_size >= _MIN_CHUNK_BYTES:
            chunks.append(chunk)
        else:
            chunk.unlink(missing_ok=True)
    if not chunks:
        raise TranscriptionError("Splitting produced no audio")

    expected = math.ceil(duration / _CHUNK_SECONDS) if duration else len(chunks)
    log.info(
        "meetings.audio_split",
        chunks=len(chunks),
        discarded=len(produced) - len(chunks),
        expected=expected,
    )
    return chunks


def _transcribe_one(path: Path) -> str:
    """One request to the transcription API.

    A per-call client rather than a cached one, matching `summarize.py`: these
    run in workers that fork, and a socket inherited across a fork is a socket
    two processes will corrupt.
    """
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        with path.open("rb") as handle:
            resp = client.audio.transcriptions.create(
                model=settings.TRANSCRIBE_MODEL,
                file=handle,
                response_format="text",
            )
    except Exception as exc:
        raise TranscriptionError(f"Transcription failed for {path.name}: {exc}") from exc

    # `response_format="text"` yields a bare string; the SDK still wraps some
    # models' replies in an object, so accept either rather than assuming.
    text = resp if isinstance(resp, str) else getattr(resp, "text", "")
    return str(text or "").strip()


__all__ = ["transcribe_url", "TranscriptionError"]
