import os
import re
import json
import uuid
import shutil
import threading
import subprocess
import time
import secrets
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse

APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
INPUT_REMOTE = os.environ.get("INPUT_REMOTE", "gdrive:VideoConverter/Input")
OUTPUT_REMOTE = os.environ.get("OUTPUT_REMOTE", "gdrive:VideoConverter/Converted")
MOUNT_PATH = Path(os.environ.get("MOUNT_PATH", "/gdrive"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work/output"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache/rclone"))
RCLONE_CONFIG = os.environ.get("RCLONE_CONFIG", "/config/rclone.conf")

app = FastAPI(title="Private Video Converter")
security = HTTPBasic()

jobs = {}
jobs_lock = threading.Lock()
queue_lock = threading.Lock()
process_lock = threading.Lock()
active_processes = {}  # job_id -> subprocess.Popen
cancel_requests = set()

worker_thread = None
mount_process = None


def auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, APP_USER)
    ok_pass = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not APP_PASSWORD or not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def run_cmd(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def check_remote():
    if not Path(RCLONE_CONFIG).exists():
        raise RuntimeError(f"Missing rclone config at {RCLONE_CONFIG}")
    result = run_cmd(["rclone", "listremotes"])
    if result.returncode != 0 or "gdrive:" not in result.stdout.splitlines():
        raise RuntimeError("rclone remote gdrive: is not configured/authenticated")


def start_mount():
    global mount_process
    check_remote()
    MOUNT_PATH.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if os.path.ismount(MOUNT_PATH):
        return

    args = [
        "rclone", "mount", INPUT_REMOTE, str(MOUNT_PATH),
        "--read-only",
        "--vfs-cache-mode", "minimal",
        "--vfs-read-chunk-size", "32M",
        "--vfs-read-chunk-size-limit", "256M",
        "--cache-dir", str(CACHE_DIR),
        "--dir-cache-time", "1m",
        "--log-level", "INFO",
    ]
    mount_process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )

    for _ in range(60):
        if os.path.ismount(MOUNT_PATH):
            return
        if mount_process.poll() is not None:
            raise RuntimeError(
                "rclone mount exited. Check FUSE permissions and rclone configuration."
            )
        time.sleep(1)

    raise RuntimeError("Timed out waiting for Google Drive mount")


def human(number):
    number = float(number or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.1f} {unit}"
        number /= 1024


def format_time(seconds):
    if seconds is None:
        return "—"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def list_input_files():
    result = run_cmd(["rclone", "lsjson", INPUT_REMOTE, "--files-only"])
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "Could not list Google Drive input folder"
        )

    items = json.loads(result.stdout or "[]")
    files = []
    for item in items:
        name = item.get("Name", "")
        if name.lower().endswith((".mov", ".mp4", ".m4v", ".mkv", ".avi")):
            files.append({
                "name": name,
                "size": item.get("Size", 0),
                "size_human": human(item.get("Size", 0)),
            })
    return sorted(files, key=lambda item: item["name"].lower())


def update_job(job_id, **changes):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)
            jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()


def is_cancelled(job_id):
    with process_lock:
        return job_id in cancel_requests


def register_process(job_id, process):
    with process_lock:
        active_processes[job_id] = process
        cancel_now = job_id in cancel_requests

    # A cancel request may arrive just before the process is registered.
    if cancel_now and process.poll() is None:
        process.terminate()


def unregister_process(job_id):
    with process_lock:
        active_processes.pop(job_id, None)


def terminate_job_process(job_id):
    with process_lock:
        process = active_processes.get(job_id)

    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def probe_duration(source):
    result = run_cmd([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ], timeout=120)

    if result.returncode != 0:
        raise RuntimeError(
            "Could not determine video duration: " + (result.stderr or "")[-1000:]
        )

    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError("FFprobe did not return a valid video duration")

    if duration <= 0:
        raise RuntimeError("Invalid video duration")

    return duration


def finish_cancelled(job_id, output, message):
    if output and output.exists():
        try:
            output.unlink()
        except OSError:
            pass

    update_job(
        job_id,
        status="cancelled",
        stage="Cancelled",
        message=message,
        progress=0,
        eta="—",
        local_output=None,
    )


