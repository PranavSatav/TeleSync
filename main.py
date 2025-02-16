from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify, after_this_request
import os
import asyncio
import sqlite3
import datetime
import tempfile
import uuid
import time
import threading
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Replace with your valid Telegram API credentials
API_ID = 2435863            # <-- Your API ID (an integer)
API_HASH = 'feadb3730b80949cd6037df1082e58ba'  # <-- Your API Hash as a string

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Change this in production!
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limit file uploads to 50MB

# Global lock to serialize database writes
db_lock = threading.Lock()

# ----------------------------------------------------------------
# Jinja2 filter to format file sizes
# ----------------------------------------------------------------
def filesizeformat(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    if value < 1024:
        return f"{value} bytes"
    elif value < 1024 * 1024:
        return f"{value/1024:.2f} KB"
    else:
        return f"{value/(1024*1024):.2f} MB"

app.jinja_env.filters['filesizeformat'] = filesizeformat

# ----------------------------------------------------------------
# Database functions (using WAL mode and extended timeout)
# ----------------------------------------------------------------
def get_db():
    conn = sqlite3.connect("files.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = get_db()
    with conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            message_id INTEGER,
            filename TEXT,
            file_size INTEGER,
            file_type TEXT,
            upload_date TEXT
        )
        """)
    conn.close()

init_db()  # Initialize the DB on app startup

# ----------------------------------------------------------------
# Asynchronous helper functions for Telegram
# ----------------------------------------------------------------
async def send_code_request(phone):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    finally:
        await client.disconnect()
    return sent.phone_code_hash

async def telegram_sign_in(phone, code, phone_code_hash, password=None):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        if password:
            try:
                await client.sign_in(password=password)
            except Exception as e:
                return False, f"Incorrect password: {str(e)}"
        else:
            return False, "Two-step verification is enabled. Please provide your password."
    except Exception as e:
        return False, str(e)
    finally:
        await client.disconnect()
    return True, "Logged in successfully"

async def send_file_to_saved_messages(phone, file_path):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return False, "User not authorized. Please log in again.", None
        messages = await client.send_file('me', file=file_path)
        message = messages[0] if isinstance(messages, list) else messages
    except Exception as e:
        return False, str(e), None
    finally:
        await client.disconnect()
    return True, "File uploaded successfully to Saved Messages", message

async def download_file_from_saved_messages(phone, message_id):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        message = await client.get_messages('me', ids=message_id)
        if message is None:
            return None, "Message not found"
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_path = temp_file.name
        temp_file.close()
        await client.download_media(message, file=temp_path)
    except Exception as e:
        return None, str(e)
    finally:
        await client.disconnect()
    return temp_path, None

async def delete_message_from_saved_messages(phone, message_id):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        await client.delete_messages('me', message_id)
    except Exception as e:
        return False, str(e)
    finally:
        await client.disconnect()
    return True, "Deleted successfully"

async def telegram_log_out(phone):
    client = TelegramClient(f'session_{phone}', API_ID, API_HASH)
    await client.connect()
    try:
        await client.log_out()
    except Exception:
        pass
    finally:
        await client.disconnect()

# ----------------------------------------------------------------
# Flask Routes
# ----------------------------------------------------------------
@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return redirect(url_for('upload'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form.get('phone')
        if not phone:
            flash("Please enter a phone number.")
            return redirect(url_for('login'))
        session['phone'] = phone
        try:
            phone_code_hash = asyncio.run(send_code_request(phone))
            session['phone_code_hash'] = phone_code_hash
            flash("OTP has been sent to your Telegram account.")
            return redirect(url_for('otp'))
        except Exception as e:
            flash(f"Error sending OTP: {str(e)}")
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/otp', methods=['GET', 'POST'])
def otp():
    if 'phone' not in session or 'phone_code_hash' not in session:
        flash("Please enter your phone number first.")
        return redirect(url_for('login'))
    if request.method == 'POST':
        otp_code = request.form.get('otp')
        password = request.form.get('password')
        phone = session.get('phone')
        phone_code_hash = session.get('phone_code_hash')
        if not otp_code:
            flash("Please enter the OTP code.")
            return redirect(url_for('otp'))
        success, message_text = asyncio.run(telegram_sign_in(phone, otp_code, phone_code_hash, password=password))
        if success:
            session['logged_in'] = True
            flash("Logged in successfully.")
            return redirect(url_for('upload'))
        else:
            flash(f"Failed to log in: {message_text}")
            return redirect(url_for('otp'))
    return render_template('otp.html')

@app.route('/upload', methods=['GET'])
def upload():
    if not session.get('logged_in'):
        flash("You need to log in first.")
        return redirect(url_for('login'))
    # Render the main upload page; file table will be loaded via AJAX.
    phone = session.get('phone')
    conn = get_db()
    cursor = conn.execute("SELECT SUM(file_size) FROM uploaded_files WHERE phone=?", (phone,))
    total_size = cursor.fetchone()[0] or 0
    conn.close()
    return render_template('upload.html', total_size=total_size)

@app.route('/files_table')
def files_table():
    if not session.get('logged_in'):
        return "", 401
    phone = session.get('phone')
    conn = get_db()
    cursor = conn.execute("SELECT * FROM uploaded_files WHERE phone=?", (phone,))
    files = cursor.fetchall()
    cursor = conn.execute("SELECT SUM(file_size) FROM uploaded_files WHERE phone=?", (phone,))
    total_size = cursor.fetchone()[0] or 0
    conn.close()
    return render_template('files_table.html', files=files, total_size=total_size)

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Not logged in."}), 401
    phone = session.get('phone')
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file part in the request."}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "message": "No file selected."}), 400

    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    if file_length > 50 * 1024 * 1024:
        return jsonify({"success": False, "message": "File exceeds 50MB limit."}), 400

    upload_folder = os.path.join(os.getcwd(), 'uploads')
    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)
    unique_filename = f"{uuid.uuid4().hex}_{file.filename}"
    file_path = os.path.join(upload_folder, unique_filename)
    file.save(file_path)
    file_size = os.path.getsize(file_path)
    file_type = file.content_type

    success, msg_text, msg_obj = asyncio.run(send_file_to_saved_messages(phone, file_path))
    os.remove(file_path)
    if success:
        message_id = msg_obj.id
        upload_date = msg_obj.date.isoformat() if msg_obj.date else datetime.datetime.now().isoformat()
        retry_attempts = 5
        for attempt in range(retry_attempts):
            try:
                with db_lock:
                    conn = get_db()
                    with conn:
                        cursor = conn.execute("""
                            INSERT INTO uploaded_files (phone, message_id, filename, file_size, file_type, upload_date)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (phone, message_id, file.filename, file_size, file_type, upload_date))
                        file_id = cursor.lastrowid
                    conn.close()
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    time.sleep(0.1)
                else:
                    raise e
        else:
            return jsonify({"success": False, "message": "Database is locked. Please try again."}), 500

        return jsonify({
            "success": True,
            "message": "File uploaded successfully.",
            "file": {
                "id": file_id,
                "filename": file.filename,
                "file_size": file_size,
                "file_type": file_type,
                "upload_date": upload_date
            }
        })
    else:
        return jsonify({"success": False, "message": f"Failed to upload file: {msg_text}"}), 500

