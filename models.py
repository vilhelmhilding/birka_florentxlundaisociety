from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(30), default='')
    role = db.Column(db.String(10), nullable=False)  # 'buyer' or 'seller'
    city = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    seller_profile = db.relationship('SellerProfile', backref='user', uselist=False, cascade='all, delete-orphan')

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class SellerProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    business_name = db.Column(db.String(200), nullable=False)
    raw_description = db.Column(db.Text)
    summary = db.Column(db.Text)
    services = db.Column(db.Text, default='[]')   # JSON list of service keys
    cities = db.Column(db.Text, default='[]')      # JSON list of city names
    price_min = db.Column(db.Integer, default=0)
    price_max = db.Column(db.Integer, default=0)

    def get_services(self): return json.loads(self.services or '[]')
    def get_cities(self):   return json.loads(self.cities or '[]')
