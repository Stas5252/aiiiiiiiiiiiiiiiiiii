"""Сборка ИИ-мультика из клипов Veo: озвучка поверх клипа, нормализация и склейка в один вертикальный ролик."""
import asyncio
import os
import tempfile

from .video import ffmpeg_available


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg: " + err.decode(errors="ignore")[-400:])


async def _has_audio(path: str) -> bool:
    proc = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                                "stream=codec_type", "-of", "csv=p=0", path,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return b"audio" in out


async def assemble(clips: list[tuple[bytes, bytes | None]], out_path: str, size: tuple[int, int] = (1080, 1920)) -> str:
    """clips: [(mp4 bytes, озвучка bytes|None)]. Родной звук клипа приглушается под озвучку."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg не установлен")
    if not clips:
        raise RuntimeError("нет клипов")
    w, h = size
    scale = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,format=yuv420p"
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, (video, voice) in enumerate(clips):
            src = os.path.join(tmp, f"src{i}.mp4")
            with open(src, "wb") as f:
                f.write(video)
            dst = os.path.join(tmp, f"part{i}.mp4")
            has_audio = await _has_audio(src)
            if voice:
                vpath = os.path.join(tmp, f"voice{i}.bin")
                with open(vpath, "wb") as f:
                    f.write(voice)
                if has_audio:
                    flt = "[0:a]volume=0.25[a0];[1:a]apad[a1];[a0][a1]amix=inputs=2:duration=first:normalize=0[a]"
                else:
                    flt = "[1:a]apad[a]"
                await _run("-i", src, "-i", vpath, "-filter_complex", f"[0:v]{scale}[v];{flt}", "-map", "[v]", "-map", "[a]",
                           "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-ar", "48000", "-ac", "2", dst)
            elif has_audio:
                await _run("-i", src, "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-ar", "48000", "-ac", "2", dst)
            else:
                await _run("-i", src, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", scale, "-shortest",
                           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", dst)
            parts.append(dst)
        if len(parts) == 1:
            await _run("-i", parts[0], "-c", "copy", "-movflags", "+faststart", out_path)
        else:
            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w") as f:
                f.write("".join(f"file '{p}'\n" for p in parts))
            await _run("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out_path)
    return out_path