def process_job(job_id, filename, crf, preset, test_seconds):
    output = None
    ffmpeg_process = None
    upload_process = None
    started_at = time.monotonic()

    try:
        update_job(
            job_id,
            status="running",
            stage="Preparing",
            message="Checking input file",
            progress=0,
            elapsed="00:00:00",
            eta="—",
        )

        if Path(filename).name != filename or filename in ("", ".", ".."):
            raise ValueError("Invalid filename")

        source = MOUNT_PATH / filename
        if not source.exists():
            raise RuntimeError("Source file is not visible in the mounted Drive folder")

        update_job(
            job_id,
            stage="Analyzing",
            message="Reading video duration with ffprobe",
        )
        duration = probe_duration(source)

        # For a test job, progress must be calculated against the test segment,
        # not the duration of the complete source video.
        target_duration = min(duration, test_seconds) if test_seconds else duration

        stem = Path(filename).stem
        output_name = f"{stem}.mp4"
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        output = WORK_DIR / f"{job_id}-{output_name}"

        free_space = shutil.disk_usage(WORK_DIR).free
        if free_space < 20 * 1024**3:
            raise RuntimeError(
                f"Only {human(free_space)} free on server; refusing to start "
                "with less than 20 GB."
            )

        update_job(
            job_id,
            duration_seconds=duration,
            duration=format_time(duration),
            target_duration_seconds=target_duration,
            target_duration=format_time(target_duration),
            stage="Encoding",
            message="Starting FFmpeg",
            progress=0,
            fps="—",
            speed="—",
            video_time="00:00:00",
            elapsed="00:00:00",
            eta="—",
            output_size="0 B",
            output_name=output_name,
        )

        command = ["ffmpeg", "-hide_banner", "-y", "-i", str(source)]
        if test_seconds:
            command += ["-t", str(test_seconds)]

        command += [
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            "-nostats",
            str(output),
        ]

        ffmpeg_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        register_process(job_id, ffmpeg_process)

        progress_data = {}
        last_update = 0.0

        while True:
            line = ffmpeg_process.stdout.readline()

            if not line:
                if ffmpeg_process.poll() is not None:
                    break
                if is_cancelled(job_id):
                    break
                continue

            line = line.strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            progress_data[key] = value

            if key == "progress":
                # FFmpeg's out_time_us is microseconds. out_time_ms is also
                # historically reported in microseconds by FFmpeg.
                raw_time = (
                    progress_data.get("out_time_us")
                    or progress_data.get("out_time_ms")
                )
                try:
                    processed_seconds = int(raw_time) / 1_000_000
                except (TypeError, ValueError):
                    processed_seconds = None

                now = time.monotonic()
                if processed_seconds is not None and now - last_update >= 0.8:
                    fraction = processed_seconds / max(target_duration, 0.001)
                    percent = min(89, max(0, int(fraction * 90)))

                    speed = progress_data.get("speed", "—")
                    fps = progress_data.get("fps", "—")
                    speed_match = re.search(r"([\d.]+)x", speed or "")
                    remaining = None

                    if speed_match:
                        try:
                            speed_value = float(speed_match.group(1))
                            if speed_value > 0:
                                remaining = (
                                    max(0, target_duration - processed_seconds)
                                    / speed_value
                                )
                        except ValueError:
                            pass

                    elapsed_seconds = now - started_at
                    output_bytes = output.stat().st_size if output.exists() else 0

                    update_job(
                        job_id,
                        stage="Encoding",
                        message="Encoding video",
                        progress=percent,
                        video_time=format_time(processed_seconds),
                        elapsed=format_time(elapsed_seconds),
                        eta=format_time(remaining) if remaining is not None else "Calculating…",
                        speed=speed,
                        fps=fps,
                        frame=progress_data.get("frame", "—"),
                        output_size=human(output_bytes),
                    )
                    last_update = now

                if is_cancelled(job_id):
                    break

        if is_cancelled(job_id):
            terminate_job_process(job_id)
            finish_cancelled(
                job_id, output,
                "Conversion cancelled; incomplete local output deleted.",
            )
            return

        ffmpeg_code = ffmpeg_process.wait()
        if ffmpeg_code != 0 or not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg failed with exit code {ffmpeg_code}. Output was not uploaded."
            )

        local_size = output.stat().st_size
        update_job(
            job_id,
            stage="Uploading",
            message="Uploading MP4 to Google Drive",
            progress=90,
            output_size=human(local_size),
            eta="—",
            upload_progress=0,
        )

        destination = f"{OUTPUT_REMOTE}/{output_name}"
        upload_command = [
            "rclone", "copyto",
            str(output),
            destination,
            "--retries", "3",
            "--low-level-retries", "10",
            "--stats", "1s",
            "--stats-one-line",
        ]

        upload_process = subprocess.Popen(
            upload_command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        register_process(job_id, upload_process)

        upload_pattern = re.compile(
            r"Transferred:\s*([^,]+),\s*([\d.]+)%"
        )

        for line in upload_process.stderr:
            if is_cancelled(job_id):
                break

            match = upload_pattern.search(line)
            if match:
                try:
                    upload_percent = min(100.0, max(0.0, float(match.group(2))))
                    overall_progress = 90 + int(upload_percent * 0.08)
                    update_job(
                        job_id,
                        stage="Uploading",
                        message="Uploading MP4 to Google Drive",
                        progress=overall_progress,
                        upload_progress=round(upload_percent, 1),
                        upload_transferred=match.group(1).strip(),
                    )
                except ValueError:
                    pass

        if is_cancelled(job_id):
            terminate_job_process(job_id)
            finish_cancelled(
                job_id, output,
                "Upload cancelled; incomplete local output deleted. "
                "Check the destination folder for any partial remote upload.",
            )
            return

        upload_code = upload_process.wait()
        if upload_code != 0:
            raise RuntimeError(
                f"Upload failed with exit code {upload_code}; "
                "local output retained for recovery."
            )

        update_job(
            job_id,
            stage="Verifying",
            message="Verifying uploaded file size",
            progress=99,
        )

        verification = run_cmd(["rclone", "lsjson", destination, "--stat"])
        if verification.returncode != 0:
            raise RuntimeError(
                "Could not verify remote upload; local output retained."
            )

        remote_info = json.loads(verification.stdout or "{}")
        if int(remote_info.get("Size", -1)) != local_size:
            raise RuntimeError(
                "Remote size does not match local output; local output retained."
            )

        output.unlink(missing_ok=True)
        update_job(
            job_id,
            status="completed",
            stage="Completed",
            progress=100,
            message="Upload verified; temporary output deleted",
            output_size=human(local_size),
            remote_path=destination,
            elapsed=format_time(time.monotonic() - started_at),
            eta="00:00:00",
            upload_progress=100,
        )

    except Exception as error:
        if is_cancelled(job_id):
            finish_cancelled(
                job_id, output,
                "Job cancelled; incomplete local output deleted.",
            )
        else:
            update_job(
                job_id,
                status="failed",
                stage="Failed",
                message=str(error),
                progress=0,
                eta="—",
                local_output=str(output) if output and output.exists() else None,
            )

    finally:
        unregister_process(job_id)
        with process_lock:
            cancel_requests.discard(job_id)


def worker_loop():
    while True:
        pending = None
        with queue_lock:
            with jobs_lock:
                for job in jobs.values():
                    if job["status"] == "queued":
                        pending = job.copy()
                        break

        if pending:
            process_job(
                pending["id"],
                pending["filename"],
                pending["crf"],
                pending["preset"],
                pending["test_seconds"],
            )
        else:
            time.sleep(1)


def request_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")

        if job["status"] in ("completed", "failed", "cancelled"):
            raise HTTPException(409, f"Job is already {job['status']}")

        if job["status"] == "queued":
            job.update(
                status="cancelled",
                stage="Cancelled",
                message="Cancelled before processing started",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"ok": True, "status": "cancelled"}

        job["cancel_requested"] = True
        job["message"] = "Cancellation requested; stopping current process…"
        job["updated_at"] = datetime.now(timezone.utc).isoformat()

    with process_lock:
        cancel_requests.add(job_id)

    terminate_job_process(job_id)
    return {"ok": True, "status": "cancelling"}


@app.on_event("startup")
def startup():
    global worker_thread
    start_mount()
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()


@app.on_event("shutdown")
def shutdown():
    global mount_process

    with process_lock:
        running_ids = list(active_processes.keys())

    for job_id in running_ids:
        terminate_job_process(job_id)

    if mount_process and mount_process.poll() is None:
        mount_process.terminate()


@app.get("/health")
def health():
    return {"ok": True, "mounted": os.path.ismount(MOUNT_PATH)}


@app.get("/", response_class=HTMLResponse)
def dashboard(_=Depends(auth)):
    return HTMLResponse(PAGE)


@app.get("/api/files")
def files(_=Depends(auth)):
    try:
        return {"files": list_input_files()}
    except Exception as error:
        raise HTTPException(500, str(error))


@app.post("/api/jobs")
def create_job(payload: dict, _=Depends(auth)):
    filename = payload.get("filename")
    crf = int(payload.get("crf", 28))
    preset = payload.get("preset", "veryfast")
    test_seconds = int(payload.get("test_seconds", 0))

    if not isinstance(filename, str) or not filename:
        raise HTTPException(400, "Choose a video file")
    if not 18 <= crf <= 35:
        raise HTTPException(400, "CRF must be between 18 and 35")
    if preset not in (
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium"
    ):
        raise HTTPException(400, "Unsupported preset")
    if test_seconds not in (0, 60, 120, 300):
        raise HTTPException(400, "Test duration must be 0, 60, 120 or 300 seconds")

    try:
        available = {item["name"] for item in list_input_files()}
    except Exception as error:
        raise HTTPException(500, str(error))

    if filename not in available:
        raise HTTPException(404, "File not found in Google Drive Input folder")

    with jobs_lock:
        active = any(
            job["status"] in ("queued", "running")
            for job in jobs.values()
        )
        if active:
            raise HTTPException(
                409,
                "A job is already queued or running. This app processes one video at a time.",
            )

        job_id = str(uuid.uuid4())[:8]
        jobs[job_id] = {
            "id": job_id,
            "filename": filename,
            "status": "queued",
            "stage": "Queued",
            "progress": 0,
            "message": "Queued",
            "crf": crf,
            "preset": preset,
            "test_seconds": test_seconds,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "elapsed": "00:00:00",
            "eta": "—",
        }

    return jobs[job_id]


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _=Depends(auth)):
    return request_cancel(job_id)


