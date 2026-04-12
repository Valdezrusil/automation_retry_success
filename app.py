from flask import Flask, render_template, Response, stream_with_context
from webshare_signup import run_automation
import traceback
import json

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST', 'GET'])
def start_process():
    def generate():
        try:
            # run_automation now yields progress dictionaries
            for progress in run_automation():
                # Yield it in SSE format
                yield f"data: {json.dumps(progress)}\n\n"
        except Exception as e:
            traceback.print_exc()
            error_data = {"status": "error", "message": f"Server Error: {str(e)}"}
            yield f"data: {json.dumps(error_data)}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
