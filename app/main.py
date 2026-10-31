import os, re, json, uuid, shutil, threading, subprocess, time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
import secrets

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
worker_thread = None
mount_process = None

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, APP_USER)
    ok_pass = secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not APP_PASSWORD or not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Authentication required",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username

def run_cmd(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def check_remote():
    if not Path(RCLONE_CONFIG).exists():
        raise RuntimeError(f"Missing rclone config at {RCLONE_CONFIG}")
    p = run_cmd(["rclone", "listremotes"])
    if p.returncode != 0 or "gdrive:" not in p.stdout.splitlines():
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
        "--read-only", "--vfs-cache-mode", "minimal",
        "--vfs-read-chunk-size", "32M",
        "--vfs-read-chunk-size-limit", "256M",
        "--cache-dir", str(CACHE_DIR),
        "--dir-cache-time", "1m",
        "--log-level", "INFO"
    ]
    mount_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        if os.path.ismount(MOUNT_PATH):
            return
        if mount_process.poll() is not None:
            raise RuntimeError("rclone mount exited. Check FUSE permissions and rclone configuration.")
        time.sleep(1)
    raise RuntimeError("Timed out waiting for Google Drive mount")

def list_input_files():
    p = run_cmd(["rclone", "lsjson", INPUT_REMOTE, "--files-only"])
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "Could not list Google Drive input folder")
    items = json.loads(p.stdout or "[]")
    result = []
    for item in items:
        name = item.get("Name", "")
        if name.lower().endswith((".mov", ".mp4", ".m4v", ".mkv", ".avi")):
            result.append({"name": name, "size": item.get("Size", 0), "size_human": human(item.get("Size", 0))})
    return sorted(result, key=lambda x: x["name"].lower())

def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024

def update_job(job_id, **kwargs):
    with jobs_lock:
        jobs[job_id].update(kwargs)
        jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