@app.get("/api/jobs")
def get_jobs(_=Depends(auth)):
    with jobs_lock:
        job_list = [job.copy() for job in jobs.values()]

    return {
        "jobs": sorted(
            job_list,
            key=lambda job: job["created_at"],
            reverse=True,
        )
    }


PAGE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Private Video Converter</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f5f6f8;color:#17202a;margin:0}
main{max-width:920px;margin:36px auto;padding:0 18px}
.panel{background:white;border:1px solid #e3e6eb;border-radius:14px;padding:22px;margin:16px 0}
h1{margin-bottom:4px}
.muted{color:#667085}
.grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px}
label{display:block;font-size:14px;font-weight:600;margin-bottom:6px}
select,button{font:inherit;padding:11px;border-radius:8px;border:1px solid #ccd2da;width:100%;background:white}
button{background:#17202a;color:white;border:0;cursor:pointer;margin-top:16px}
button:disabled{opacity:.5;cursor:not-allowed}
button.cancel{background:#b42318;width:auto;padding:8px 14px;margin-top:10px}
progress{width:100%;height:14px}
.job{border-top:1px solid #eee;padding:14px 0}
.pill{display:inline-block;border-radius:20px;padding:4px 9px;background:#eef2f6;font-size:12px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}
.stat{background:#f7f8fa;border-radius:8px;padding:9px;min-width:0}
.stat b{display:block;font-size:14px;overflow-wrap:anywhere}
.stat small{color:#667085}
.stage{font-weight:600;margin:8px 0}
.details{color:#667085;font-size:13px;line-height:1.7}
@media(max-width:700px){.grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<main>
<h1>Video Converter</h1>
<div class="muted">Private Google Drive → FFmpeg → Google Drive. One job at a time.</div>

<div class="panel">
<h2>Choose a video</h2>
<p class="muted">Files are listed from VideoConverter/Input. Originals are mounted read-only.</p>
<button id="refresh" onclick="loadFiles()">Refresh Drive files</button>
<div style="margin-top:16px">
<label for="file">Input video</label>
<select id="file"><option>Loading…</option></select>
</div>

<div class="grid" style="margin-top:16px">
<div>
<label for="crf">Quality (CRF)</label>
<select id="crf">
<option value="28" selected>28 — smaller file</option>
<option value="26">26 — balanced</option>
<option value="24">24 — higher quality</option>
<option value="22">22 — very high quality</option>
</select>
</div>
<div>
<label for="preset">Speed preset</label>
<select id="preset">
<option>veryfast</option><option>faster</option><option>fast</option><option>medium</option>
</select>
</div>
<div>
<label for="test">Test duration</label>
<select id="test">
<option value="120">2 minutes (test)</option>
<option value="300">5 minutes (test)</option>
<option value="60">1 minute (test)</option>
<option value="0">Full video</option>
</select>
</div>
</div>

<button id="start" onclick="startJob()">Start conversion</button>
<p class="muted">Use short tests first. Do not run full conversion on very large files until storage and cache behavior are validated.</p>
</div>

<div class="panel">
<h2>Jobs</h2>
<div id="jobs" class="muted">No jobs yet.</div>
</div>
</main>

<script>
async function api(url, options={}) {
    const response = await fetch(url, {
        headers: {"Content-Type":"application/json"},
        ...options
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || "Request failed");
    return data;
}

async function loadFiles() {
    const select = document.getElementById("file");
    select.innerHTML = "<option>Loading…</option>";
    try {
        const data = await api("/api/files");
        select.innerHTML = "";
        for (const file of data.files) {
            const option = document.createElement("option");
            option.value = file.name;
            option.textContent = `${file.name} (${file.size_human})`;
            select.appendChild(option);
        }
        if (!data.files.length) {
            select.innerHTML = '<option value="">No supported videos found</option>';
        }
    } catch (error) {
        select.innerHTML = "";
        const option = document.createElement("option");
        option.textContent = error.message;
        select.appendChild(option);
    }
}

async function startJob() {
    const button = document.getElementById("start");
    try {
        button.disabled = true;
        await api("/api/jobs", {
            method: "POST",
            body: JSON.stringify({
                filename: document.getElementById("file").value,
                crf: Number(document.getElementById("crf").value),
                preset: document.getElementById("preset").value,
                test_seconds: Number(document.getElementById("test").value)
            })
        });
        await loadJobs();
    } catch (error) {
        alert(error.message);
    } finally {
        button.disabled = false;
    }
}

async function cancelJob(id) {
    if (!confirm("Cancel this job? Incomplete local output will be deleted.")) return;
    try {
        await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, {method:"POST"});
        await loadJobs();
    } catch (error) {
        alert(error.message);
    }
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
    }[character]));
}

async function loadJobs() {
    try {
        const data = await api("/api/jobs");
        const container = document.getElementById("jobs");

        if (!data.jobs.length) {
            container.textContent = "No jobs yet.";
            return;
        }

        container.innerHTML = data.jobs.map(job => {
            const active = ["queued", "running"].includes(job.status);
            const stats = [
                ["Stage", job.stage || job.status],
                ["Speed", job.speed || "—"],
                ["FPS", job.fps || "—"],
                ["ETA", job.eta || "—"],
                ["Elapsed", job.elapsed || "—"],
                ["Video processed", `${job.video_time || "—"} / ${job.target_duration || job.duration || "—"}`],
                ["Output size", job.output_size || "—"],
                ["Upload", job.upload_progress != null ? `${job.upload_progress}%` : "—"]
            ];

            return `
            <div class="job">
                <b>${escapeHtml(job.filename)}</b>
                <span class="pill">${escapeHtml(job.status)}</span>
                <div class="stage">${escapeHtml(job.message || "")}</div>
                <progress max="100" value="${Number(job.progress || 0)}"></progress>
                <small>${Number(job.progress || 0)}%</small>
                <div class="stats">
                    ${stats.map(([label, value]) => `
                        <div class="stat">
                            <small>${escapeHtml(label)}</small>
                            <b>${escapeHtml(value)}</b>
                        </div>
                    `).join("")}
                </div>
                ${active ? `<button class="cancel" onclick="cancelJob('${escapeHtml(job.id)}')">Cancel job</button>` : ""}
                ${job.remote_path ? `<p>Saved: ${escapeHtml(job.remote_path)}</p>` : ""}
                ${job.local_output ? `<p>Local recovery file: ${escapeHtml(job.local_output)}</p>` : ""}
            </div>`;
        }).join("");
    } catch (error) {
        document.getElementById("jobs").textContent = error.message;
    }
}

loadFiles();
loadJobs();
setInterval(loadJobs, 1500);
</script>
</body>
</html>
"""
