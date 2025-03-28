#!/usr/bin/env python3
import os
import secrets
import random
import threading
import webbrowser
import time
import io
import socket
import hashlib
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, render_template_string, send_file, url_for, session, redirect, flash, make_response
from fpdf import FPDF

# Configurez le logging pour la production
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Création de l'application Flask et configuration de la clé secrète
# -----------------------------------------------------------------------------
app = Flask(__name__)
# Pour la production, la SECRET_KEY doit être définie dans l'environnement
app.secret_key = os.environ.get("07ffda66dd44daf06c10bc672b47f0b0eaff1f2fade1034e3bfdb57c4dcb7cc8", secrets.token_hex(32))

# -----------------------------------------------------------------------------
# Chargement des informations sensibles depuis les variables d'environnement
# -----------------------------------------------------------------------------
# GOOGLE_CREDENTIALS_INFO et SERVICE_ACCOUNT_INFO doivent être fournis sous forme de JSON
try:
    GOOGLE_CREDENTIALS_INFO = json.loads(os.environ.get("GOOGLE_CREDENTIALS_INFO", "{}"))
    SERVICE_ACCOUNT_INFO = json.loads(os.environ.get("SERVICE_ACCOUNT_INFO", "{}"))
except Exception as e:
    logger.error("Erreur lors du chargement des credentials Google: %s", e)
    GOOGLE_CREDENTIALS_INFO = {}
    SERVICE_ACCOUNT_INFO = {}

# Vérifier que les credentials minimum sont présents (sinon, on peut lever une exception ou loguer une alerte)
if not GOOGLE_CREDENTIALS_INFO or not SERVICE_ACCOUNT_INFO:
    logger.warning("Les informations de credentials Google ne sont pas correctement configurées.")

# -----------------------------------------------------------------------------
# Configuration et constantes
# -----------------------------------------------------------------------------
SCOPES = ['https://www.googleapis.com/auth/drive']

def get_drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    service = build('drive', 'v3', credentials=creds)
    return service

def create_folder(service, folder_name, parent_id=None):
    folder_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    if parent_id:
        folder_metadata['parents'] = [parent_id]
    folder = service.files().create(body=folder_metadata, fields='id').execute()
    logger.info("Folder '%s' created with ID: %s", folder_name, folder.get('id'))
    return folder.get('id')

def get_medicsas_folder_id(service):
    query = "mimeType='application/vnd.google-apps.folder' and name='MEDICSAS_FILES' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    if items:
        logger.info("Folder MEDICSAS_FILES already exists.")
        return items[0]['id']
    else:
        logger.info("Folder MEDICSAS_FILES does not exist. It will be created.")
        return create_folder(service, "MEDICSAS_FILES")

def get_config_folder_id(service):
    parent_id = get_medicsas_folder_id(service)
    query = ("mimeType='application/vnd.google-apps.folder' and name='Config' and "
             f"'{parent_id}' in parents and trashed=false")
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    if items:
        return items[0]['id']
    else:
        return create_folder(service, "Config", parent_id)

