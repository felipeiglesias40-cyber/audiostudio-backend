from flask import Flask, render_template, request, jsonify, session, send_file, send_from_directory
from pymongo import MongoClient
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import requests
import uuid
import time
from bson import ObjectId
import threading

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'chave-super-secreta')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['ALLOWED_EXTENSIONS'] = {'mp3', 'wav', 'flac', 'm4a'}

# MongoDB
uri = os.getenv("MONGODB_URI")
db_name = os.getenv("DB_NAME")
client = MongoClient(uri)
db = client[db_name]
users = db["users"]
songs = db["songs"]

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# ============================================
# ROTAS DE AUTENTICAÇÃO
# ============================================

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    name = data.get('name', 'Usuário')
    
    if users.find_one({'email': email}):
        return jsonify({'error': 'Email já cadastrado'}), 400
    
    user = {
        'email': email,
        'name': name,
        'password_hash': generate_password_hash(password),
        'created_at': datetime.utcnow(),
        'plan': 'free',
        'monthly_usage': 0
    }
    
    result = users.insert_one(user)
    session['user_id'] = str(result.inserted_id)
    return jsonify({'success': True}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    
    user = users.find_one({'email': email})
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Email ou senha inválidos'}), 401
    
    session['user_id'] = str(user['_id'])
    return jsonify({'success': True})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/me', methods=['GET'])
def me():
    if 'user_id' not in session:
        return jsonify({'error': 'Não logado'}), 401
    return jsonify({'user': {'id': session['user_id']}})

@app.route('/usage', methods=['GET'])
def usage():
    if 'user_id' not in session:
        return jsonify({'error': 'Não logado'}), 401
    user = users.find_one({'_id': ObjectId(session['user_id'])})
    return jsonify({
        'usage': user.get('monthly_usage', 0),
        'limit': 5,
        'remaining': 5 - user.get('monthly_usage', 0)
    })

# ============================================
# ROTAS DE UPLOAD (COM COLAB)
# ============================================

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'user_id' not in session:
        return jsonify({'error': 'Faça login primeiro'}), 401
    
    user = users.find_one({'_id': ObjectId(session['user_id'])})
    if user.get('monthly_usage', 0) >= 5:
        return jsonify({'error': 'Limite mensal atingido (5 músicas)'}), 403
    
    file = request.files['file']
    if not file or not allowed_file(file.filename):
        return jsonify({'error': 'Arquivo inválido'}), 400
    
    filename = secure_filename(file.filename)
    unique_id = str(uuid.uuid4())[:8]
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_{filename}")
    file.save(filepath)
    
    song = {
        'user_id': ObjectId(session['user_id']),
        'filename': filename,
        'filepath': filepath,
        'status': 'processing',
        'created_at': datetime.utcnow(),
        'stems': []
    }
    result = songs.insert_one(song)
    song_id = str(result.inserted_id)
    
    # Processar com Colab em background
    thread = threading.Thread(target=process_with_colab, args=(song_id, filepath, filename, session['user_id']))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True, 
        'song_id': song_id,
        'message': 'Processamento iniciado (30-60 segundos)'
    })

