import os
import signal
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, request


app = Flask(__name__)

ROOT = "/workspace"
PORT = 9003

jobs = {}
jobs_lock = threading.Lock()


def run_job(job_id, command, cwd):
    job = jobs[job_id]

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )

        job["pid"] = process.pid
        job["status"] = "running"
        job["started_at"] = time.time()

        for line in process.stdout:
            job["logs"].append(line.rstrip("\n"))

        process.wait()

        job["exit_code"] = process.returncode
        job["finished_at"] = time.time()

        if process.returncode == 0:
            job["status"] = "success"
        else:
            job["status"] = "failed"

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["finished_at"] = time.time()


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "dragster-runner",
    })


@app.post("/run")
def run():
    data = request.get_json(force=True)

    command = (data.get("command") or "").strip()
    cwd = (data.get("cwd") or ROOT).strip()

    if not command:
        return jsonify({
            "error": "command is required"
        }), 400

    cwd = os.path.abspath(cwd)

    if cwd != ROOT and not cwd.startswith(ROOT + "/"):
        return jsonify({
            "error": "cwd must be inside /workspace"
        }), 400

    if not os.path.isdir(cwd):
        return jsonify({
            "error": "working directory does not exist"
        }), 400

    job_id = uuid.uuid4().hex[:12]

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "command": command,
            "cwd": cwd,
            "status": "starting",
            "pid": None,
            "exit_code": None,
            "logs": [],
            "started_at": None,
            "finished_at": None,
        }

    thread = threading.Thread(
        target=run_job,
        args=(job_id, command, cwd),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "ok": True,
        "job": job_id,
    }), 202


@app.get("/jobs/<job_id>")
def job(job_id):
    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "job not found"
        }), 404

    return jsonify({
        **job,
        "logs": None,
    })


@app.get("/jobs/<job_id>/logs")
def logs(job_id):
    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "job not found"
        }), 404

    return jsonify({
        "id": job_id,
        "status": job["status"],
        "logs": job["logs"],
    })


@app.post("/jobs/<job_id>/stop")
def stop(job_id):
    job = jobs.get(job_id)

    if not job:
        return jsonify({
            "error": "job not found"
        }), 404

    pid = job.get("pid")

    if not pid:
        return jsonify({
            "ok": False,
            "error": "job has not started"
        }), 400

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        job["status"] = "stopped"

        return jsonify({
            "ok": True,
            "id": job_id,
        })

    except ProcessLookupError:
        job["status"] = "stopped"

        return jsonify({
            "ok": True,
            "id": job_id,
        })


@app.get("/jobs")
def list_jobs():
    return jsonify({
        "jobs": list(jobs.values())
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )