"""Build a silent two-minute walkthrough from captured, real UI checkpoints.

This is an annotated sequence of screenshots, not a real-time screen recording.
Capture the screenshots through the browser before running this script.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
WORK = ROOT / "artifacts" / "private" / "video"
SCENES = [
    ("01-home.png", 10, "01 / DATA DETECTIVE", "A local investigation tool. These are real UI checkpoints, with public/synthetic demo data."),
    ("10-upload.png", 12, "02 / CONFIRM THE DATA CONTRACT", "Map the columns. Choose dates, number separators and currency. Formats are never guessed."),
    ("02-investigate.png", 12, "03 / START WITH A QUESTION", "The Duplicate Dispatch case totals GBP 4,701.37. Four leads need evidence review."),
    ("03-evidence.png", 18, "04 / INSPECT THE SOURCE RECORDS", "Matching rows are candidates, not proof of an error. Repeated orders and returns can be legitimate."),
    ("04-plan.png", 14, "05 / MAKE AN EXPLICIT REPAIR PLAN", "The case correction note identifies extra copies at rows 137, 138 and 139. Other rows stay intact."),
    ("05-preview.png", 18, "06 / PREVIEW THE COMBINED IMPACT", "GBP 4,701.37 becomes GBP 4,593.97: a change of -107.40. Nothing is saved yet."),
    ("06-confirm.png", 8, "07 / CONFIRM THE REVIEWED CHANGE", "Save only after reviewing the affected rows. The preview is bound to its source version."),
    ("07-history.png", 12, "08 / KEEP THE EVIDENCE", "Download the included rows, exact edit history and a report with unresolved findings."),
    ("08-restore.png", 8, "09 / RESTORE WITHOUT ERASING HISTORY", "Choose Original import. Restoration creates another version and preserves the repair record."),
    ("09-restored.png", 8, "10 / BACK TO THE ORIGINAL RESULT", "GBP 4,701.37 is restored. A working demo demonstrates behavior, not measured user time savings."),
]


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("Install FFmpeg/ffprobe to build the optional walkthrough video.")
    WORK.mkdir(parents=True, exist_ok=True)
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if not font.is_file():
        raise SystemExit("This video builder uses the Windows Segoe UI font; select a local font on other systems.")
    font_arg = str(font).replace("\\", "/").replace(":", "\\:")
    metadata = []
    clips = []
    for index, (filename, duration, title, body) in enumerate(SCENES):
        source = ASSETS / filename
        if not source.is_file():
            raise SystemExit(f"Capture the missing UI checkpoint first: {source}")
        title_file = WORK / f"{index:02}-title.txt"
        body_file = WORK / f"{index:02}-body.txt"
        title_file.write_text(title, encoding="utf-8")
        body_file.write_text(body, encoding="utf-8")
        title_path = title_file.relative_to(ROOT).as_posix()
        body_path = body_file.relative_to(ROOT).as_posix()
        filters = (
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:816:0:0:color=0xf7f8fa,"
            f"drawtext=fontfile='{font_arg}':textfile='{title_path}':fontsize=22:fontcolor=0x087f74:x=24:y=736,"
            f"drawtext=fontfile='{font_arg}':textfile='{body_path}':fontsize=18:fontcolor=0x182b3a:x=24:y=773"
        )
        clip = WORK / f"{index:02}.mp4"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "24",
                        "-i", str(source), "-t", str(duration), "-vf", filters, "-c:v", "libx264",
                        "-threads", "2", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p", str(clip)],
                       cwd=ROOT, check=True)
        clips.append(clip)
        metadata.append({"image": filename, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "duration_seconds": duration, "title": title, "caption": body})
    concat = WORK / "concat.txt"
    concat.write_text("\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8")
    output = ASSETS / "walkthrough.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)], check=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_name",
                            "-of", "json", str(output)], capture_output=True, text=True, check=True)
    verification = json.loads(probe.stdout)
    assert abs(float(verification["format"]["duration"]) - 120) < 0.1
    report = {"type": "Annotated real UI screenshot sequence, not real-time capture or user study",
              "scenes": metadata, "video_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "video_bytes": output.stat().st_size, "ffprobe": verification}
    (ROOT / "reports" / "walkthrough.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(output), "duration": 120, "bytes": output.stat().st_size}))


if __name__ == "__main__":
    main()
