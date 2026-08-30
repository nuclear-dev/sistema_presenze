import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for

app = Flask(__name__)
DB_FILE = 'database.db'

NOME_ASSOCIAZIONE = "Croce Verde Civitanova Marche"
PATH_LOGO = "logo.png"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS militi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode_id TEXT UNIQUE NOT NULL,
            nome TEXT NOT NULL,
            cognome TEXT NOT NULL,
            ruolo TEXT DEFAULT 'volontario',
            attivo INTEGER DEFAULT 1
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS timbrature (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            milite_id INTEGER NOT NULL,
            inizio_turno TEXT NOT NULL,
            fine_turno TEXT,
            ore_totali REAL,
            tag_turni TEXT,
            FOREIGN KEY(milite_id) REFERENCES militi(id)
        )
    ''')
    conn.commit()
    conn.close()

# Fasce espresse in minuti a partire da inizio giornata (0..1440)
FASCE = [
    {'code': 'M', 'nome': 'Mattina',    'inizio': 7 * 60,  'fine': 13 * 60},
    {'code': 'I', 'nome': 'Infraturno', 'inizio': 12 * 60, 'fine': 16 * 60},
    {'code': 'P', 'nome': 'Pomeriggio', 'inizio': 15 * 60, 'fine': 20 * 60},
    {'code': 'S', 'nome': 'Sera',       'inizio': 20 * 60, 'fine': 23 * 60},
    {'code': 'N', 'nome': 'Notte',      'inizio': 23 * 60, 'fine': (24 + 7) * 60} # 23:00 - 07:00 (31h)
]

def calcola_turno_e_tag(inizio_iso, fine_iso, is_assistenza=False):
    d_inizio = datetime.fromisoformat(inizio_iso)
    d_fine = datetime.fromisoformat(fine_iso)
    
    diff_sec = (d_fine - d_inizio).total_seconds()
    if diff_sec <= 0:
        return 0.0, ""

    ore = round(diff_sec / 3600.0, 2)

    if is_assistenza:
        return ore, "ASS"

    in_min = d_inizio.hour * 60 + d_inizio.minute
    durata_minuti = int(diff_sec // 60)
    fine_min = in_min + durata_minuti

    tags = []
    for f in FASCE:
        sovrapposizione = min(fine_min, f['fine']) - max(in_min, f['inizio'])
        durata_fascia = f['fine'] - f['inizio']
        # Una fascia viene taggata solo se il turno la copre per almeno il 50%
        # della sua durata (non basta piu' un semplice sconfinamento di pochi
        # minuti in una fascia adiacente, es. un turno 8-13 non deve prendere
        # anche il tag Infraturno solo perche' tocca 12-13).
        if sovrapposizione >= durata_fascia * 0.5:
            tags.append(f['code'])

    return ore, ", ".join(tags)

@app.route('/')
def index():
    return render_template('index.html', associazione=NOME_ASSOCIAZIONE, logo=PATH_LOGO)

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html', associazione=NOME_ASSOCIAZIONE, logo=PATH_LOGO)

@app.route('/militi')
def gestione_militi():
    sort_by = request.args.get('sort', 'cognome')
    sort_dir = request.args.get('dir', 'asc').upper()

    col = "cognome" if sort_by == 'cognome' else "nome"
    sec_col = "nome" if col == "cognome" else "cognome"
    
    order_clause = f"{col} {sort_dir}, {sec_col} ASC"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM militi ORDER BY {order_clause}")
    militi = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return render_template('militi.html', militi=militi, associazione=NOME_ASSOCIAZIONE, logo=PATH_LOGO, sort_current=sort_by, sort_dir=sort_dir.lower())

@app.route('/tessera/<int:milite_id>')
def genera_tessera(milite_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM militi WHERE id = ?", (milite_id,))
    milite_row = cursor.fetchone()
    conn.close()
    if not milite_row:
        return "Milite non trovato", 404
    return render_template('tessera.html', milite=dict(milite_row), associazione=NOME_ASSOCIAZIONE, logo=PATH_LOGO)

@app.route('/api/militi/salva', methods=['POST'])
def salva_milite():
    milite_id = request.form.get('id')
    barcode_id = request.form.get('barcode_id').strip()
    nome = request.form.get('nome').strip()
    cognome = request.form.get('cognome').strip()
    ruolo = request.form.get('ruolo')
    attivo = 1 if request.form.get('attivo') else 0

    conn = get_db_connection()
    cursor = conn.cursor()

    if milite_id:
        cursor.execute('''
            UPDATE militi 
            SET barcode_id = ?, nome = ?, cognome = ?, ruolo = ?, attivo = ? 
            WHERE id = ?
        ''', (barcode_id, nome, cognome, ruolo, attivo, milite_id))
    else:
        cursor.execute('''
            INSERT INTO militi (barcode_id, nome, cognome, ruolo, attivo) 
            VALUES (?, ?, ?, ?, ?)
        ''', (barcode_id, nome, cognome, ruolo, attivo))

    conn.commit()
    conn.close()
    return redirect(url_for('gestione_militi'))

@app.route('/api/timbra', methods=['POST'])
def timbra():
    data = request.json or {}
    barcode = data.get('barcode', '').strip()

    if not barcode:
        return jsonify({'errore': 'Codice a barre vuoto'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM militi WHERE barcode_id = ? AND attivo = 1", (barcode,))
    milite = cursor.fetchone()

    if not milite:
        conn.close()
        return jsonify({'errore': 'Tessera non trovata o disattivata'}), 404

    cursor.execute("SELECT * FROM timbrature WHERE milite_id = ? AND fine_turno IS NULL", (milite['id'],))
    t_aperta = cursor.fetchone()

    ora_attuale = datetime.now().isoformat()

    if not t_aperta:
        cursor.execute("INSERT INTO timbrature (milite_id, inizio_turno) VALUES (?, ?)", (milite['id'], ora_attuale))
        conn.commit()
        conn.close()
        return jsonify({
            'milite': f"{milite['cognome']} {milite['nome']}",
            'messaggio': 'INIZIO TURNO REGISTRATO'
        })
    else:
        ore, tags = calcola_turno_e_tag(t_aperta['inizio_turno'], ora_attuale)
        cursor.execute(
            "UPDATE timbrature SET fine_turno = ?, ore_totali = ?, tag_turni = ? WHERE id = ?",
            (ora_attuale, ore, tags, t_aperta['id'])
        )
        conn.commit()
        conn.close()
        return jsonify({
            'milite': f"{milite['cognome']} {milite['nome']}",
            'messaggio': f"FINE TURNO ({ore}h - Turni: {tags or 'Extra'})"
        })

@app.route('/api/report-matrix', methods=['GET'])
def report_matrix():
    da = request.args.get('da')
    a = request.args.get('a')
    sort_by = request.args.get('sort', 'cognome')
    sort_dir = request.args.get('dir', 'asc').upper()

    col = "cognome" if sort_by == 'cognome' else "nome"
    sec_col = "nome" if col == "cognome" else "cognome"
    order_clause = f"{col} {sort_dir}, {sec_col} ASC"

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(f"SELECT id, nome, cognome, ruolo FROM militi WHERE attivo = 1 ORDER BY {order_clause}")
    militi = [dict(r) for r in cursor.fetchall()]

    query_timbrature = '''
        SELECT id, milite_id, inizio_turno, fine_turno, ore_totali, tag_turni,
               strftime('%Y-%m-%d', inizio_turno) as giorno
        FROM timbrature 
        WHERE strftime('%Y-%m-%d', inizio_turno) >= ? AND strftime('%Y-%m-%d', inizio_turno) <= ?
    '''
    cursor.execute(query_timbrature, (da, a))
    timbrature = [dict(r) for r in cursor.fetchall()]
    conn.close()

    mappa_timbrature = {}
    for t in timbrature:
        m_id = str(t['milite_id'])
        giorno = t['giorno']
        if m_id not in mappa_timbrature:
            mappa_timbrature[m_id] = {}
        if giorno not in mappa_timbrature[m_id]:
            mappa_timbrature[m_id][giorno] = []

        mappa_timbrature[m_id][giorno].append(t)

    return jsonify({'militi': militi, 'mappaTimbrature': mappa_timbrature})

@app.route('/api/timbratura/salva-manuale', methods=['POST'])
def salva_timbratura_manuale():
    data = request.json or {}
    timbratura_id = data.get('timbratura_id') # ID per aggiornare un turno esistente
    milite_id = data.get('milite_id')
    giorno_inizio = data.get('giorno_inizio')
    ora_inizio = data.get('ora_inizio')
    giorno_fine = data.get('giorno_fine')
    ora_fine = data.get('ora_fine')
    is_assistenza = data.get('is_assistenza', False)

    if not milite_id or not giorno_inizio or not ora_inizio or not giorno_fine or not ora_fine:
        return jsonify({'errore': 'Parametri incompleti'}), 400

    try:
        t_in = datetime.strptime(f"{giorno_inizio} {ora_inizio}", "%Y-%m-%d %H:%M")
        t_fin = datetime.strptime(f"{giorno_fine} {ora_fine}", "%Y-%m-%d %H:%M")

        diff_secondi = (t_fin - t_in).total_seconds()
        
        if diff_secondi <= 0:
            return jsonify({'errore': "L'orario di fine deve essere successivo all'orario di inizio"}), 400
        if diff_secondi < 300: # Meno di 5 minuti
            return jsonify({'errore': 'Il turno deve durare almeno 5 minuti'}), 400
        if diff_secondi > 86400: # Più di 24 ore
            return jsonify({'errore': 'Un turno non può superare le 24 ore consecutive'}), 400

        dt_inizio_iso = t_in.isoformat()
        dt_fine_iso = t_fin.isoformat()

        ore, tags = calcola_turno_e_tag(dt_inizio_iso, dt_fine_iso, is_assistenza)

        conn = get_db_connection()
        cursor = conn.cursor()
        
        if timbratura_id:
            cursor.execute('''
                UPDATE timbrature 
                SET inizio_turno = ?, fine_turno = ?, ore_totali = ?, tag_turni = ?
                WHERE id = ? AND milite_id = ?
            ''', (dt_inizio_iso, dt_fine_iso, ore, tags, timbratura_id, milite_id))
        else:
            cursor.execute('''
                INSERT INTO timbrature (milite_id, inizio_turno, fine_turno, ore_totali, tag_turni)
                VALUES (?, ?, ?, ?, ?)
            ''', (milite_id, dt_inizio_iso, dt_fine_iso, ore, tags))

        conn.commit()
        conn.close()

        return jsonify({'ok': True, 'ore': ore, 'tags': tags})
    except Exception as e:
        return jsonify({'errore': f'Errore nel formato dati: {str(e)}'}), 400

@app.route('/api/timbratura/elimina', methods=['POST'])
def elimina_timbratura():
    data = request.json or {}
    timbratura_id = data.get('timbratura_id')

    if not timbratura_id:
        return jsonify({'errore': 'Parametri incompleti'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM timbrature WHERE id = ?', (timbratura_id,))
    
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)