def get_user_drive_folder_id(service, user_email, parent_folder_id):
    folder_name = f"user_{user_email}"
    query = (
        f"mimeType='application/vnd.google-apps.folder' and "
        f"name='{folder_name}' and '{parent_folder_id}' in parents and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    if items:
        logger.info("Folder for %s already exists.", user_email)
        return items[0]['id']
    else:
        logger.info("Folder for %s does not exist. It will be created.", user_email)
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        logger.info("Folder '%s' created with ID: %s", folder_name, folder.get('id'))
        return folder.get('id')

def upload_bytes_to_drive(file_bytes, filename, mime_type='application/octet-stream', folder_id=None):
    service = get_drive_service()
    file_metadata = {'name': filename}
    if folder_id:
        file_metadata['parents'] = [folder_id]
    stream = io.BytesIO(file_bytes)
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(stream, mimetype=mime_type)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    logger.info("File %s uploaded with ID %s", filename, file.get('id'))
    return file.get('id')

def update_file_in_drive(service, file_id, file_bytes, mime_type='application/octet-stream'):
    stream = io.BytesIO(file_bytes)
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(stream, mimetype=mime_type)
    file = service.files().update(fileId=file_id, media_body=media).execute()
    return file.get('id')

def download_file_from_drive(file_id):
    service = get_drive_service()
    request_drive = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload
    downloader = MediaIoBaseDownload(fh, request_drive)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh

def get_drive_file_id(service, filename, folder_id):
    query = f"name='{filename}' and '{folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])
    if items:
        return items[0]['id']
    else:
        return None

def get_user_folder_id(user_email="default_user"):
    service = get_drive_service()
    medicsas_folder_id = get_medicsas_folder_id(service)
    return get_user_drive_folder_id(service, user_email, medicsas_folder_id)

# -----------------------------------------------------------------------------
# Gestion de la persistance des utilisateurs sur Google Drive (pas de stockage local)
# -----------------------------------------------------------------------------
def load_users():
    try:
        service = get_drive_service()
        config_folder_id = get_config_folder_id(service)
        file_id = get_drive_file_id(service, "users.json", config_folder_id)
        if file_id:
            fh = download_file_from_drive(file_id)
            content = fh.read().decode("utf-8")
            data = json.loads(content)
            for email, info in data.items():
                if "plan_start" in info and info["plan_start"]:
                    info["plan_start"] = datetime.fromisoformat(info["plan_start"])
            return data
        else:
            return {}
    except Exception as e:
        logger.error("Error loading users: %s", e)
        return {}

def save_users():
    try:
        data_to_save = {}
        for email, info in users.items():
            data = info.copy()
            if "plan_start" in data and isinstance(data["plan_start"], datetime):
                data["plan_start"] = data["plan_start"].isoformat()
            data_to_save[email] = data
        content = json.dumps(data_to_save, indent=4)
        service = get_drive_service()
        config_folder_id = get_config_folder_id(service)
        file_id = get_drive_file_id(service, "users.json", config_folder_id)
        content_bytes = content.encode("utf-8")
        if file_id:
            update_file_in_drive(service, file_id, content_bytes, "application/json")
        else:
            upload_bytes_to_drive(content_bytes, "users.json", mime_type="application/json", folder_id=config_folder_id)
    except Exception as e:
        logger.error("Error saving users: %s", e)

users = load_users()

# -----------------------------------------------------------------------------
# Variables globales pour les exercices
# -----------------------------------------------------------------------------
latest_exercises = None
latest_meta = None
latest_result = None

# -----------------------------------------------------------------------------
# Définition du token utilisé pour le calcul de la clé d'activation
# -----------------------------------------------------------------------------
ACTIVATION_TOKEN = os.environ.get("ACTIVATION_TOKEN", "1r2h3y4f7e5dsf6")

def generate_activation_key(email, plan, secret, date_str):
    data = f"{email.lower()}_{plan}_{secret}_{date_str}"
    hash_hex = hashlib.sha256(data.encode()).hexdigest()
    num = int(hash_hex, 16)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    key_base36 = ""
    while num > 0:
        num, rem = divmod(num, 36)
        key_base36 = alphabet[rem] + key_base36
    key_base36 = key_base36.zfill(16)
    activation_key = key_base36[:16]
    formatted_key = "-".join([activation_key[i:i+4] for i in range(0, len(activation_key), 4)])
    return formatted_key

# -----------------------------------------------------------------------------
# Fonctions utilitaires
# -----------------------------------------------------------------------------
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        s.close()
    return local_ip

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def generate_exercise(operation, level):
    if operation in ['addition', 'subtraction']:
        if level == 'easy':
            low, high = 0, 10
        elif level == 'intermediate':
            low, high = 0, 50
        elif level == 'hard':
            low, high = 0, 100
        elif level == 'very hard':
            low, high = 0, 200
        elif level == 'expert':
            low, high = 0, 1000000
        a = random.randint(low, high)
        if operation == 'addition':
            b = random.randint(low, high)
            result = a + b
            return {'a': a, 'b': b, 'op': '+', 'result': result, 'res_len': len(str(result))}
        else:
            b = random.randint(low, a)
            result = a - b
            return {'a': a, 'b': b, 'op': '-', 'result': result, 'res_len': len(str(result))}
    elif operation == 'multiplication':
        if level == 'easy':
            low, high = 0, 5
        elif level == 'intermediate':
            low, high = 0, 10
        elif level == 'hard':
            low, high = 0, 20
        elif level == 'very hard':
            low, high = 0, 30
        elif level == 'expert':
            low, high = 0, 1000
        a = random.randint(low, high)
        b = random.randint(low, high)
        result = a * b
        return {'a': a, 'b': b, 'op': '×', 'result': result, 'res_len': len(str(result))}
    elif operation == 'division':
        if level == 'easy':
            b_low, b_high = 1, 5; q_low, q_high = 0, 5
        elif level == 'intermediate':
            b_low, b_high = 1, 10; q_low, q_high = 0, 10
        elif level == 'hard':
            b_low, b_high = 1, 20; q_low, q_high = 0, 20
        elif level == 'very hard':
            b_low, b_high = 1, 30; q_low, q_high = 0, 30
        elif level == 'expert':
            b_low, b_high = 1, 100; q_low, q_high = 0, 10000
        b = random.randint(b_low, b_high)
        quotient = random.randint(q_low, q_high)
        a = b * quotient
        return {'a': a, 'b': b, 'op': '÷', 'result': quotient, 'res_len': len(str(quotient))}

def draw_exercise_box(pdf, ex_num, ex, x, y, col_width, line_height, solution_text=None):
    num_width = 12
    content_width = col_width - num_width
    pdf.set_font("Courier", "", 8)
    pdf.set_xy(x, y)
    pdf.cell(num_width, line_height, f"{ex_num}.", border=0, align="R")
    pdf.set_font("Courier", "", 12)
    pdf.set_xy(x + num_width, y)
    pdf.cell(content_width, line_height, f"{ex['a']}", border=0, align="R", ln=1)
    pdf.set_xy(x, y + line_height)
    op_sym = ex["op"]
    if op_sym in ["*", "×"]:
        op_sym = "×"
    elif op_sym in ["/", "÷"]:
        op_sym = "÷"
    pdf.cell(num_width, line_height, op_sym, border=0, align="R")
    pdf.set_xy(x + num_width, y + line_height)
    pdf.cell(content_width, line_height, f"{ex['b']}", border='B', align="R", ln=1)
    pdf.set_xy(x, y + 2 * line_height)
    pdf.cell(num_width, line_height, "=", border=0, align="R")
    pdf.set_xy(x + num_width, y + 2 * line_height)
    if solution_text is None:
        pdf.cell(content_width, line_height, "", border='B', align="R", ln=1)
    else:
        pdf.cell(content_width, line_height, f"{solution_text}", border=0, align="R", ln=1)

# -----------------------------------------------------------------------------
# Design et intégration d'icônes et animations (HTML/CSS)
# -----------------------------------------------------------------------------
nav_html = """
<nav class="navbar navbar-expand-lg navbar-light mac-navbar">
  <div class="container-fluid">
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav" 
            aria-controls="navbarNav" aria-expanded="false" aria-label="Toggle navigation">
      <span class="navbar-toggler-icon"></span>
    </button>
    <a class="navbar-brand" href="/"><i class="fas fa-graduation-cap"></i> MathSTK-Ex</a>
    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav ms-auto">
        {% if session.user %}
          <li class="nav-item"><a class="nav-link" href="/"><i class="fas fa-home"></i> Home</a></li>
          <li class="nav-item"><a class="nav-link" href="/activation"><i class="fas fa-unlock"></i> Activation</a></li>
          <li class="nav-item"><a class="nav-link" href="/change_password"><i class="fas fa-key"></i> Change Password</a></li>
          <li class="nav-item"><a class="nav-link" href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a></li>
        {% else %}
          <li class="nav-item"><a class="nav-link" href="/login"><i class="fas fa-sign-in-alt"></i> Login</a></li>
          <li class="nav-item"><a class="nav-link" href="/register"><i class="fas fa-user-plus"></i> Register</a></li>
          <li class="nav-item"><a class="nav-link" href="/forgot_password"><i class="fas fa-unlock-alt"></i> Forgot Password</a></li>
        {% endif %}
        <li class="nav-item">
          <div class="theme-select">
            <select id="themeSelect" onchange="changeTheme(this.value)" class="form-select">
              <option value="blue" {% if session.theme == 'blue' %}selected{% endif %}>Blue</option>
              <option value="pink" {% if session.theme == 'pink' %}selected{% endif %}>Pink</option>
              <option value="green" {% if session.theme == 'green' %}selected{% endif %}>Green</option>
              <option value="yellow" {% if session.theme == 'yellow' %}selected{% endif %}>Yellow</option>
              <option value="kid_friendly" {% if session.theme == 'kid_friendly' %}selected{% endif %}>Kid Friendly</option>
            </select>
          </div>
        </li>
      </ul>
    </div>
  </div>
</nav>
<!-- Navigation arrows container -->
<div class="nav-arrows">
  <div class="arrow-left">
    <a href="javascript:history.back()"><i class="fas fa-arrow-circle-left"></i></a>
  </div>
  <div class="arrow-right">
    <a href="javascript:history.forward()"><i class="fas fa-arrow-circle-right"></i></a>
  </div>
</div>
<style>
  .theme-select {
    margin-left: 20px;
    display: flex;
    align-items: center;
  }
  .theme-select select {
    -webkit-appearance: none;
    -moz-appearance: none;
    appearance: none;
  }
  .nav-arrows {
    display: flex;
    justify-content: space-between;
    padding: 10px 20px;
  }
  .nav-arrows .arrow-left, .nav-arrows .arrow-right {
    font-size: 2em;
    color: #333;
  }
  .nav-arrows a {
    text-decoration: none;
    color: inherit;
  }
</style>
<script>
function changeTheme(theme) {
    window.location.href = "/set_theme/" + theme;
}
</script>
"""

footer_html = """
<div class="card-footer text-center footer mac-footer">
  SASTOUKA DIGITAL © 2025 sastoukadigital@gmail.com • Whatsapp +212652084735<br>
  Access via local network: <span>{{ host_address }}</span>
</div>
<style>
  .mac-footer {
    background: rgba(255, 255, 255, 0.85);
    backdrop-filter: blur(10px);
    color: #343a40;
    margin-top:20px;
    font-size: 0.9em;
  }
</style>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
"""

common_theme_css = """
<style>
  body.blue { background-color: #D0E7FF; color: #333; }
  body.pink { background-color: #FFD1DC; color: #333; }
  body.green { background-color: #D0FFD6; color: #333; }
  body.yellow { background-color: #FFFAD1; color: #333; }
  body.kid_friendly { background: linear-gradient(135deg, #FFEEAD, #FF6F69); color: #333; }
  h1, h2, h3, .navbar-brand { font-family: 'Fredoka One', cursive; }
  p, label, input, select, button { font-family: 'Poppins', sans-serif; }
  @keyframes popIn {
    0% { transform: scale(0.8); opacity: 0; }
    100% { transform: scale(1); opacity: 1; }
  }
  .btn {
    animation: popIn 0.5s ease-out;
    transition: transform 0.2s;
  }
  .btn:hover {
    transform: scale(1.1);
  }
  @keyframes bounceIn {
    0% { transform: scale(0.5); opacity: 0; }
    60% { transform: scale(1.2); opacity: 1; }
    100% { transform: scale(1); }
  }
  h1 {
    animation: bounceIn 0.7s ease-out;
  }
  .score-motivation {
    animation: pulse 1s infinite;
  }
  @keyframes pulse {
    0% { transform: scale(1); }
    50% { transform: scale(1.05); }
    100% { transform: scale(1); }
  }
</style>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Poppins:wght@400;600&display=swap" rel="stylesheet">
"""

meta_head = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
<link rel="manifest" href="/static/manifest.json">
"""

# Page d'accueil (landing page) lorsque l'utilisateur n'est pas connecté
landing_template = """
<!doctype html>
<html lang="fr">
  <head>
    """ + meta_head + """
    <title>Bienvenue sur MathSTK-Ex</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    """ + common_theme_css + """
    <style>
      body { padding-top: 50px; }
      .landing-container {
        max-width: 500px;
        margin: auto;
        text-align: center;
      }
      .btn {
        margin: 10px;
      }
    </style>
  </head>
  <body class="{{ session.theme if session.theme else 'blue' }}">
    <div class="landing-container">
      <h1>Bienvenue sur MathSTK-Ex</h1>
      <p>Veuillez vous connecter ou créer un compte pour continuer.</p>
      <a href="/login" class="btn btn-primary"><i class="fas fa-sign-in-alt"></i> Se connecter</a>
      <a href="/register" class="btn btn-success"><i class="fas fa-user-plus"></i> S'enregistrer</a>
    </div>
  </body>
</html>
"""

# Les autres templates restent inchangés (selection_template, exercise_template, result_template, choose_plan_template, login_template, register_template, forgot_template, change_template, activation_template).
# Pour la concision, ils sont inclus tels quels ici :

selection_template = """
<!doctype html>
<html lang="en">
  <head>
    """ + meta_head + """
    <title>Math Exercises Selection</title>
    <link rel="manifest" href="/static/manifest.json">
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css">
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .content-container { margin-top: 20px; }
      .form-container { max-width: 500px; margin: auto; background: rgba(255, 255, 255, 0.95); padding: 20px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); backdrop-filter: blur(5px); }
      #installButton { display: none; margin-bottom: 20px; transition: transform 0.3s ease; }
      #installButton:hover { transform: scale(1.05); }
      .btn { transition: all 0.3s ease; border-radius: 25px; padding: 10px 20px; font-weight: bold; }
      .btn-primary { background: linear-gradient(135deg, #FF9900, #FFCC00); border: none; }
      .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
      .plan-info { background: rgba(255, 255, 255, 0.95); border-radius: 8px; padding: 15px; margin-bottom: 15px; backdrop-filter: blur(5px); }
      @media (max-width: 576px) { .form-container { padding: 15px; } h1 { font-size: 1.8em; } }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container content-container animate__animated animate__fadeIn">
      <button id="installButton" class="btn btn-info btn-block"><i class="fas fa-download"></i> Install</button>
      <h1 class="mb-4 text-center">Math Exercises</h1>
      {% if user_plan == 'free' %}
      <div class="plan-info">
        <p>Free Plan: 1 use per level.</p>
        <ul>
          <li>Easy : {{ usage_count['easy'] }}/1</li>
          <li>Intermediate : {{ usage_count['intermediate'] }}/1</li>
          <li>Hard : {{ usage_count['hard'] }}/1</li>
          <li>Very Hard : {{ usage_count['very hard'] }}/1</li>
          <li>Expert : {{ usage_count['expert'] }}/1</li>
        </ul>
      </div>
      {% elif user_plan == 'monthly' %}
      <div class="plan-info">
        <p>Monthly Plan: Unlimited access for 30 days.</p>
        <p>Start Date: {{ plan_start }} (expires on {{ plan_end }})</p>
      </div>
      {% elif user_plan == 'twenty' %}
      <div class="plan-info">
        <p>20-Tries Plan: Maximum 20 uses, all levels.</p>
        <p>Uses: {{ usage_count['total'] }}/20</p>
      </div>
      {% endif %}
      <form method="POST">
        <input type="hidden" name="phase" value="generate">
        <div class="form-group">
          <label for="level">Select Level:</label>
          <select class="form-control" id="level" name="level" required>
            <option value="easy" {% if not can_use['easy'] %}disabled{% endif %}>Easy</option>
            <option value="intermediate" {% if not can_use['intermediate'] %}disabled{% endif %}>Intermediate</option>
            <option value="hard" {% if not can_use['hard'] %}disabled{% endif %}>Hard</option>
            <option value="very hard" {% if not can_use['very hard'] %}disabled{% endif %}>Very Hard</option>
            <option value="expert" {% if not can_use['expert'] %}disabled{% endif %}>Expert</option>
          </select>
        </div>
        <div class="form-group">
          <label for="category">Select Category:</label>
          <select class="form-control" id="category" name="category" required>
            <option value="addition">Addition</option>
            <option value="subtraction">Subtraction</option>
            <option value="multiplication">Multiplication</option>
            <option value="division">Division</option>
            <option value="all">All Operations</option>
          </select>
        </div>
        <div class="form-group">
          <label for="nb_ops">Number of Operations:</label>
          <select class="form-control" id="nb_ops" name="nb_ops" required>
            <option value="10">10</option>
            <option value="20">20</option>
            <option value="50">50</option>
            <option value="100" selected>100</option>
            <option value="200">200</option>
            <option value="400">400</option>
            <option value="600">600</option>
          </select>
        </div>
        <div class="form-group">
          <label for="pdf_columns">Number of PDF Columns:</label>
          <select class="form-control" id="pdf_columns" name="pdf_columns" required>
            <option value="3" selected>3</option>
            <option value="4">4</option>
            <option value="5">5</option>
            <option value="6">6</option>
          </select>
        </div>
        <button id="generateBtn" type="submit" class="btn btn-primary btn-block"><i class="fas fa-play"></i> Generate Exercises</button>
      </form>
      """ + footer_html + """
    </div>
    <script>
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/static/sw.js')
          .then(function(registration) {
            console.log('Service Worker registered:', registration.scope);
          })
          .catch(function(error) {
            console.log('Service Worker error:', error);
          });
      }
      let deferredPrompt;
      const installButton = document.getElementById('installButton');
      window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        installButton.style.display = 'block';
      });
      installButton.addEventListener('click', async () => {
        if (deferredPrompt) {
          deferredPrompt.prompt();
          const { outcome } = await deferredPrompt.userChoice;
          console.log('User response:', outcome);
          deferredPrompt = null;
          installButton.style.display = 'none';
        } else {
          alert("Installation not available.");
        }
      });
    </script>
  </body>
