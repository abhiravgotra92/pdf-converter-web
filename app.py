import asyncio
import os
import queue
import tempfile
import threading
import uuid

from flask import Flask, Response, jsonify, render_template, request, send_file

import crawler

app = Flask(__name__)

APP_PASSWORD = os.environ.get('APP_PASSWORD', 'artest')

# In-memory job store  {job_id: {'status', 'pdf_path', 'domain', 'error', 'progress'}}
jobs = {}
job_lock = threading.Lock()


def run_job(job_id, url, work_dir):
    def progress_cb(done, total):
        with job_lock:
            jobs[job_id]['progress'] = f"Crawled {done} / {total} pages..."

    try:
        with job_lock:
            jobs[job_id]['status'] = 'running'
        pdf_path, domain = asyncio.run(crawler.run(url, work_dir, progress_cb))
        with job_lock:
            jobs[job_id].update({'status': 'done', 'pdf_path': pdf_path, 'domain': domain})
    except Exception as e:
        with job_lock:
            jobs[job_id].update({'status': 'error', 'error': str(e)})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/convert', methods=['POST'])
def convert():
    data     = request.get_json()
    password = (data or {}).get('password', '')
    url      = (data or {}).get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL is required.'}), 400
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    job_id   = str(uuid.uuid4())
    work_dir = tempfile.mkdtemp()

    with job_lock:
        jobs[job_id] = {'status': 'queued', 'pdf_path': None, 'domain': None, 'error': None, 'progress': 'Starting...'}

    thread = threading.Thread(target=run_job, args=(job_id, url, work_dir), daemon=True)
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found.'}), 404
    return jsonify({
        'status':   job['status'],
        'progress': job.get('progress', ''),
        'domain':   job.get('domain', ''),
        'error':    job.get('error', ''),
    })


@app.route('/download/<job_id>')
def download(job_id):
    with job_lock:
        job = jobs.get(job_id)
    if not job or job['status'] != 'done':
        return jsonify({'error': 'PDF not ready.'}), 404
    filename = (job.get('domain') or 'output').replace('.', '_') + '.pdf'
    return send_file(job['pdf_path'], as_attachment=True, download_name=filename, mimetype='application/pdf')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