def process_job(job_id, filename, crf, preset, test_seconds):
    output = None
    try:
        update_job(job_id, status="running", message="Checking input file", progress=0)
        # Exact path is constrained to a single filename from Drive listing.
        if Path(filename).name != filename or filename in ("", ".", ".."):
            raise ValueError("Invalid filename")
        source = MOUNT_PATH / filename
        if not source.exists():
            raise RuntimeError("Source file is not visible in the mounted Drive folder")
        stem = Path(filename).stem
        output_name = f"{stem}.mp4"
        output = WORK_DIR / f"{job_id}-{output_name}"
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(WORK_DIR).free
        if free < 20 * 1024**3:
            raise RuntimeError(f"Only {human(free)} free on server; refusing to start with less than 20 GB.")
        update_job(job_id, message="Encoding with FFmpeg", output_name=output_name, progress=1)
        cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(source)]
        if test_seconds:
            cmd += ["-t", str(test_seconds)]
        cmd += [
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(output)
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        duration = None
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    out_sec = int(line.split("=",1)[1]) / 1_000_000
                    if duration:
                        pct = min(95, int(out_sec / duration * 95))
                        update_job(job_id, progress=max(1, pct), message="Encoding with FFmpeg")
                except ValueError:
                    pass
            elif line.startswith("progress=end"):
                break
        code = proc.wait()
        if code != 0 or not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg failed. Output was not uploaded.")
        update_job(job_id, progress=96, message="Uploading MP4 to Google Drive")
        dest = f"{OUTPUT_REMOTE}/{output_name}"
        up = run_cmd(["rclone", "copyto", str(output), dest, "--retries", "3", "--low-level-retries", "10"])
        if up.returncode != 0:
            raise RuntimeError("Upload failed; local output retained for recovery: " + up.stderr[-1200:])
        update_job(job_id, progress=99, message="Verifying uploaded file")
        verify = run_cmd(["rclone", "lsjson", dest, "--stat"])
        if verify.returncode != 0:
            raise RuntimeError("Could not verify remote upload; local output retained.")
        remote_info = json.loads(verify.stdout or "{}")
        local_size = output.stat().st_size
        if int(remote_info.get("Size", -1)) != local_size:
            raise RuntimeError("Remote size does not match local output; local output retained.")
        output.unlink(missing_ok=True)
        update_job(job_id, status="completed", progress=100, message="Upload verified; temporary output deleted",
                   output_size=human(local_size), remote_path=dest)
    except Exception as e:
        update_job(job_id, status="failed", message=str(e), progress=0,
                   local_output=str(output) if output and output.exists() else None)

def worker_loop():
    while True:
        with queue_lock:
            pending = next((j for j in jobs.values() if j["status"] == "queued"), None)
        if pending:
            process_job(pending["id"], pending["filename"], pending["crf"], pending["preset"], pending["test_seconds"])
        else:
            time.sleep(1)

@app.on_event("startup")
def startup():
    global worker_thread
    start_mount()
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

@app.on_event("shutdown")
def shutdown():
    global mount_process
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
    except Exception as e:
        raise HTTPException(500, str(e))

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
    if preset not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"):
        raise HTTPException(400, "Unsupported preset")
    if test_seconds not in (0, 60, 120, 300):
        raise HTTPException(400, "Test duration must be 0, 60, 120 or 300 seconds")
    try:
        available = {x["name"] for x in list_input_files()}
    except Exception as e:
        raise HTTPException(500, str(e))
    if filename not in available:
        raise HTTPException(404, "File not found in Google Drive Input folder")
    with jobs_lock:
        active = any(j["status"] in ("queued", "running") for j in jobs.values())
        if active:
            raise HTTPException(409, "A job is already queued or running. This MVP processes one video at a time.")
        job_id = str(uuid.uuid4())[:8]
        jobs[job_id] = {"id": job_id, "filename": filename, "status": "queued", "progress": 0,
                        "message": "Queued", "crf": crf, "preset": preset,
                        "test_seconds": test_seconds, "created_at": datetime.now(timezone.utc).isoformat()}
    return jobs[job_id]

@app.get("/api/jobs")
def get_jobs(_=Depends(auth)):
    with jobs_lock:
        return {"jobs": sorted(jobs.values(), key=lambda x: x["created_at"], reverse=True)}

PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Private Video Converter</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f5f6f8;color:#17202a;margin:0}
main{max-width:920px;margin:36px auto;padding:0 18px}.panel{background:white;border:1px solid #e3e6eb;border-radius:14px;padding:22px;margin:16px 0}
h1{margin-bottom:4px}.muted{color:#667085}.grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px}
label{display:block;font-size:14px;font-weight:600;margin-bottom:6px}select,button{font:inherit;padding:11px;border-radius:8px;border:1px solid #ccd2da;width:100%;background:white}
button{background:#17202a;color:white;border:0;cursor:pointer;margin-top:16px}button:disabled{opacity:.5;cursor:not-allowed}
progress{width:100%;height:14px} .job{border-top:1px solid #eee;padding:14px 0}.pill{display:inline-block;border-radius:20px;padding:4px 9px;background:#eef2f6;font-size:12px}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<h1>Video Converter</h1><div class="muted">Private Google Drive → FFmpeg → Google Drive. One job at a time.</div>
<div class="panel"><h2>Choose a video</h2><p class="muted">Files are listed from VideoConverter/Input. Originals are mounted read-only.</p>
<button id="refresh" onclick="loadFiles()">Refresh Drive files</button><div style="margin-top:16px">
<label for="file">Input video</label><select id="file"><option>Loading…</option></select></div>
<div class="grid" style="margin-top:16px">
<div><label for="crf">Quality (CRF)</label><select id="crf"><option value="28" selected>28 — smaller file</option><option value="26">26 — balanced</option><option value="24">24 — higher quality</option><option value="22">22 — very high quality</option></select></div>
<div><label for="preset">Speed preset</label><select id="preset"><option>veryfast</option><option>faster</option><option>fast</option><option>medium</option></select></div>
<div><label for="test">Test duration</label><select id="test"><option value="120">2 minutes (test)</option><option value="300">5 minutes (test)</option><option value="60">1 minute (test)</option><option value="0">Full video</option></select></div></div>
<button id="start" onclick="startJob()">Start conversion</button><p class="muted">The first tests should use 1–5 minutes. Full conversion is enabled but do not use it on very large client files until storage and cache behavior are validated.</p>
</div>
<div class="panel"><h2>Jobs</h2><div id="jobs" class="muted">No jobs yet.</div></div>
</main><script>
async function api(url,opts={}){let r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});let j=await r.json();if(!r.ok)throw Error(j.detail||'Request failed');return j}
async function loadFiles(){let s=document.getElementById('file');s.innerHTML='<option>Loading…</option>';try{let d=await api('/api/files');s.innerHTML='';for(let f of d.files){let o=document.createElement('option');o.value=f.name;o.textContent=f.name+' ('+f.size_human+')';s.appendChild(o)}if(!d.files.length)s.innerHTML='<option value="">No supported videos found</option>'}catch(e){s.innerHTML='<option>'+e.message+'</option>'}}
async function startJob(){try{let b=document.getElementById('start');b.disabled=true;await api('/api/jobs',{method:'POST',body:JSON.stringify({filename:document.getElementById('file').value,crf:+document.getElementById('crf').value,preset:document.getElementById('preset').value,test_seconds:+document.getElementById('test').value})});await loadJobs()}catch(e){alert(e.message)}finally{document.getElementById('start').disabled=false}}
async function loadJobs(){try{let d=await api('/api/jobs');let el=document.getElementById('jobs');if(!d.jobs.length){el.textContent='No jobs yet.';return}el.innerHTML=d.jobs.map(j=>`<div class="job"><b>${esc(j.filename)}</b> <span class="pill">${esc(j.status)}</span><p>${esc(j.message||'')}</p><progress max="100" value="${j.progress||0}"></progress><small>${j.progress||0}% · ${esc(j.output_size||'')}</small>${j.remote_path?'<p>Saved: '+esc(j.remote_path)+'</p>':''}${j.local_output?'<p>Local recovery file: '+esc(j.local_output)+'</p>':''}</div>`).join('')}catch(e){document.getElementById('jobs').textContent=e.message}}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
loadFiles();loadJobs();setInterval(loadJobs,3000);
</script></body></html>
"""
