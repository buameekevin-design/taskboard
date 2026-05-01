from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import sqlite3, os, json, smtplib, threading
from email.mime.text import MIMEText
from datetime import datetime, date
import anthropic
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
DB = os.path.join(os.path.dirname(__file__), 'taskboard.db')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_PASS = os.environ.get('GMAIL_PASS', '')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL', '')
PASSWORD_HASH = generate_password_hash('0410598711Kk@')

def send_email(subject, body):
    print(f'>>> send_email called: {subject}', flush=True)
    if not GMAIL_USER or not GMAIL_PASS or not NOTIFY_EMAIL:
        print(f'>>> Email skipped: GMAIL_USER={bool(GMAIL_USER)}, GMAIL_PASS={bool(GMAIL_PASS)}, NOTIFY_EMAIL={bool(NOTIFY_EMAIL)}', flush=True)
        return False
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL
        print(f'>>> Connecting to Gmail SMTP...', flush=True)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as s:
            s.login(GMAIL_USER, GMAIL_PASS.replace(' ', ''))
            s.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        print(f'>>> Email sent successfully: {subject}', flush=True)
        return True
    except Exception as e:
        print(f'Email error: {e}', flush=True)
        return False

STATUSES = ['Backlog', 'In Progress', 'Review', 'Done']
PRIORITIES = ['Low', 'Medium', 'High']

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#666666',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'Backlog',
                priority TEXT DEFAULT 'Medium',
                due_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER REFERENCES tickets(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER REFERENCES tickets(id),
                remind_at TEXT NOT NULL,
                message TEXT DEFAULT '',
                dismissed INTEGER DEFAULT 0,
                email_sent INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
        ''')
        # Seed default projects if empty
        cur = conn.execute('SELECT COUNT(*) FROM projects')
        if cur.fetchone()[0] == 0:
            conn.executemany('INSERT INTO projects (name, color) VALUES (?, ?)', [
                ('Work', '#4A9EFF'),
                ('Personal', '#A855F7'),
                ('Guitar', '#F97316'),
            ])

init_db()

# Migrate existing DB to add email_sent column if missing
with get_db() as _conn:
    try:
        _conn.execute('ALTER TABLE reminders ADD COLUMN email_sent INTEGER DEFAULT 0')
    except Exception:
        pass

def reminder_scheduler():
    import time
    while True:
        try:
            with get_db() as conn:
                now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                due = conn.execute('''
                    SELECT r.*, t.title as ticket_title
                    FROM reminders r JOIN tickets t ON r.ticket_id = t.id
                    WHERE r.dismissed = 0 AND r.email_sent = 0
                      AND r.remind_at <= ?
                ''', (now,)).fetchall()
                for r in due:
                    send_email(
                        f"Reminder: {r['ticket_title']}",
                        f"Reminder for ticket: {r['ticket_title']}\n\n{r['message'] or 'No message'}\n\nScheduled for: {r['remind_at']}"
                    )
                    conn.execute('UPDATE reminders SET email_sent = 1 WHERE id = ?', (r['id'],))
        except Exception as e:
            print(f'Scheduler error: {e}')
        time.sleep(60)

threading.Thread(target=reminder_scheduler, daemon=True).start()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ── Auth Pages ──────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if check_password_hash(PASSWORD_HASH, password):
            session['user'] = 'kevin'
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid password')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

# ── Pages ──────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY name').fetchall()
        tickets = conn.execute('''
            SELECT t.*, p.name as project_name, p.color as project_color
            FROM tickets t LEFT JOIN projects p ON t.project_id = p.id
            ORDER BY t.updated_at DESC
        ''').fetchall()
        due_soon = conn.execute('''
            SELECT t.*, p.name as project_name, p.color as project_color
            FROM tickets t LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.due_date IS NOT NULL AND t.due_date != ''
              AND t.status != 'Done'
              AND date(t.due_date) <= date('now', '+3 days')
            ORDER BY t.due_date ASC
        ''').fetchall()
        reminders = conn.execute('''
            SELECT r.*, t.title as ticket_title
            FROM reminders r JOIN tickets t ON r.ticket_id = t.id
            WHERE r.dismissed = 0 AND datetime(r.remind_at) <= datetime('now')
        ''').fetchall()
    return render_template('index.html',
        projects=projects, tickets=tickets,
        statuses=STATUSES, priorities=PRIORITIES,
        due_soon=due_soon, reminders=reminders,
        today=date.today().isoformat())

@app.route('/board')
@login_required
def board():
    project_id = request.args.get('project_id', '')
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY name').fetchall()
        q = '''SELECT t.*, p.name as project_name, p.color as project_color
               FROM tickets t LEFT JOIN projects p ON t.project_id = p.id'''
        if project_id:
            tickets = conn.execute(q + ' WHERE t.project_id = ? ORDER BY t.priority DESC, t.due_date ASC', (project_id,)).fetchall()
        else:
            tickets = conn.execute(q + ' ORDER BY t.priority DESC, t.due_date ASC').fetchall()
    by_status = {s: [t for t in tickets if t['status'] == s] for s in STATUSES}
    return render_template('board.html',
        projects=projects, by_status=by_status,
        statuses=STATUSES, selected_project=project_id)

@app.route('/ticket/<int:tid>')
@login_required
def ticket(tid):
    with get_db() as conn:
        t = conn.execute('''SELECT t.*, p.name as project_name, p.color as project_color
            FROM tickets t LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.id = ?''', (tid,)).fetchone()
        msgs = conn.execute('SELECT * FROM messages WHERE ticket_id = ? ORDER BY created_at', (tid,)).fetchall()
        reminders = conn.execute('SELECT * FROM reminders WHERE ticket_id = ? ORDER BY remind_at', (tid,)).fetchall()
        projects = conn.execute('SELECT * FROM projects ORDER BY name').fetchall()
    if not t:
        return redirect('/')
    return render_template('ticket.html', ticket=t, messages=msgs,
        reminders=reminders, projects=projects,
        statuses=STATUSES, priorities=PRIORITIES)

# ── API ────────────────────────────────────────────────────────────────────

@app.route('/api/projects', methods=['POST'])
@login_required
def create_project():
    d = request.json
    with get_db() as conn:
        cur = conn.execute('INSERT INTO projects (name, color) VALUES (?, ?)',
            (d['name'], d.get('color', '#666666')))
    return jsonify({'id': cur.lastrowid})

@app.route('/api/projects/<int:pid>', methods=['DELETE'])
@login_required
def delete_project(pid):
    with get_db() as conn:
        conn.execute('DELETE FROM projects WHERE id = ?', (pid,))
    return jsonify({'ok': True})

@app.route('/api/tickets', methods=['POST'])
@login_required
def create_ticket():
    d = request.json
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO tickets (project_id, title, description, status, priority, due_date)
            VALUES (?, ?, ?, ?, ?, ?)''',
            (d.get('project_id'), d['title'], d.get('description',''),
             d.get('status','Backlog'), d.get('priority','Medium'), d.get('due_date')))
    return jsonify({'id': cur.lastrowid})

