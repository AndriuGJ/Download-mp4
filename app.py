import os
import json
import base64
import uuid
import time
import threading
import yt_dlp
from flask import Flask, render_template, request, send_file, jsonify, Response

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = None
cookies_b64 = os.environ.get("COOKIES_B64")
if cookies_b64:
    COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    with open(COOKIES_FILE, "wb") as f:
        f.write(base64.b64decode(cookies_b64))

jobs = {}


def get_ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts


def progress_hook(d, job_id):
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        current = d.get("downloaded_bytes", 0)
        speed = d.get("speed")
        eta = d.get("eta")
        percent = round((current / total) * 100, 1) if total else 0
        jobs[job_id].update({
            "status": "downloading",
            "percent": percent,
            "speed": round(speed / 1048576, 1) if speed else None,
            "eta": eta,
        })
    elif d["status"] == "finished":
        jobs[job_id].update({"status": "processing", "percent": 100})


def run_download(job_id, url, format_id, is_audio, custom_name=None):
    try:
        outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title).150s.%(ext)s")
        ydl_opts = get_ydl_opts({
            "outtmpl": outtmpl,
            "restrictfilenames": True,
            "progress_hooks": [lambda d: progress_hook(d, job_id)],
        })

        if is_audio:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        elif format_id == "best":
            ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            ydl_opts["merge_output_format"] = "mp4"
        else:
            ydl_opts["format"] = f"{format_id}+bestaudio/best"
            ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            if is_audio:
                filename = os.path.splitext(filename)[0] + ".mp3"
            elif not os.path.exists(filename):
                base = os.path.splitext(filename)[0]
                filename = base + ".mp4"

            clean_name = os.path.basename(filename)
            if clean_name.startswith(job_id + "_"):
                clean_name = clean_name[len(job_id) + 1:]
            ext = os.path.splitext(clean_name)[1]
            if custom_name:
                clean_name = custom_name + ext
            else:
                name_no_ext = os.path.splitext(clean_name)[0]
                if len(name_no_ext.encode("utf-8")) > 200:
                    name_no_ext = name_no_ext[:200].rstrip()
                clean_name = name_no_ext + ext
            clean_path = os.path.join(DOWNLOAD_DIR, clean_name)
            if filename != clean_path:
                os.rename(filename, clean_path)
                filename = clean_path

        jobs[job_id].update({"status": "done", "filename": filename})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
def video_info():
    url = request.json.get("url", "")
    if not url:
        return jsonify({"error": "No se proporciono una URL"}), 400

    try:
        ydl_opts = get_ydl_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        seen = set()
        for f in info.get("formats", []):
            key = f.get("format_note") or f.get("resolution", "unknown")
            ext = f.get("ext", "")
            filesize = f.get("filesize") or f.get("filesize_approx")
            if ext == "mp4" and f.get("vcodec") != "none":
                label = f"{key} (mp4)"
                if label not in seen:
                    seen.add(label)
                    formats.append({
                        "format_id": f["format_id"],
                        "label": label,
                        "filesize": round(filesize / 1048576, 1) if filesize else None,
                    })

        formats.insert(0, {
            "format_id": "best",
            "label": "Mejor calidad (mp4)",
            "filesize": None,
        })

        formats.append({
            "format_id": "mp3",
            "label": "Solo audio (mp3)",
            "filesize": None,
        })

        return jsonify({
            "title": info.get("title", "Sin titulo"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", "Desconocido"),
            "formats": formats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/download", methods=["POST"])
def start_download():
    url = request.form.get("url", "")
    format_id = request.form.get("format_id", "best")
    custom_name = request.form.get("custom_name", "").strip() or None

    if not url:
        return jsonify({"error": "No se proporciono una URL"}), 400

    job_id = uuid.uuid4().hex[:12]
    is_audio = format_id == "mp3"
    jobs[job_id] = {"status": "starting", "percent": 0}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_id, is_audio, custom_name))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    def generate():
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job no encontrado'})}\n\n"
                break
            yield f"data: {json.dumps(job)}\n\n"
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/file/<job_id>")
def serve_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Archivo no listo"}), 404

    filename = job["filename"]
    response = send_file(filename, as_attachment=True)

    def cleanup():
        time.sleep(5)
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except OSError:
            pass

    thread = threading.Thread(target=cleanup)
    thread.daemon = True
    thread.start()

    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