def process_with_colab(song_id, filepath, filename, user_id):
    """Processa usando o servidor Colab com GPU"""
    try:
        COLAB_URL = os.getenv('COLAB_URL')
        if not COLAB_URL:
            raise Exception("COLAB_URL não configurada no .env")
        
        print(f"🎵 Enviando para Colab: {filename}")
        print(f"📡 URL: {COLAB_URL}/upload")
        
        # 1. Upload para o Colab
        with open(filepath, 'rb') as f:
            files = {'file': (filename, f, 'audio/mpeg')}
            response = requests.post(
                f"{COLAB_URL}/upload",
                files=files,
                timeout=30
            )
        
        if response.status_code != 200:
            raise Exception(f"Erro no upload: {response.text}")
        
        data = response.json()
        task_id = data['task_id']
        print(f"📤 Task ID: {task_id}")
        
        # 2. Aguardar processamento
        tentativas = 0
        while tentativas < 60:  # 5 minutos máximo
            time.sleep(5)
            tentativas += 1
            
            status_response = requests.get(f"{COLAB_URL}/status/{task_id}")
            if status_response.status_code == 200:
                status_data = status_response.json()
                status = status_data.get('status')
                
                print(f"⏳ Status: {status} ({tentativas*5}s)")
                
                if status == 'completed':
                    print("✅ Processamento concluído!")
                    arquivos = status_data.get('arquivos', [])
                    
                    # 3. Baixar arquivos
                    output_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'separated', song_id)
                    os.makedirs(output_dir, exist_ok=True)
                    
                    stems_baixados = []
                    for arq in arquivos:
                        download_url = f"{COLAB_URL}/download/{task_id}/{arq}"
                        download_response = requests.get(download_url, stream=True)
                        
                        if download_response.status_code == 200:
                            caminho = os.path.join(output_dir, arq)
                            with open(caminho, 'wb') as f:
                                for chunk in download_response.iter_content(8192):
                                    f.write(chunk)
                            stems_baixados.append(arq)
                            print(f"   ✅ {arq}")
                    
                    # 4. Atualizar banco
                    songs.update_one(
                        {'_id': ObjectId(song_id)},
                        {'$set': {
                            'status': 'completed',
                            'stems': stems_baixados,
                            'output_path': output_dir
                        }}
                    )
                    users.update_one(
                        {'_id': ObjectId(user_id)},
                        {'$inc': {'monthly_usage': 1}}
                    )
                    print(f"✅ Processado: {filename}")
                    return True
                    
                elif status == 'failed':
                    erro = status_data.get('erro', 'Erro desconhecido')
                    raise Exception(f"Falha no processamento: {erro}")
        
        raise Exception("Tempo limite excedido")
        
    except Exception as e:
        print(f"❌ Erro: {e}")
        songs.update_one(
            {'_id': ObjectId(song_id)},
            {'$set': {'status': 'error', 'error': str(e)}}
        )
        return False

@app.route('/status/<song_id>', methods=['GET'])
def get_status(song_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Não logado'}), 401
    
    song = songs.find_one({'_id': ObjectId(song_id)})
    return jsonify({
        'status': song.get('status'),
        'stems': song.get('stems', []),
        'error': song.get('error')
    })

@app.route('/download/<song_id>/<stem>', methods=['GET'])
def download_stem(song_id, stem):
    if 'user_id' not in session:
        return jsonify({'error': 'Não logado'}), 401
    
    song = songs.find_one({'_id': ObjectId(song_id)})
    stem_path = os.path.join(song.get('output_path', ''), stem)
    
    if os.path.exists(stem_path):
        return send_file(stem_path, as_attachment=True)
    return jsonify({'error': 'Arquivo não encontrado'}), 404

@app.route('/songs', methods=['GET'])
def list_songs():
    if 'user_id' not in session:
        return jsonify({'error': 'Não logado'}), 401
    
    user_songs = songs.find(
        {'user_id': ObjectId(session['user_id'])},
        sort=[('created_at', -1)]
    )
    
    return jsonify({'songs': [
        {'id': str(s['_id']), 'filename': s['filename'], 'status': s['status'], 'stems': s.get('stems', [])}
        for s in user_songs
    ]})

# ============================================
# PÁGINAS
# ============================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/dashboard')
def dashboard():
    return send_from_directory('.', 'dashboard.html')

# ============================================
# INICIAR
# ============================================

if __name__ == '__main__':
    print("="*60)
    print("🎵 AudioStudio Online - Com GPU no Colab")
    print("="*60)
    print("📁 Uploads: uploads/")
    print("🔗 Acesse: http://localhost:5000")
    print("⚡ Processamento: 30-60 segundos (GPU)")
    print("💰 Custo: R$ 0")
    print("="*60)
    app.run(debug=True, port=5000)