from flask import Flask, render_template, jsonify
from webshare_signup import run_automation
import traceback

app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start_process():
    try:
        # Blocks and runs the proxy generation.
        result = run_automation()
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Server Error: {str(e)}"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