@app.route('/download/<int:file_id>')
def download(file_id):
    if not session.get('logged_in'):
        flash("You need to log in first.")
        return redirect(url_for('login'))
    phone = session.get('phone')
    conn = get_db()
    cursor = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND phone=?", (file_id, phone))
    file_record = cursor.fetchone()
    conn.close()
    if not file_record:
        flash("File not found.")
        return redirect(url_for('upload'))
    message_id = file_record['message_id']
    filename = file_record['filename']
    temp_path, err = asyncio.run(download_file_from_saved_messages(phone, message_id))
    if err:
        flash(f"Error downloading file: {err}")
        return redirect(url_for('upload'))
    @after_this_request
    def remove_file(response):
        try:
            os.remove(temp_path)
        except Exception:
            pass
        return response
    return send_file(temp_path, as_attachment=True, download_name=filename)

@app.route('/delete/<int:file_id>')
def delete(file_id):
    if not session.get('logged_in'):
        flash("You need to log in first.")
        return redirect(url_for('login'))
    phone = session.get('phone')
    conn = get_db()
    cursor = conn.execute("SELECT * FROM uploaded_files WHERE id=? AND phone=?", (file_id, phone))
    file_record = cursor.fetchone()
    if not file_record:
        flash("File not found.")
        conn.close()
        return redirect(url_for('upload'))
    message_id = file_record['message_id']
    success, msg_text = asyncio.run(delete_message_from_saved_messages(phone, message_id))
    if success:
        retry_attempts = 5
        for attempt in range(retry_attempts):
            try:
                with db_lock:
                    with conn:
                        conn.execute("DELETE FROM uploaded_files WHERE id=?", (file_id,))
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    time.sleep(0.1)
                else:
                    raise e
        flash("File deleted successfully.")
    else:
        flash(f"Error deleting file: {msg_text}")
    conn.close()
    return redirect(url_for('upload'))

@app.route('/logout')
def logout():
    if 'phone' in session:
        phone = session.get('phone')
        try:
            asyncio.run(telegram_log_out(phone))
        except Exception as e:
            flash(f"Error logging out: {str(e)}")
    session.clear()
    flash("Logged out successfully.")
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
