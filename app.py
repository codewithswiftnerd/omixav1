from flask import Flask
from flask_cors import CORS
from routes.upload import upload_bp
from routes.clean import clean_bp
from routes.export import export_bp
from routes.profile import profile_bp
from routes.auth import auth_bp
from core import user_store

app = Flask(__name__)
CORS(app)

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'

# Register blueprints
app.register_blueprint(upload_bp, url_prefix='/api')
app.register_blueprint(clean_bp, url_prefix='/api')
app.register_blueprint(export_bp, url_prefix='/api')
app.register_blueprint(profile_bp, url_prefix='/api')
app.register_blueprint(auth_bp, url_prefix='/api')

user_store.init_db()


# Always answer /api errors with JSON (never an HTML page), so the frontend can parse them
@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(500)
def json_errors(e):
    from flask import request
    code = getattr(e, 'code', 500)
    if request.path.startswith('/api'):
        return {'error': getattr(e, 'description', 'Server error')}, code
    return e

@app.route('/api/health', methods=['GET'])
def health():
    return {'status': 'ok', 'service': 'Omixa Data Cleaner'}, 200

if __name__ == '__main__':
    import os
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    app.run(debug=True, port=5000)