@app.route('/api/tickets/<int:tid>', methods=['PATCH'])
@login_required
def update_ticket(tid):
    d = request.json
    fields = {k: v for k, v in d.items() if k in ['title','description','status','priority','due_date','project_id']}
    fields['updated_at'] = datetime.now().isoformat()
    sets = ', '.join(f'{k}=?' for k in fields)
    with get_db() as conn:
        conn.execute(f'UPDATE tickets SET {sets} WHERE id = ?', (*fields.values(), tid))
    return jsonify({'ok': True})

@app.route('/api/tickets/<int:tid>', methods=['DELETE'])
@login_required
def delete_ticket(tid):
    with get_db() as conn:
        conn.execute('DELETE FROM messages WHERE ticket_id = ?', (tid,))
        conn.execute('DELETE FROM reminders WHERE ticket_id = ?', (tid,))
        conn.execute('DELETE FROM tickets WHERE id = ?', (tid,))
    return jsonify({'ok': True})

@app.route('/api/tickets/<int:tid>/chat', methods=['POST'])
@login_required
def chat(tid):
    d = request.json
    user_msg = d.get('message', '').strip()
    if not user_msg:
        return jsonify({'error': 'empty'}), 400

    with get_db() as conn:
        t = conn.execute('SELECT * FROM tickets WHERE id = ?', (tid,)).fetchone()
        history = conn.execute('SELECT role, content FROM messages WHERE ticket_id = ? ORDER BY created_at', (tid,)).fetchall()
        conn.execute('INSERT INTO messages (ticket_id, role, content) VALUES (?, ?, ?)', (tid, 'user', user_msg))

    system = f"""You are a personal productivity assistant helping Kevin manage his tasks.
Current ticket:
- Title: {t['title']}
- Status: {t['status']}
- Priority: {t['priority']}
- Due: {t['due_date'] or 'No due date'}
- Description: {t['description'] or 'No description'}

Help Kevin think through this task, break it down, suggest approaches, or answer questions about it. Be concise and practical."""

    messages = [{'role': r['role'], 'content': r['content']} for r in history]
    messages.append({'role': 'user', 'content': user_msg})

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1024,
        system=system,
        messages=messages
    )
    reply = resp.content[0].text

    with get_db() as conn:
        conn.execute('INSERT INTO messages (ticket_id, role, content) VALUES (?, ?, ?)', (tid, 'assistant', reply))

    return jsonify({'reply': reply})