</html>
"""

exercise_template = """
<!doctype html>
<html lang="en">
  <head>
    """ + meta_head + """
    <title>Exercises - Level {{ level|capitalize }} - {% if selected_category=='all' %}All{% else %}{{ selected_category|capitalize }}{% endif %}</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css">
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      h1 { text-align: center; margin-bottom: 30px; animation: fadeInDown 0.8s ease; }
      .exercise { display: inline-block; margin: 10px; padding: 10px; background: rgba(255, 255, 255, 0.95); border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.1); width: 150px; transition: transform 0.3s ease, box-shadow 0.3s ease; }
      .exercise:hover { transform: translateY(-5px); box-shadow: 0 6px 20px rgba(0,0,0,0.15); }
      table { width: 100%; }
      td { vertical-align: top; }
      .right { text-align: right; }
      .underline { border-bottom: 2px solid #000; min-width: 40px; display: inline-block; }
      .input-answer { border: none; border-bottom: 1px solid #ccc; text-align: right; background: transparent; transition: border-color 0.3s ease; }
      .input-answer:focus { outline: none; border-color: #FF9900; }
      @keyframes fadeInDown { from { opacity: 0; transform: translateY(-20px); } to { opacity: 1; transform: translateY(0); } }
      @media (max-width: 576px) { .exercise { width: 100px; padding: 5px; } .input-answer { width: 40px !important; } }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container animate__animated animate__fadeIn">
      <h1>Exercises - Level {{ level|capitalize }} - {% if selected_category=='all' %}All{% else %}{{ selected_category|capitalize }}{% endif %}</h1>
      <form method="POST" action="/answers">
        <input type="hidden" name="phase" value="answers">
        <input type="hidden" name="level" value="{{ level }}">
        <input type="hidden" name="selected_category" value="{{ selected_category }}">
        <input type="hidden" name="theme" value="{{ session.theme }}">
        {% for cat, ex_list in exercises.items() %}
          <div class="category">
            <h2 class="category-title">{{ cat|capitalize }}</h2>
            <div class="row">
              {% for ex in ex_list %}
              <div class="col-md-3 col-sm-4 col-6">
                <div class="exercise">
                  <div class="number">{{ loop.index }}.</div>
                  <table>
                    <tr>
                      <td class="right" colspan="2">{{ ex.a }}</td>
                    </tr>
                    <tr>
                      <td class="right" style="width:30px;">{{ ex.op }}</td>
                      <td class="right"><span class="underline">{{ ex.b }}</span></td>
                    </tr>
                    <tr>
                      <td class="right">=</td>
                      <td class="right">
                        <input type="text" name="{{ cat }}_{{ loop.index0 }}" class="input-answer" style="width:{{ 10 * ex.res_len if 10 * ex.res_len > 40 else 40 }}px;">
                      </td>
                    </tr>
                  </table>
                </div>
              </div>
              <input type="hidden" name="{{ cat }}_{{ loop.index0 }}_a" value="{{ ex.a }}">
              <input type="hidden" name="{{ cat }}_{{ loop.index0 }}_b" value="{{ ex.b }}">
              <input type="hidden" name="{{ cat }}_{{ loop.index0 }}_op" value="{{ ex.op }}">
              {% endfor %}
            </div>
          </div>
        {% endfor %}
        <div class="row">
          <div class="col-md-6">
            <button type="submit" class="btn btn-success btn-block mt-4"><i class="fas fa-check"></i> Submit</button>
          </div>
          <div class="col-md-6">
            <a href="/generate_pdf" class="btn btn-info btn-block mt-4"><i class="fas fa-file-pdf"></i> PDF</a>
          </div>
        </div>
      </form>
      """ + footer_html + """
    </div>
  </body>
</html>
"""

result_template = """
<!doctype html>
<html lang="en">
  <head>
    """ + meta_head + """
    <title>Results</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css">
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      h1 { text-align: center; margin-bottom: 30px; animation: fadeInDown 0.8s ease; }
      .result { font-family: monospace; padding: 10px; margin: 5px 0; border-radius: 5px; transition: all 0.3s ease; }
      .correct { background-color: rgba(0, 255, 0, 0.2); }
      .incorrect { background-color: rgba(255, 0, 0, 0.2); }
      .category { margin-bottom: 30px; }
      .btn-container { margin-top: 20px; }
      .score-motivation { text-align: center; font-size: 1.5em; margin: 20px 0; padding: 10px; border-radius: 10px; background: linear-gradient(135deg, #FF9900, #FFCC00); color: white; box-shadow: 0 4px 15px rgba(0,0,0,0.2); }
      @keyframes fadeInDown { from { opacity: 0; transform: translateY(-20px); } to { opacity: 1; transform: translateY(0); } }
      @media (max-width: 576px) { h1 { font-size: 1.8em; } }
    </style>
  </head>
  <body class="{{ theme }}">
    """ + nav_html + """
    <div class="container animate__animated animate__fadeIn">
      <h1>Results</h1>
      <div class="score-motivation animate__animated animate__bounceIn">
        {% if score >= 0 and score <= 20 %}
          Don't worry, keep practicing!
        {% elif score >= 21 and score <= 40 %}
          You're making progress, keep it up!
        {% elif score >= 41 and score <= 60 %}
          Well done, you're on the right track!
        {% elif score >= 61 and score <= 80 %}
          Excellent work, you're almost at the top!
        {% elif score >= 81 and score <= 100 %}
          Congratulations, you're a champion!
        {% endif %}
      </div>
      {% for cat, results in feedback.items() %}
        <div class="category">
          <h2>{{ cat|capitalize }}</h2>
          {% for res in results %}
            <div class="result {% if res.correct %}correct{% else %}incorrect{% endif %} animate__animated animate__fadeIn">
              {{ res.text }}
            </div>
          {% endfor %}
        </div>
      {% endfor %}
      <div class="btn-container row">
        <div class="col-md-6">
          <a href="/generate_pdf" class="btn btn-info btn-block"><i class="fas fa-file-pdf"></i> Download PDF</a>
        </div>
        <div class="col-md-6">
          <a href="/" class="btn btn-secondary btn-block"><i class="fas fa-redo"></i> Restart</a>
        </div>
      </div>
      """ + footer_html + """
    </div>
  </body>
</html>
"""

choose_plan_template = """
<!doctype html>
<html>
  <head>
    """ + meta_head + """
    <title>Choose a Plan</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .container { max-width: 600px; margin-top: 50px; background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); }
      .plan-footer { margin-top: 30px; text-align: center; font-size: 0.9em; color: #343a40; }
      @media (max-width: 576px) { .container { padding: 20px; } }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container mt-5">
      <h1>Choose Your Plan</h1>
      <p>Please select one of the options:</p>
      <form method="POST">
        <div class="form-check">
          <input class="form-check-input" type="radio" name="plan" id="planFree" value="free"
            {% if free_disabled %}disabled{% endif %} required>
          <label class="form-check-label" for="planFree">
            Free (1 use per level){% if free_disabled %} - Already used up{% endif %}
          </label>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="plan" id="planMonthly" value="monthly" required>
          <label class="form-check-label" for="planMonthly">
            1 Month $10 (Unlimited access for 30 days) - Payment via PayPal
          </label>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="plan" id="planTwenty" value="twenty" required>
          <label class="form-check-label" for="planTwenty">
            20 Tries $5 (Maximum 20 uses) - Payment via PayPal
          </label>
        </div>
        <button type="submit" class="btn btn-primary mt-3"><i class="fas fa-check"></i> Submit</button>
      </form>
      <div class="plan-footer">
        SASTOUKA DIGITAL © 2025 sastoukadigital@gmail.com • Whatsapp +212652084735
      </div>
    </div>
  </body>
</html>
"""

login_template = """
<!doctype html>
<html lang="en">
  <head>
    """ + meta_head + """
    <title>Login</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&display=swap" rel="stylesheet">
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .login-container { max-width: 400px; margin: 50px auto; background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); backdrop-filter: blur(5px); }
      @media (max-width: 576px) { .login-container { padding: 20px; margin: 30px auto; } }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container login-container">
      <h1 class="text-center mb-4"><i class="fas fa-sign-in-alt"></i> Login</h1>
      <form method="POST" action="/login">
        <div class="form-group">
          <label for="email"><i class="fas fa-envelope"></i> Email</label>
          <input type="email" id="email" name="email" required class="form-control" placeholder="Enter your email">
        </div>
        <div class="form-group">
          <label for="password"><i class="fas fa-lock"></i> Password</label>
          <input type="password" id="password" name="password" required class="form-control" placeholder="Enter your password">
        </div>
        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" name="remember" id="rememberMe">
          <label class="form-check-label" for="rememberMe">Remember me</label>
        </div>
        <button type="submit" class="btn btn-primary btn-block"><i class="fas fa-sign-in-alt"></i> Login</button>
      </form>
    </div>
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""

register_template = """
<!doctype html>
<html>
  <head>
    """ + meta_head + """
    <title>Register</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .container { max-width: 500px; margin-top: 50px; background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container">
      <h1 class="mb-4 text-center"><i class="fas fa-user-plus"></i> Register</h1>
      <form method="POST" action="/register">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" required class="form-control">
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" required class="form-control">
        </div>
        <div class="form-group">
          <label>Confirm Password</label>
          <input type="password" name="confirm_password" required class="form-control">
        </div>
        <div class="form-group">
          <label>Birth Date</label>
          <input type="date" name="birth_date" required class="form-control">
        </div>
        <div class="form-group">
          <label>Birth Place</label>
          <input type="text" name="birth_place" required class="form-control">
        </div>
        <div class="form-group">
          <label>Father's Full Name</label>
          <input type="text" name="father_name" required class="form-control">
        </div>
        <div class="form-group">
          <label>Mother's Full Name</label>
          <input type="text" name="mother_name" required class="form-control">
        </div>
        <button type="submit" class="btn btn-primary"><i class="fas fa-check"></i> Create Account</button>
      </form>
    </div>
  </body>
</html>
"""

forgot_template = """
<!doctype html>
<html>
  <head>
    """ + meta_head + """
    <title>Forgot Password</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .container { max-width: 500px; margin-top: 50px; background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container">
      <h1 class="mb-4 text-center"><i class="fas fa-unlock-alt"></i> Forgot Password</h1>
      <p>Please enter your email, as well as your father's and mother's full names to verify your identity.</p>
      <form method="POST" action="/forgot_password">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" required class="form-control">
        </div>
        <div class="form-group">
          <label>Father's Full Name</label>
          <input type="text" name="father_name" required class="form-control">
        </div>
        <div class="form-group">
          <label>Mother's Full Name</label>
          <input type="text" name="mother_name" required class="form-control">
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" name="new_password" required class="form-control">
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" name="confirm_password" required class="form-control">
        </div>
        <button type="submit" class="btn btn-primary"><i class="fas fa-check"></i> Reset</button>
      </form>
    </div>
  </body>
</html>
"""

change_template = """
<!doctype html>
<html>
  <head>
    """ + meta_head + """
    <title>Change Password</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .container { max-width: 500px; margin-top: 50px; background: rgba(255,255,255,0.95); padding: 30px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.1); }
    </style>
  </head>
  <body class="{{ session.theme }}">
    """ + nav_html + """
    <div class="container">
      <h1 class="mb-4 text-center"><i class="fas fa-key"></i> Change Password</h1>
      <form method="POST" action="/change_password">
        <div class="form-group">
          <label>Old Password</label>
          <input type="password" name="old_password" required class="form-control">
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" name="new_password" required class="form-control">
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" name="confirm_password" required class="form-control">
        </div>
        <button type="submit" class="btn btn-primary"><i class="fas fa-check"></i> Change</button>
      </form>
    </div>
  </body>
</html>
"""

activation_template = """
<!doctype html>
<html lang="en">
  <head>
    """ + meta_head + """
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <title>Plan Activation</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    """ + common_theme_css + """
    <style>
      body { padding-left: 20px; padding-right: 20px; }
      .activation-email { font-weight: bold; }
      .nav-tabs .nav-link { font-size: 1.1em; }
      .tab-content { margin-top: 20px; }
      .card { background: rgba(255,255,255,0.95); backdrop-filter: blur(5px); }
      .activation-footer { margin-top: 30px; text-align: center; font-size: 0.9em; color: #343a40; }
    </style>
  </head>
  <body class="{{ session.theme }}">
    <div class="container mt-4">
      <h2 class="text-center">Plan Activation</h2>
      <div class="text-center mb-3">
          <p>Your activation email is: <span class="activation-email">{{ email }}</span></p>
      </div>
      <ul class="nav nav-tabs justify-content-center" id="activationTab" role="tablist">
        <li class="nav-item">
          <a class="nav-link active" id="payment-tab" data-toggle="tab" href="#payment" role="tab" aria-controls="payment" aria-selected="true">
            <i class="fas fa-credit-card"></i> Activation by Payment
          </a>
        </li>
        <li class="nav-item">
          <a class="nav-link" id="key-tab" data-toggle="tab" href="#key" role="tab" aria-controls="key" aria-selected="false">
            <i class="fas fa-key"></i> Activation by Key
          </a>
        </li>
        <li class="nav-item">
          <a class="nav-link" id="myemail-tab" data-toggle="tab" href="#myemail" role="tab" aria-controls="myemail" aria-selected="false">
            <i class="fas fa-envelope"></i> My Activation Email
          </a>
        </li>
      </ul>
      <div class="tab-content" id="activationTabContent">
        <div class="tab-pane fade show active" id="payment" role="tabpanel" aria-labelledby="payment-tab">
          <div class="card">
            <div class="card-body">
              <h4 class="card-title">Activation by Payment</h4>
              <p class="card-text">To activate a paid plan, please go to the <a href="{{ url_for('choose_plan') }}">Choose a Plan</a> page.</p>
            </div>
          </div>
        </div>
        <div class="tab-pane fade" id="key" role="tabpanel" aria-labelledby="key-tab">
          <div class="card">
            <div class="card-body">
              <h4 class="card-title">Activation by Key</h4>
              <form method="POST" action="{{ url_for('activate_key') }}">
                <div class="form-group">
                  <label for="activation_key">Enter your activation key:</label>
                  <input type="text" name="activation_key" id="activation_key" class="form-control" required>
                </div>
                <button type="submit" class="btn btn-primary">Validate Key</button>
              </form>
            </div>
          </div>
        </div>
        <div class="tab-pane fade" id="myemail" role="tabpanel" aria-labelledby="myemail-tab">
          <div class="card">
            <div class="card-body">
              <h4 class="card-title">My Activation Email</h4>
              <p>Your activation email is: <span class="activation-email">{{ email }}</span></p>
            </div>
          </div>
        </div>
      </div>
      <div class="activation-footer">
        SASTOUKA DIGITAL © 2025 sastoukadigital@gmail.com • Whatsapp +212652084735
      </div>
    </div>
    <script src="https://code.jquery.com/jquery-3.5.1.slim.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.2/dist/js/bootstrap.bundle.min.js"></script>
  </body>
</html>
"""
# -----------------------------------------------------------------------------
# Fonctions de gestion des plans et activation
# -----------------------------------------------------------------------------
def can_use_plan(email, level):
    user_data = users[email]
    usage_count = user_data.setdefault("usage_count", {"easy": 0, "intermediate": 0, "hard": 0, "very hard": 0, "expert": 0, "total": 0})
    if user_data["plan"] == "free":
        return usage_count.get(level, 0) < 1
    elif user_data["plan"] == "monthly":
        return True
    elif user_data["plan"] == "twenty":
        return usage_count["total"] < 20
    return False

def track_usage(email, level):
    user_data = users[email]
    usage_count = user_data.setdefault("usage_count", {"easy": 0, "intermediate": 0, "hard": 0, "very hard": 0, "expert": 0, "total": 0})
    plan = user_data["plan"]
    if plan == "free":
        usage_count[level] += 1
    elif plan == "twenty":
        usage_count["total"] += 1
        usage_count[level] += 1
    save_users()

# -----------------------------------------------------------------------------
# Gestion des thèmes et cookies
# -----------------------------------------------------------------------------
@app.before_request
def check_theme():
    theme = request.args.get('theme')
    if theme in ['blue', 'pink', 'green', 'yellow', 'kid_friendly']:
        session['theme'] = theme
        if "user" in session:
            users[session["user"]]["theme"] = theme
            save_users()
    elif "theme" not in session:
        if "user" in session and "theme" in users[session["user"]]:
            session["theme"] = users[session["user"]]["theme"]
        else:
            session["theme"] = "blue"

@app.before_request
def check_remember_me():
    if "user" not in session:
        token = request.cookies.get("remember_token")
        if token:
            for email, data in users.items():
                if data.get("remember_token") == token:
                    session["user"] = email
                    break

@app.route("/set_theme/<theme>")
def set_theme(theme):
    if theme not in ['blue', 'pink', 'green', 'yellow', 'kid_friendly']:
        flash("Invalid theme", "warning")
        return redirect(request.referrer or "/")
    session['theme'] = theme
    if "user" in session:
        users[session["user"]]["theme"] = theme
        save_users()
    flash(f"Theme changed to {theme}", "success")
    return redirect(request.referrer or "/")

# -----------------------------------------------------------------------------
# Route racine modifiée pour afficher une page d'accueil (landing page) si l'utilisateur n'est pas connecté
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index_get():
    if "user" not in session:
        return render_template_string(landing_template, session=session)
    email = session["user"]
    user_data = users[email]
    if "plan" not in user_data:
        return redirect("/choose_plan")
    if user_data["plan"] == "monthly":
        start = user_data["plan_start"]
        if datetime.now() > (start + timedelta(days=30)):
            flash("Your monthly subscription has expired. Please choose a new plan.", "warning")
            user_data.pop("plan", None)
            user_data.pop("plan_start", None)
            save_users()
            return redirect("/choose_plan")
    if user_data["plan"] == "free":
        usage_count = user_data.setdefault("usage_count", {"easy": 0, "intermediate": 0, "hard": 0, "very hard": 0, "expert": 0, "total": 0})
        levels = ["easy", "intermediate", "hard", "very hard", "expert"]
        all_exhausted = all(usage_count.get(lvl, 0) >= 1 for lvl in levels)
        if all_exhausted:
            flash("Your free trial is exhausted. Please choose another plan.", "warning")
            return redirect("/choose_plan")
    usage_count = user_data.setdefault("usage_count", {"easy": 0, "intermediate": 0, "hard": 0, "very hard": 0, "expert": 0, "total": 0})
    host_address = f"{get_local_ip()}:5500"
    levels = ["easy", "intermediate", "hard", "very hard", "expert"]
    can_use_dict = {lvl: can_use_plan(email, lvl) for lvl in levels}
    plan_start_str = ""
    plan_end_str = ""
    if user_data["plan"] == "monthly":
        plan_start_str = user_data["plan_start"].strftime("%Y-%m-%d")
        plan_end_str = (user_data["plan_start"] + timedelta(days=30)).strftime("%Y-%m-%d")
    return render_template_string(selection_template,
                                  session=session,
                                  user_plan=user_data["plan"],
                                  usage_count=usage_count,
                                  plan_start=plan_start_str,
                                  plan_end=plan_end_str,
                                  host_address=host_address,
                                  can_use=can_use_dict)

@app.route("/", methods=["POST"])
def index_post():
    if "user" not in session:
        return redirect("/login")
    email = session["user"]
    if "plan" not in users[email]:
        return redirect("/choose_plan")
    phase = request.form.get("phase")
    if phase == "generate":
        level = request.form.get("level")
        if not can_use_plan(email, level):
            flash("You have exhausted your uses for this level.", "danger")
            return redirect("/")
        track_usage(email, level)
        selected_category = request.form.get("category")
        theme = session.get("theme", "blue")
        nb_ops = int(request.form.get("nb_ops", 100))
        pdf_columns = int(request.form.get("pdf_columns", 3))
        global latest_exercises, latest_meta, latest_result
        latest_meta = {"level": level,
                       "selected_category": selected_category,
                       "theme": theme,
                       "nb_ops": nb_ops,
                       "pdf_columns": pdf_columns}
        if selected_category == "all":
            operations = ["addition", "subtraction", "multiplication", "division"]
        else:
            operations = [selected_category]
        exercises = {}
        for op in operations:
            exercises[op] = [generate_exercise(op, level) for _ in range(nb_ops)]
        latest_exercises = exercises
        latest_result = None
        host_address = f"{get_local_ip()}:5500"
        return render_template_string(exercise_template,
                                      exercises=exercises,
                                      level=level,
                                      selected_category=selected_category,
                                      host_address=host_address,
                                      session=session)
    return redirect("/")

@app.route("/choose_plan", methods=["GET", "POST"])
def choose_plan():
    if "user" not in session:
        return redirect("/login")
    email = session["user"]
    user_data = users[email]
    free_disabled = False
    if "usage_count" in user_data:
        levels = ["easy", "intermediate", "hard", "very hard", "expert"]
        free_disabled = all(user_data["usage_count"].get(lvl, 0) >= 1 for lvl in levels)
    if request.method == "POST":
        plan = request.form.get("plan")
        if plan in ("monthly", "twenty"):
            return redirect(url_for("purchase_plan", plan=plan))
        if plan not in ("free", "monthly", "twenty"):
            flash("Invalid plan.", "danger")
            return render_template_string(choose_plan_template, session=session, free_disabled=free_disabled)
        if plan == "free" and free_disabled:
            flash("Your free trial is exhausted. Please choose another plan.", "warning")
            return render_template_string(choose_plan_template, session=session, free_disabled=True)
        user_data["plan"] = plan
        user_data.setdefault("usage_count", {"easy": 0, "intermediate": 0, "hard": 0, "very hard": 0, "expert": 0, "total": 0})
        save_users()
        flash("Plan successfully saved.", "success")
        return redirect("/")
    return render_template_string(choose_plan_template, session=session, free_disabled=free_disabled)

@app.route("/activation")
def activation():
    if "user" not in session:
        return redirect("/login")
    email = session["user"]
    return render_template_string(activation_template, email=email)

@app.route("/activate_key", methods=["POST"])
def activate_key():
    if "user" not in session:
        return redirect("/login")
    activation_key_input = request.form.get("activation_key")
    email = session["user"]
    today = datetime.now().strftime("%Y%m%d")
    expected_key_monthly = generate_activation_key(email, "monthly", ACTIVATION_TOKEN, today)
    expected_key_twenty = generate_activation_key(email, "twenty", ACTIVATION_TOKEN, today)
    if activation_key_input == expected_key_monthly:
        update_activation_after_payment("monthly")
        flash("Activation key valid! Your monthly plan is activated.", "success")
        return redirect(url_for("index_get"))
    elif activation_key_input == expected_key_twenty:
        update_activation_after_payment("twenty")
        flash("Activation key valid! Your 20-tries plan is activated.", "success")
        return redirect(url_for("index_get"))
    else:
        flash("Invalid activation key.", "danger")
        return redirect(url_for("activation"))

def update_activation_after_payment(plan):
    email = session["user"]
    user_data = users[email]
    now_str = datetime.now().strftime("%Y%m%d%H%M%S")
    if plan == "monthly":
        activation_id = f"{email}_{now_str}"
        user_data["plan"] = "monthly"
        user_data["plan_start"] = datetime.now()
        user_data["activation_id"] = activation_id
    elif plan == "twenty":
        birth_date = user_data.get("birth_date", "unknown")
        activation_id = f"{email}_{birth_date}_{now_str}"
        user_data["plan"] = "twenty"
        user_data["activation_id"] = activation_id
    save_users()

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID") or "AYPizBBNq1vp8WyvzvTHITGq9KoUUTXmzE0DBA7D_lWl5Ir6wEwVCB-gorvd1jgyX35ZqyURK6SMvps5"
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET") or "EKSvwa_yK7ZYTuq45VP60dbRMzChbrko90EnhQsRzrMNZhqU2mHLti4_UTYV60ytY9uVZiAg7BoBlNno"
PAYPAL_OAUTH_URL = "https://api-m.paypal.com/v1/oauth2/token"
PAYPAL_ORDER_API = "https://api-m.paypal.com/v2/checkout/orders"

def get_paypal_access_token():
    response = requests.post(
        PAYPAL_OAUTH_URL,
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET)
    )
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        raise Exception(f"Error obtaining PayPal token: {response.status_code} {response.text}")

def create_paypal_order(amount, currency="USD"):
    token = get_paypal_access_token()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": amount}}],
        "application_context": {
            "return_url": url_for("paypal_success", _external=True),
            "cancel_url": url_for("paypal_cancel", _external=True),
            "landing_page": "BILLING"
        }
    }
    response = requests.post(PAYPAL_ORDER_API, json=body, headers=headers)
    if response.status_code in (200, 201):
        data = response.json()
        order_id = data["id"]
        approval_url = None
        for link in data["links"]:
            if link["rel"] in ("approve", "payer-action"):
                approval_url = link["href"]
                break
        return order_id, approval_url
    else:
        raise Exception(f"Error creating PayPal order: {response.status_code} {response.text}")

def capture_paypal_order(order_id):
    token = get_paypal_access_token()
    url = f"{PAYPAL_ORDER_API}/{order_id}/capture"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    response = requests.post(url, headers=headers)
    if response.status_code in (200, 201):
        data = response.json()
        if data.get("status") == "COMPLETED":
            return True
        return False
    return False

purchase_orders = {}

@app.route("/purchase_plan/<plan>")
def purchase_plan(plan):
    if "user" not in session:
        flash("Please log in to make a purchase.", "warning")
        return redirect("/login")
    if plan not in ["monthly", "twenty"]:
        return "Invalid plan", 400
    amount = "10.00" if plan == "monthly" else "5.00"
    try:
        order_id, approval_url = create_paypal_order(amount, "USD")
        purchase_orders[order_id] = plan
        return redirect(approval_url)
    except Exception as e:
        return f"Error: {e}"

@app.route("/paypal_success")
def paypal_success():
    order_id = request.args.get("token", None)
    if not order_id:
        return "Missing 'token' parameter in URL."
    success = capture_paypal_order(order_id)
    if success:
        plan = purchase_orders.get(order_id)
        if plan:
            update_activation_after_payment(plan)
            flash(f"Payment validated for the {plan} plan!", "success")
        else:
            flash("Payment validated, but unknown plan.", "error")
        return redirect(url_for("index_get"))
    else:
        flash("Payment not completed.", "error")
        return redirect(url_for("index_get"))

@app.route("/paypal_cancel")
def paypal_cancel():
    flash("Payment cancelled by the user.", "error")
    return redirect(url_for("index_get"))

@app.route("/generate_pdf")
def generate_pdf_route():
    global latest_result, latest_exercises, latest_meta
    if not latest_result:
        if not latest_exercises or not latest_meta:
            return "No result to convert to PDF.", 400
        solutions = {}
        for op, ex_list in latest_exercises.items():
            solutions[op] = []
            for ex in ex_list:
                question_text = f"{ex['a']:3d} {ex['op']} {ex['b']:3d}"
                solutions[op].append({"question": question_text, "solution": ex["result"]})
        latest_result = {
            "feedback": {},
            "solutions": solutions,
            "score": 0,
            "theme": latest_meta["theme"],
            "level": latest_meta["level"],
            "selected_category": latest_meta["selected_category"],
            "exercises": latest_exercises,
            "pdf_columns": latest_meta["pdf_columns"]
        }
    pdf_columns = latest_meta.get("pdf_columns", 3)
    pdf = FPDF(orientation="P", unit="mm", format="A5")
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_page()
    pdf.set_y(pdf.h / 2 - 20)
    pdf.set_font("Arial", "B", 28)
    pdf.cell(0, 10, "Math Exercises", ln=True, align="C")
    pdf.ln(10)
    pdf.set_font("Arial", "I", 20)
    pdf.cell(0, 10, "by SASTOUKA DIGITAL", ln=True, align="C")
    pdf.add_page()
    pdf.set_font("Arial", "B", 18)
    pdf.cell(0, 10, "Questions", ln=True, align="C")
    pdf.ln(5)
    col_width = (pdf.w - pdf.l_margin - pdf.r_margin) / pdf_columns
    line_height = 6
    box_height = 3 * line_height
    exercise_categories = list(latest_result["exercises"].items())
    for idx, (cat, ex_list) in enumerate(exercise_categories):
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, cat.capitalize(), ln=True)
        y = pdf.get_y()
        x = pdf.l_margin
        col = 0
        for i, ex in enumerate(ex_list):
            draw_exercise_box(pdf, i+1, ex, x, y, col_width, line_height, solution_text=None)
            col += 1
            if col == pdf_columns:
                col = 0
                x = pdf.l_margin
                y += box_height + 4
                if y + box_height > pdf.h - pdf.b_margin:
                    pdf.add_page()
                    y = pdf.t_margin
            else:
                x += col_width
        if idx != len(exercise_categories) - 1:
            pdf.add_page()
    pdf.add_page()
    pdf.set_font("Arial", "B", 18)
    pdf.cell(0, 10, "Solutions", ln=True, align="C")
    pdf.ln(5)
    solution_categories = list(latest_result["solutions"].items())
    for idx, (cat, sol_list) in enumerate(solution_categories):
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, cat.capitalize(), ln=True)
        y = pdf.get_y()
        x = pdf.l_margin
        col = 0
        for i, sol in enumerate(sol_list):
            parts = sol["question"].split()
            ex = {"a": parts[0], "op": parts[1], "b": parts[2]}
            draw_exercise_box(pdf, i+1, ex, x, y, col_width, line_height, solution_text=sol["solution"])
            col += 1
            if col == pdf_columns:
                col = 0
                x = pdf.l_margin
                y += box_height + 4
                if y + box_height > pdf.h - pdf.b_margin:
                    pdf.add_page()
                    y = pdf.t_margin
            else:
                x += col_width
        if idx != len(solution_categories) - 1:
            pdf.add_page()
    # Générer le PDF en mémoire
    pdf_data = pdf.output(dest="S").encode("latin1")
    user_email = session.get("user", "default_user")
    folder_id = get_user_folder_id(user_email)
    upload_bytes_to_drive(pdf_data, "exercise_results.pdf", mime_type="application/pdf", folder_id=folder_id)
    return send_file(io.BytesIO(pdf_data), mimetype='application/pdf', as_attachment=True, download_name="exercise_results.pdf")

