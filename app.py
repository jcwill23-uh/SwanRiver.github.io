import os
import urllib.parse
import logging
from flask import Flask, redirect, url_for, session, request, render_template, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from functools import wraps
import msal
import requests
import pyodbc
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__, template_folder='docs', static_folder='docs')
app.secret_key = os.getenv('SECRET_KEY')

# Configure session storage
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_FILE_DIR'] = '/tmp/flask_session'
app.config['SESSION_FILE_THRESHOLD'] = 100
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Ensure session directory exists
os.makedirs(app.config['SESSION_FILE_DIR'], exist_ok=True)

# Azure SQL Database setup
params = urllib.parse.quote_plus(
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=tcp:swan-river-user-information.database.windows.net,1433;"
    "DATABASE=UserDatabase;"
    "UID=jcwill23@cougarnet.uh.edu@swan-river-user-information;"
    "PWD=H1ghLander;"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30"
)

# Configure SQLAlchemy engine
engine = create_engine(
    f"mssql+pyodbc:///?odbc_connect={params}",
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=10
)

# Bind engine to SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = engine.url
db = SQLAlchemy(app)

# User Model
class User(db.Model):
    __tablename__ = 'User'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    role = db.Column(db.String(50), default="basicuser")
    status = db.Column(db.String(20), default="active")

# Azure AD Configuration
CLIENT_ID = "7fbeba40-e221-4797-8f8a-dc364de519c7"
CLIENT_SECRET = "x2T8Q~yVzAOoC~r6FYtzK6sqCJQR_~RCVH5-dcw8"
TENANT_ID = "170bbabd-a2f0-4c90-ad4b-0e8f0f0c4259"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = "https://swan-river-group-project.azurewebsites.net/auth/callback"
SCOPE = ['User.Read']

# Authentication Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/azure_login')
def azure_login():
    session['state'] = 'random_state'
    auth_url = _build_auth_url(scopes=SCOPE, state=session['state'])
    return redirect(auth_url)