@app.route('/api/suggest')
@login_required
def suggest():
    with get_db() as conn:
        tickets = conn.execute('''
            SELECT t.*, p.name as project_name FROM tickets t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status != 'Done'
            ORDER BY t.priority DESC, t.due_date ASC
        ''').fetchall()

    if not tickets:
        return jsonify({'reply': "You have no open tickets. Great job! Add some tasks to get started."})

    ticket_list = '\n'.join([
        f"- [{t['priority']}] {t['title']} | {t['status']} | Project: {t['project_name'] or 'None'} | Due: {t['due_date'] or 'No date'}"
        for t in tickets
    ])

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=512,
        messages=[{'role': 'user', 'content': f"""Here are Kevin's open tasks:

{ticket_list}

What should Kevin work on next? Give a short, practical recommendation (2-3 sentences max). Consider priority, due dates, and what's in progress."""}]
    )
    return jsonify({'reply': resp.content[0].text})

@app.route('/api/weekly-summary')
@login_required
def weekly_summary():
    with get_db() as conn:
        done = conn.execute('''SELECT t.*, p.name as project_name FROM tickets t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'Done' AND date(t.updated_at) >= date('now', '-7 days')''').fetchall()
        open_tickets = conn.execute('''SELECT t.*, p.name as project_name FROM tickets t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status != 'Done' ORDER BY t.priority DESC''').fetchall()

    done_list = '\n'.join([f"- {t['title']} ({t['project_name'] or 'No project'})" for t in done]) or 'None'
    open_list = '\n'.join([f"- [{t['priority']}] {t['title']} ({t['status']})" for t in open_tickets]) or 'None'

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=600,
        messages=[{'role': 'user', 'content': f"""Write a brief weekly summary for Kevin.

Completed this week:
{done_list}

Still open:
{open_list}

Write a short, encouraging summary (3-4 sentences): what was accomplished, what's still on the plate, and one motivating thought for the week ahead."""}]
    )
    return jsonify({'reply': resp.content[0].text})

@app.route('/api/test-email')
@login_required
def test_email():
    ok = send_email('Taskboard test email', 'If you see this, email is working!')
    return jsonify({
        'sent': ok,
        'gmail_user_set': bool(GMAIL_USER),
        'gmail_pass_set': bool(GMAIL_PASS),
        'notify_email_set': bool(NOTIFY_EMAIL),
        'notify_email': NOTIFY_EMAIL
    })

@app.route('/api/reminders', methods=['POST'])
@login_required
def create_reminder():
    d = request.json
    remind_at = d['remind_at'].replace('T', ' ')
    if len(remind_at) == 16:
        remind_at += ':00'
    with get_db() as conn:
        cur = conn.execute('INSERT INTO reminders (ticket_id, remind_at, message) VALUES (?, ?, ?)',
            (d['ticket_id'], remind_at, d.get('message', '')))
    return jsonify({'id': cur.lastrowid})

@app.route('/api/reminders/<int:rid>/dismiss', methods=['POST'])
@login_required
def dismiss_reminder(rid):
    with get_db() as conn:
        conn.execute('UPDATE reminders SET dismissed = 1 WHERE id = ?', (rid,))
    return jsonify({'ok': True})

@app.route('/marketing')
@login_required
def marketing():
    return render_template('marketing.html')

@app.route('/api/marketing/generate', methods=['POST'])
@login_required
def marketing_generate():
    d = request.json
    content_type = d.get('content_type', 'Instagram post')
    about = d.get('about', '').strip()
    business = d.get('business', 'My guitar tech side hustle')
    platform = d.get('platform', 'Instagram')
    tone = d.get('tone', 'Casual & real')
    extra = d.get('extra', '').strip()

    if not about:
        return jsonify({'error': 'Please describe what the content is about.'}), 400

    tone_notes = {
        'Casual & real': 'relaxed, authentic, conversational — like a real person not a brand',
        'Professional': 'polished and credible, still approachable',
        'Hype / energetic': 'excited, punchy, high energy — use caps and exclamation where it fits',
        'Educational': 'informative and helpful, teaches the reader something',
        'Very NZ / local': 'New Zealand casual slang, Kiwi feel, very local and down-to-earth',
    }

    prompt = f"""You are writing short-form marketing content for a guitar business.

Business: {business}
Content type requested: {content_type}
Platform: {platform}
Tone: {tone} — {tone_notes.get(tone, tone)}
Topic / context: {about}
{f'Extra instructions: {extra}' if extra else ''}

Write the {content_type} now. Keep it tight and ready to post. Include hashtags only if appropriate for the platform and content type. Do not add any explanation or preamble — just the content itself."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=600,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return jsonify({'content': resp.content[0].text})

@app.route('/api/reminders/pending')
@login_required
def pending_reminders():
    with get_db() as conn:
        rows = conn.execute('''SELECT r.*, t.title as ticket_title FROM reminders r
            JOIN tickets t ON r.ticket_id = t.id
            WHERE r.dismissed = 0 AND datetime(r.remind_at) <= datetime('now')''').fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