@app.route("/answers", methods=["POST"])
def answers_route():
    global latest_exercises, latest_result, latest_meta
    if "user" not in session:
        return redirect("/login")
    level = request.form.get("level")
    selected_category = request.form.get("selected_category")
    theme = request.form.get("theme")
    if not latest_exercises or not latest_meta:
        flash("No exercise in progress.", "danger")
        return redirect("/")
    feedback = {}
    total_correct = 0
    total_questions = 0
    if selected_category == "all":
        operations = ["addition", "subtraction", "multiplication", "division"]
    else:
        operations = [selected_category]
    for op in operations:
        feedback[op] = []
        op_keys = [key for key in request.form.keys() if key.startswith(f"{op}_") and key.endswith("_a")]
        nb_ops = len(op_keys)
        for i in range(nb_ops):
            user_answer = request.form.get(f"{op}_{i}")
            if user_answer is None or user_answer.strip() == "":
                user_answer = "Not answered"
            else:
                try:
                    user_answer = int(user_answer)
                except:
                    user_answer = "Not answered"
            a = int(request.form.get(f"{op}_{i}_a"))
            b = int(request.form.get(f"{op}_{i}_b"))
            op_symbol = request.form.get(f"{op}_{i}_op")
            if op_symbol == '+':
                correct = a + b
            elif op_symbol == '-':
                correct = a - b
            elif op_symbol in ("*", "×"):
                correct = a * b
            elif op_symbol in ("/", "÷"):
                correct = a // b
            else:
                correct = None
            total_questions += 1
            question_text = f"{a:3d} {op_symbol} {b:3d}"
            if user_answer == "Not answered":
                feedback[op].append({"text": f"{i+1:3d}. {question_text} = {user_answer}", "correct": False})
            elif user_answer == correct:
                feedback[op].append({"text": f"{i+1:3d}. {question_text} = {user_answer} -> Well done", "correct": True})
                total_correct += 1
            else:
                feedback[op].append({"text": f"{i+1:3d}. {question_text} = {user_answer} -> Try again (expected {correct})", "correct": False})
    score = round((total_correct / total_questions) * 100) if total_questions > 0 else 0
    solutions = {}
    for op, ex_list in latest_exercises.items():
        solutions[op] = []
        for ex in ex_list:
            question_text = f"{ex['a']:3d} {ex['op']} {ex['b']:3d}"
            solutions[op].append({"question": question_text, "solution": ex["result"]})
    host_address = f"{get_local_ip()}:5500"
    rendered = render_template_string(result_template,
                                      feedback=feedback,
                                      score=score,
                                      theme=theme,
                                      host_address=host_address,
                                      session=session)
    latest_result = {"feedback": feedback,
                     "solutions": solutions,
                     "score": score,
                     "theme": theme,
                     "level": latest_meta["level"],
                     "selected_category": latest_meta["selected_category"],
                     "exercises": latest_exercises,
                     "pdf_columns": latest_meta["pdf_columns"]}
    return rendered

