from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import sqlite3, os, io, xlsxwriter
from datetime import datetime
from dotenv import load_dotenv

# ===== Usuarios / seguridad =====
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

# ===== OpenAI (opcional, ya lo usas) =====
try:
    from openai import OpenAI
except:
    OpenAI = None

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-this")

# Firmador de tokens (confirmación)
serializer = URLSafeTimedSerializer(SECRET_KEY)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

DB = 'pymax.db'


# ====== DB: inicialización ======
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # transacciones
    c.execute('''
      CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        type TEXT,
        category TEXT,
        amount REAL,
        client TEXT,
        note TEXT
      )
    ''')
    # índices
    c.execute('CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tx_type ON transactions(type)')

    # usuarios (nuevo)
    c.execute('''
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
      )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
    conn.commit()
    conn.close()

init_db()


# ====== Helpers DB ======
def db_execute(query, params=()):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(query, params)
    conn.commit()
    last = c.lastrowid
    conn.close()
    return last

def db_query(query, params=()):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows


# =========================================================
# ===============  MÓDULO: USUARIOS  ======================
# =========================================================

# POST /api/register  -> crea usuario (no confirmado) y devuelve confirm_url
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not name or not email or not password:
        return jsonify({'error': 'name, email y password son requeridos'}), 400

    # existe?
    existing = db_query('SELECT id FROM users WHERE email=?', (email,))
    if existing:
        return jsonify({'error': 'El email ya está registrado'}), 409

    # hash
    ph = generate_password_hash(password)
    now = datetime.utcnow().isoformat(timespec='seconds')

    try:
        db_execute(
            'INSERT INTO users (name,email,password_hash,confirmed,created_at) VALUES (?,?,?,?,?)',
            (name, email, ph, 0, now)
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # token firmado (expira a las 48h, se valida en /api/confirm)
    token = serializer.dumps({'email': email})
    # NO construimos aquí el dominio (lo arma el front con tu BASE_URL),
    # pero por conveniencia devolvemos solo el token:
    return jsonify({
        'message': 'Usuario creado. Enviar email de confirmación.',
        'token': token
    }), 201


# GET /api/confirm?token=...
@app.route('/api/confirm', methods=['GET'])
def confirm():
    token = request.args.get('token', '').strip()
    if not token:
        return jsonify({'error': 'token requerido'}), 400

    try:
        data = serializer.loads(token, max_age=172800)  # 48 horas
        email = (data.get('email') or '').lower()
    except SignatureExpired:
        return jsonify({'error': 'Token expirado'}), 400
    except BadSignature:
        return jsonify({'error': 'Token inválido'}), 400

    # marcar confirmado
    rows = db_query('SELECT id,confirmed FROM users WHERE email=?', (email,))
    if not rows:
        return jsonify({'error': 'Usuario no encontrado'}), 404

    uid, confirmed = rows[0]
    if confirmed:
        return jsonify({'message': 'Cuenta ya estaba confirmada'}), 200

    db_execute('UPDATE users SET confirmed=1 WHERE id=?', (uid,))
    return jsonify({'message': 'Cuenta confirmada correctamente'}), 200


# POST /api/login  -> valida credenciales y confirmación
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'email y password son requeridos'}), 400

    rows = db_query('SELECT id,name,password_hash,confirmed FROM users WHERE email=?', (email,))
    if not rows:
        return jsonify({'error': 'Credenciales inválidas'}), 401

    uid, name, phash, confirmed = rows[0]
    if not check_password_hash(phash, password):
        return jsonify({'error': 'Credenciales inválidas'}), 401

    if not confirmed:
        return jsonify({'error': 'Correo no confirmado'}), 403

    # (Opcional) generar un token de sesión simple firmado
    session_token = serializer.dumps({'uid': uid, 'email': email})
    return jsonify({'message': 'ok', 'name': name, 'email': email, 'session_token': session_token})


# =========================================================
# =========  TUS ENDPOINTS FINANCIEROS EXISTENTES  =========
# =========================================================

# Agregar transacción
@app.route('/api/transaction', methods=['POST'])
def add_transaction():
    data = request.get_json()
    date = data.get('date')
    typ = data.get('type')
    category = data.get('category','General')
    amount = float(data.get('amount',0))
    client = data.get('client','')
    note = data.get('note','')
    # Validación simple
    if not date or not typ:
        return jsonify({'error':'date y type requeridos'}), 400
    db_execute('INSERT INTO transactions (date,type,category,amount,client,note) VALUES (?,?,?,?,?,?)',
               (date,typ,category,amount,client,note))
    return jsonify({'message':'ok'}), 201

# Obtener transacciones
@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    date = request.args.get('date')
    if date:
        rows = db_query('SELECT id,date,type,category,amount,client,note FROM transactions WHERE date=? ORDER BY id DESC', (date,))
    else:
        rows = db_query('SELECT id,date,type,category,amount,client,note FROM transactions ORDER BY date DESC, id DESC LIMIT 1000')
    cols = ['id','date','type','category','amount','client','note']
    data = [dict(zip(cols,row)) for row in rows]
    return jsonify(data)

# Resumen por fecha
@app.route('/api/summary', methods=['GET'])
def summary():
    date = request.args.get('date')
    if not date: return jsonify({'error':'date required'}), 400
    rows = db_query('SELECT type, SUM(amount) FROM transactions WHERE date=? GROUP BY type',(date,))
    res = {r[0]: r[1] for r in rows}
    ventas = res.get('venta',0); compras = res.get('compra',0); gastos=res.get('gasto',0)
    utilidad = ventas - (compras + gastos)
    return jsonify({'ventas':ventas,'compras':compras,'gastos':gastos,'utilidad':utilidad})

# Estado de Resultados
@app.route('/api/estado', methods=['GET'])
def estado():
    start = request.args.get('start')
    end = request.args.get('end')
    if not start or not end: return jsonify({'error':'start and end required'}), 400
    rows = db_query('SELECT type, SUM(amount) FROM transactions WHERE date BETWEEN ? AND ? GROUP BY type', (start, end))
    res = {r[0]: r[1] for r in rows}
    ventas = res.get('venta',0); compras = res.get('compra',0); gastos=res.get('gasto',0)
    utilidad_bruta = ventas - compras
    utilidad_neta = utilidad_bruta - gastos
    impuesto_estimado = utilidad_neta * 0.25 if utilidad_neta>0 else 0
    estado = {
      'ventas':ventas,'compras':compras,'gastos':gastos,
      'utilidad_bruta':utilidad_bruta,'utilidad_neta':utilidad_neta,'impuesto_estimado':impuesto_estimado
    }
    return jsonify(estado)

# Exportar Excel
@app.route('/api/export', methods=['GET'])
def export():
    start = request.args.get('start'); end = request.args.get('end')
    if not start or not end: return jsonify({'error':'start and end required'}), 400
    rows = db_query('SELECT date,type,category,amount,client,note FROM transactions WHERE date BETWEEN ? AND ? ORDER BY date, id', (start,end))
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    ws = workbook.add_worksheet('Transacciones')
    headers = ['Fecha','Tipo','Categoria','Importe','Cliente','Nota']
    for c, h in enumerate(headers): ws.write(0,c,h)
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row):
            ws.write(r,c,val)
    workbook.close()
    output.seek(0)
    filename = f"Transacciones_{start}_to_{end}.xlsx"
    return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# IA (opcional)
@app.route('/api/ai_advice', methods=['POST'])
def ai_advice():
    if not OPENAI_API_KEY or not OpenAI:
        return jsonify({'error':'AI not configured on server'}), 500
    client = OpenAI(api_key=OPENAI_API_KEY)
    payload = request.get_json() or {}
    prompt = payload.get('prompt','').strip()
    if not prompt: return jsonify({'error':'prompt is required'}), 400
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Eres un asesor contable y financiero profesional."},
                {"role":"user","content": prompt}
            ],
            max_tokens=800
        )
        text = resp.choices[0].message.content
        return jsonify({'answer': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===== Main =====
if __name__ == '__main__':
    app.run(debug=True, port=5000)
