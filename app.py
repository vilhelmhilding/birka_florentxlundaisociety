import os, json, logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, redirect, url_for, session, flash
from models import db, User, SellerProfile
from llm import parse_seller_profile, parse_buyer_search, SERVICES

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'birka-dev-secret')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///marketplace.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
with app.app_context():
    db.create_all()

handler = RotatingFileHandler('birka.log', maxBytes=500_000, backupCount=3)
handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(message)s'))
log = logging.getLogger('birka')
log.setLevel(logging.INFO)
log.addHandler(handler)
log.propagate = False

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

def current_user():
    uid = session.get('user_id')
    return User.query.get(uid) if uid else None

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    u = current_user()
    if u:
        return redirect(url_for('seller_dashboard' if u.role == 'seller' else 'buyer_search'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if User.query.filter_by(email=request.form['email']).first():
            flash('Email already registered.')
            return redirect(url_for('register'))
        u = User(email=request.form['email'], name=request.form['name'],
                 phone=request.form.get('phone', ''), role=request.form['role'],
                 city=request.form.get('city', ''))
        u.set_password(request.form['password'])
        db.session.add(u)
        db.session.commit()
        session['user_id'] = u.id
        return redirect(url_for('seller_setup' if u.role == 'seller' else 'buyer_search'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(email=request.form['email']).first()
        if u and u.check_password(request.form['password']):
            session['user_id'] = u.id
            return redirect(url_for('seller_dashboard' if u.role == 'seller' else 'buyer_search'))
        flash('Invalid email or password.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ── Seller ────────────────────────────────────────────────────────────────────

@app.route('/seller/setup', methods=['GET', 'POST'])
def seller_setup():
    u = current_user()
    if not u or u.role != 'seller':
        return redirect(url_for('login'))
    if request.method == 'POST':
        data = parse_seller_profile(request.form['business_name'], request.form['description'])
        p = u.seller_profile or SellerProfile(user_id=u.id)
        p.business_name = request.form['business_name']
        p.raw_description = request.form['description']
        p.summary = data.get('summary', '')
        p.services = json.dumps(data.get('services', []))
        p.cities = json.dumps(data.get('cities', []))
        p.price_min = int(data.get('price_min') or 0)
        p.price_max = int(data.get('price_max') or 0)
        db.session.add(p)
        db.session.commit()
        return redirect(url_for('seller_dashboard'))
    return render_template('seller_setup.html', user=u, profile=u.seller_profile)

@app.route('/seller/dashboard')
def seller_dashboard():
    u = current_user()
    if not u or u.role != 'seller':
        return redirect(url_for('login'))
    return render_template('seller_dashboard.html', user=u, profile=u.seller_profile)

# ── Buyer ─────────────────────────────────────────────────────────────────────

@app.route('/search', methods=['GET', 'POST'])
def buyer_search():
    u = current_user()
    if not u or u.role != 'buyer':
        return redirect(url_for('login'))
    results, query, parsed = [], '', {}
    if request.method == 'POST':
        query = request.form['query']
        parsed = parse_buyer_search(query)
        services = parsed.get('services', [])
        city = parsed.get('city', '').lower()
        max_price = int(parsed.get('max_price') or 0)
        for p in SellerProfile.query.all():
            if not any(s in p.get_services() for s in services):
                continue
            if city and not any(city in c.lower() for c in p.get_cities()):
                continue
            if max_price and p.price_min and p.price_min > max_price:
                continue
            results.append(p)
    return render_template('search.html', user=u, results=results, query=query, parsed=parsed)

@app.route('/profile/<int:seller_id>')
def profile(seller_id):
    u = current_user()
    if not u:
        return redirect(url_for('login'))
    p = SellerProfile.query.get_or_404(seller_id)
    return render_template('profile.html', user=u, profile=p)

# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if not session.get('is_admin'):
        if request.method == 'POST' and request.form.get('password') == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin'))
        if request.method == 'POST':
            flash('Wrong password.')
        return render_template('admin_login.html')
    users = User.query.order_by(User.id).all()
    profiles = SellerProfile.query.order_by(SellerProfile.id).all()
    return render_template('admin.html', users=users, profiles=profiles, services=SERVICES)

@app.route('/admin/delete/user/<int:uid>', methods=['POST'])
def admin_delete_user(uid):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    u = User.query.get_or_404(uid)
    db.session.delete(u)
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/delete/profile/<int:pid>', methods=['POST'])
def admin_delete_profile(pid):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    p = SellerProfile.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