@app.route("/login", methods=["GET", "POST"])
def login_route():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        remember = request.form.get("remember")
        if email in users:
            stored_hash = users[email]["password"]
            if stored_hash == hash_password(password):
                session["user"] = email
                flash("Login successful.", "success")
                resp = make_response(redirect("/"))
                if remember == "on":
                    token = secrets.token_hex(32)
                    users[email]["remember_token"] = token
                    expires = datetime.now() + timedelta(days=30)
                    resp.set_cookie("remember_token", token, expires=expires)
                else:
                    resp.set_cookie("remember_token", "", expires=0)
                    users[email].pop("remember_token", None)
                save_users()
                return resp
        flash("Invalid credentials.", "danger")
    return render_template_string(login_template, session=session)

@app.route("/logout")
def logout_route():
    if "user" in session:
        email = session["user"]
        if email in users:
            users[email].pop("remember_token", None)
    session.pop("user", None)
    flash("Logged out.", "info")
    resp = make_response(redirect("/login"))
    resp.set_cookie("remember_token", "", expires=0)
    save_users()
    return resp

@app.route("/register", methods=["GET", "POST"])
def register_route():
    if request.method == "POST":
        email = request.form.get("email")
        pw = request.form.get("password")
        cpw = request.form.get("confirm_password")
        birth_date = request.form.get("birth_date")
        birth_place = request.form.get("birth_place")
        father = request.form.get("father_name")
        mother = request.form.get("mother_name")
        if email in users:
            flash("This email is already used.", "warning")
            return render_template_string(register_template, session=session)
        if pw != cpw:
            flash("Passwords do not match.", "warning")
            return render_template_string(register_template, session=session)
        users[email] = {"password": hash_password(pw),
                        "birth_date": birth_date,
                        "birth_place": birth_place,
                        "father_name": father,
                        "mother_name": mother}
        save_users()
        flash("Account created successfully!", "success")
        return redirect("/login")
    return render_template_string(register_template, session=session)

