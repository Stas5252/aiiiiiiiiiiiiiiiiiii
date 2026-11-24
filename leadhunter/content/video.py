"""Короткое вертикальное видео из кадров через ffmpeg."""
import asyncio
import os
import shutil
import tempfile

from .render import Brand, render_scene

FPS = 30
PAD = 0.3  # пауза после озвучки


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


async def _run(*args: str) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _ffmpeg(*args: str) -> None:
    code, _, err = await _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args)
    if code != 0:
        raise RuntimeError(f"ffmpeg завершился с кодом {code}: {err[-800:]}")


async def probe_duration(path: str) -> float:
    try:
        code, out, _ = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", path)
        return float(out.strip()) if code == 0 else 0.0
    except (OSError, ValueError):
        return 0.0


ANIM_FPS = 25  # кадры анимации сцены приходят из браузера с этой частотой


async def build_motion(frames: list[bytes], out_path: str, *, hold: float = 1.5) -> str:
    """Короткое видео без звука из кадров анимации (обложка карусели): анимация и пауза на последнем кадре."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg не установлен")
    if not frames:
        raise RuntimeError("нет кадров для видео")
    with tempfile.TemporaryDirectory(prefix="lhmotion_") as tmp:
        for i, frame in enumerate(frames):
            with open(os.path.join(tmp, f"{i:04d}.jpg"), "wb") as f:
                f.write(frame)
        await _ffmpeg("-framerate", str(ANIM_FPS), "-i", os.path.join(tmp, "%04d.jpg"),
                      "-vf", f"tpad=stop_mode=clone:stop_duration={hold:.2f},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(FPS), "-movflags", "+faststart", out_path)
    return out_path


async def build_video(scenes: list[dict], brand: Brand, out_path: str, *, music_path: str | None = None,
                      size=(1080, 1920), frames: list[list[bytes]] | None = None) -> str:
    """frames — кадры анимации каждой сцены (из браузера): сцена сначала двигается, потом стоит до конца своей
    длительности. Без них сцена — один неподвижный кадр, нарисованный Pillow."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg не установлен")
    if not scenes:
        raise RuntimeError("нет сцен для видео")
    with tempfile.TemporaryDirectory(prefix="lhvideo_") as tmp:
        durations, audio_files, lines = [], [], []
        for i, sc in enumerate(scenes):
            anim = frames[i] if frames and i < len(frames) else None
            if not anim:
                anim = [render_scene(sc.get("text", ""), brand, size, sc.get("subtitle", ""))]
            ext = "jpg" if anim[0][:3] == b"\xff\xd8\xff" else "png"
            paths = []
            for k, data in enumerate(anim):
                paths.append(os.path.join(tmp, f"f{i}_{k}.{ext}"))
                with open(paths[-1], "wb") as f:
                    f.write(data)
            frame = paths[-1]
            dur = float(sc.get("duration") or 3.0)
            if sc.get("audio"):
                ap = os.path.join(tmp, f"a{i}.audio")
                with open(ap, "wb") as f:
                    f.write(sc["audio"])
                dur = await probe_duration(ap) + PAD
                audio_files.append(ap)
            step = 1 / ANIM_FPS
            # сцена не короче трёх четвертей анимации: к этому моменту все слова уже на экране
            dur = max(dur, 0.75 * len(paths) * step) if len(paths) > 1 else dur
            paths = paths[:max(1, int(dur / step))]
            frame = paths[-1]
            durations.append(dur)
            for path in paths[:-1]:
                lines += [f"file '{path}'", f"duration {step:.3f}"]
            lines += [f"file '{frame}'", f"duration {dur - step * (len(paths) - 1):.3f}"]
        lines.append(f"file '{frame}'")  # concat demuxer теряет последнюю длительность
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            f.write("\n".join(lines))
        total = sum(durations)

        args = ["-f", "concat", "-safe", "0", "-i", lst]
        inputs, filt = 1, []
        voice = None
        if audio_files and len(audio_files) == len(scenes):
            # у каждой сцены есть озвучка: приводим к единому формату и добиваем паузой до длины сцены
            for ap, dur in zip(audio_files, durations):
                args += ["-i", ap]
            parts = "".join(
                f"[{i + 1}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=whole_dur={d:.3f}[v{i}];"
                for i, d in enumerate(durations))
            filt.append(parts + "".join(f"[v{i}]" for i in range(len(durations))) + f"concat=n={len(durations)}:v=0:a=1[voice]")
            inputs += len(audio_files)
            voice = "[voice]"
        elif audio_files:
            # озвучка только у части сцен: тишина вместо остальных
            idx = 0
            labels = []
            for i, d in enumerate(durations):
                if scenes[i].get("audio"):
                    args += ["-i", audio_files[idx]]
                    filt.append(f"[{inputs}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=whole_dur={d:.3f}[v{i}]")
                    idx += 1
                else:
                    args += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
                    filt.append(f"[{inputs}:a]anull[v{i}]")
                inputs += 1
                labels.append(f"[v{i}]")
            filt.append("".join(labels) + f"concat=n={len(durations)}:v=0:a=1[voice]")
            voice = "[voice]"
        music = None
        if music_path:
            args += ["-stream_loop", "-1", "-i", music_path]
            music = f"[{inputs}:a]"
            inputs += 1
        if voice and music:
            filt.append(f"{music}volume=0.15[bg];{voice}[bg]amix=inputs=2:duration=first:normalize=0[aout]")
            amap = "[aout]"
        elif music:
            filt.append(f"{music}volume=0.15[aout]")
            amap = "[aout]"
        else:
            amap = voice

        w, h = size
        filt.append(f"[0:v]scale={w}:{h},fps={FPS},format=yuv420p[vout]")
        cmd = args + ["-filter_complex", ";".join(filt), "-map", "[vout]"]
        if amap:
            cmd += ["-map", amap, "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-movflags", "+faststart", out_path]
        await _ffmpeg(*cmd)
    return out_path