@app.route('/auth/callback')
def authorized():
    try:
        code = request.args.get('code')
        if not code:
            return redirect(url_for('index'))
        
        token = _get_token_from_code(code)
        user_info = _get_user_info(token)
        email = user_info.get('mail') or user_info.get('userPrincipalName')

        logger.info(f"User attempting login: {email}")

        # Fetch user from database
        user = User.query.filter_by(email=email).first()

        if not user:
            logger.info(f"User {email} not found. Creating new basic user.")
            user = User(
                name=user_info.get('displayName', 'Unknown'),
                email=email,
                role='basicuser',  # Default role
                status='active'    # Default status
            )
            db.session.add(user)
            db.session.commit()
            db.session.refresh(user)  # Ensure role and status are fresh

        else:
            logger.info(f"User {email} found in database with role: {user.role} and status: {user.status}")

        db.session.refresh(user)  # Refresh to get the latest role and status

        # Check if the user is suspended
        if user.status.lower() != "active":
            logger.warning(f"User {email} is suspended. Redirecting to login.")
            flash("Account suspended. Please contact support.", "error")
            return redirect(url_for('login'))

        # Store user details properly in session
        session['user'] = {
            'name': user.name,
            'email': user.email,
            'role': user.role.strip().lower(),  # Ensure this reflects the database value
            'status': user.status
        }

        logger.info(f"User {user.email} logged in with role: {session['user']['role']} and status: {session['user']['status']}")

         # Debug before role check
        logger.info(f"Checking role for {user.email}: session['user']['role'] = {session['user']['role']}")

        # Redirect based on role
        if session.get('user', {}).get('role', '').strip().lower() == "admin":
            logger.info(f"Admin {user.email} is being redirected to admin_home")
            return redirect(url_for('admin_home'))
        else:
            return redirect(url_for('basic_user_home'))

    except Exception as e:
        logger.error(f"Internal Server Error: {str(e)}", exc_info=True)
        flash("An error occurred while logging in. Please try again.", "error")
        return redirect(url_for('login'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# Basic User Routes
@app.route('/basic_user_home')
def basic_user_home():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('basic_user_home.html', user_name=session['user']['name'])

@app.route('/basic_user_view')
def basic_user_view():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template("basic_user_view.html", user=session['user'])

@app.route('/basic_user_edit')
def basic_user_edit():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template("basic_user_edit.html", user=session['user'])

# Admin Routes
@app.route('/admin_home')
def admin_home():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin.html', user_name=session['user']['name'])

@app.route('/admin_create_user')
def admin_create_user():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-create-user.html')

@app.route('/admin_delete_user')
def admin_delete_user():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-delete-user.html')

@app.route('/admin_edit_profile')
def admin_edit_profile():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-edit-profile.html', user=session['user'])

@app.route('/admin_update_user')
def admin_update_user():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-update-user.html')

@app.route('/admin_view_profile')
def admin_view_profile():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-view-profile.html', user=session['user'])

@app.route('/admin_view_users')
def admin_view_users():
    if 'user' not in session:
        return redirect(url_for('index'))
    return render_template('admin-view-user.html')


@app.route('/user/profile/update', methods=['PUT'])
def update_user_profile():
    if "user" not in session:
        return jsonify({"error": "User not logged in"}), 401

    user = User.query.filter_by(email=session['user'].get('email')).first()
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json()
    new_name = data.get("name", user.name)
    new_email = data.get("email", user.email)

    # Prevent duplicate emails
    if new_email != user.email:
        existing_user = User.query.filter_by(email=new_email).first()
        if existing_user:
            return jsonify({"error": "Email already in use"}), 400

    user.name = new_name
    user.email = new_email
    db.session.commit()

    # Update session with new user data
    session['user'] = {'name': user.name, 'email': user.email, 'role': user.role, 'status': user.status}

    return jsonify({"message": "Profile updated successfully!"})

@app.route('/admin/create_user', methods=['POST'])
def create_user():
    if "user" not in session or session["user"]["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    role = data.get("role", "basicuser").strip().lower()
    status = data.get("status", "active").strip().lower()

    if not name or not email:
        return jsonify({"error": "Name and email are required"}), 400

    # Check if the email already exists
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"error": "User with this email already exists"}), 400

    # Create new user
    new_user = User(name=name, email=email, role=role, status=status)
    db.session.add(new_user)
    db.session.commit()

    logger.info(f"Admin {session['user']['email']} created new user {email} with role {role}")

    return jsonify({"message": "User created successfully!"}), 201

# Update user information in database
@app.route('/admin/update_user/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    if "user" not in session or session["user"]["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        data = request.get_json()
        new_name = data.get("name", user.name).strip()
        new_email = data.get("email", user.email).strip()
        new_role = data.get("role", user.role).strip().lower()
        new_status = data.get("status", user.status).strip().lower()

        # Prevent duplicate emails
        if new_email != user.email:
            existing_user = User.query.filter_by(email=new_email).first()
            if existing_user:
                return jsonify({"error": "Email already in use"}), 400

        # Update user details
        user.name = new_name
        user.email = new_email
        user.role = new_role
        user.status = new_status

        db.session.commit()

        logger.info(f"Admin {session['user']['email']} updated user {user.email}")

        return jsonify({"message": "User updated successfully!"}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error: {str(e)}")
        return jsonify({"error": "Database error, could not update user"}), 500

# Suspend user's account
@app.route('/admin/deactivate_user/<int:user_id>', methods=['PUT'])
def deactivate_user(user_id):
    if "user" not in session or session["user"]["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json()
    new_status = data.get("status", "deactivated").strip().lower()

    # Ensure the status is valid
    if new_status not in ["active", "deactivated"]:
        return jsonify({"error": "Invalid status value"}), 400

    user.status = new_status
    db.session.commit()

    logger.info(f"Admin {session['user']['email']} suspended user {user.email}")

    return jsonify({"message": "User suspended successfully!"}), 200

# Fetch all users in database
@app.route('/admin/all_users')
def all_users():
    users = User.query.all()
    return jsonify([
        {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "status": user.status}
        for user in users
    ])
    
# Helper functions
def _build_auth_url(scopes=None, state=None):
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY).get_authorization_request_url(
        scopes, state=state, redirect_uri=REDIRECT_URI)

def _get_token_from_code(code):
    client = msal.ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
    result = client.acquire_token_by_authorization_code(code, scopes=SCOPE, redirect_uri=REDIRECT_URI)
    return result.get("access_token")

def _get_user_info(token):
    return requests.get('https://graph.microsoft.com/v1.0/me', headers={'Authorization': 'Bearer ' + token}).json()

# Run Flask App
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run()
    