@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password_route():
    if request.method == "POST":
        email = request.form.get("email")
        father = request.form.get("father_name")
        mother = request.form.get("mother_name")
        new_pw = request.form.get("new_password")
        conf_pw = request.form.get("confirm_password")
        if email not in users:
            flash("Email not found.", "danger")
            return render_template_string(forgot_template, session=session)
        if new_pw != conf_pw:
            flash("Passwords do not match.", "warning")
            return render_template_string(forgot_template, session=session)
        user_data = users[email]
        if user_data["father_name"] == father and user_data["mother_name"] == mother:
            user_data["password"] = hash_password(new_pw)
            save_users()
            flash("Password reset successfully!", "success")
            return redirect("/login")
        else:
            flash("Incorrect verification information.", "danger")
    return render_template_string(forgot_template, session=session)

@app.route("/change_password", methods=["GET", "POST"])
def change_password_route():
    if "user" not in session:
        flash("Please log in.", "warning")
        return redirect("/login")
    if request.method == "POST":
        old_pw = request.form.get("old_password")
        new_pw = request.form.get("new_password")
        conf_pw = request.form.get("confirm_password")
        email = session["user"]
        user_data = users[email]
        if user_data["password"] != hash_password(old_pw):
            flash("Incorrect old password.", "danger")
            return render_template_string(change_template, session=session)
        if new_pw != conf_pw:
            flash("New passwords do not match.", "warning")
            return render_template_string(change_template, session=session)
        user_data["password"] = hash_password(new_pw)
        save_users()
        flash("Password changed successfully!", "success")
        return redirect("/")
    return render_template_string(change_template, session=session)

@app.route("/logout")
def root():
    if "user" not in session:
        return redirect("/login")
    else:
        return redirect("/")

# -----------------------------------------------------------------------------
# Lancement de l'application
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    # Pour la production, ne pas utiliser le serveur intégré.
    port = int(os.environ.get("PORT", 5500))
    app.run(host="0.0.0.0", port=port, debug=False)
