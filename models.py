from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
import json

db = SQLAlchemy()


def get_existing_services():
    """Return all unique service categories currently in use across all listings."""
    from sqlalchemy import text
    rows = db.session.execute(text("SELECT listings FROM seller_profile")).fetchall()
    seen = set()
    for (raw,) in rows:
        for listing in json.loads(raw or "[]"):
            s = listing.get("service", "").lower().strip()
            if s:
                seen.add(s)
    return sorted(seen)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(10), nullable=False)  # 'buyer' or 'seller'
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(30))
    city = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=_utcnow)

    seller_profile = db.relationship('SellerProfile', backref='user', uselist=False)
    searches = db.relationship('Search', backref='buyer', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class SellerProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    raw_description = db.Column(db.Text)
    profile_description = db.Column(db.Text)
    website_url = db.Column(db.Text)
    website_pages_json = db.Column(db.Text)
    website_page_count = db.Column(db.Integer, default=0)
    website_scraped_at = db.Column(db.DateTime)
    city = db.Column(db.String(100))
    # listings: JSON array of {service, availability_days[], price_min, price_max}
    listings = db.Column(db.Text, default='[]')
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    cities = db.Column(db.Text, default='[]')          # JSON list of city strings
    avg_rating = db.Column(db.Float, nullable=True)   # None = no ratings yet
    rating_count = db.Column(db.Integer, default=0)
    contact_email = db.Column(db.String(120), nullable=True)

    def get_listings(self):
        return json.loads(self.listings or '[]')

    def set_listings(self, lst):
        self.listings = json.dumps(lst)

    def get_cities(self):
        """Return list of lowercase city strings (supports legacy single city field)."""
        raw = json.loads(self.cities or '[]')
        if raw:
            return [c.lower() for c in raw if c]
        if self.city:
            return [self.city.lower()]
        return []

    def set_cities(self, lst):
        self.cities = json.dumps(lst)

    def recalculate_rating(self):
        """Recompute avg_rating and rating_count from completed, rated transactions."""
        ratings = [t.rating for t in self.user.transactions_as_seller
                   if t.status == 'completed' and t.rated and t.rating]
        self.rating_count = len(ratings)
        self.avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None


class Search(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    raw_query = db.Column(db.Text)
    mapped_service = db.Column(db.String(100))
    city = db.Column(db.String(100))
    price_max = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=_utcnow)


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    buyer = db.relationship('User', foreign_keys=[buyer_id], backref='conversations_as_buyer')
    seller = db.relationship('User', foreign_keys=[seller_id], backref='conversations_as_seller')
    messages = db.relationship('Message', backref='conversation', lazy=True,
                               order_by='Message.created_at', cascade='all, delete-orphan')

    def other_party(self, user_id):
        return self.seller if self.buyer_id == user_id else self.buyer

    def unread_count(self, user_id):
        return sum(1 for m in self.messages if not m.is_read and m.sender_id != user_id)

    def last_message(self):
        return self.messages[-1] if self.messages else None


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    is_read = db.Column(db.Boolean, default=False)
    # 'text' | 'payment' | 'quote_request' | 'quote_response'
    message_type = db.Column(db.String(20), default='text')
    transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True)
    quote_request_id = db.Column(db.Integer, nullable=True)  # FK added via migration

    sender = db.relationship('User', foreign_keys=[sender_id])
    transaction = db.relationship('Transaction', foreign_keys=[transaction_id])


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversation.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)           # SEK
    description = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default='pending')     # 'pending' | 'completed' | 'cancelled'
    created_at = db.Column(db.DateTime, default=_utcnow)
    rated = db.Column(db.Boolean, default=False)
    rating = db.Column(db.Integer, nullable=True)            # 1–5, anonymous

    conversation = db.relationship('Conversation', backref='transactions')
    seller = db.relationship('User', foreign_keys=[seller_id], backref='transactions_as_seller')
    buyer = db.relationship('User', foreign_keys=[buyer_id], backref='transactions_as_buyer')


class ScrapeCache(db.Model):
    __tablename__ = "scrape_cache"
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.Text, nullable=False, unique=True)
    pages_json = db.Column(db.Text, nullable=False)
    page_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)


class QuoteRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    service = db.Column(db.String(80))
    cities = db.Column(db.Text, default='[]')  # JSON
    formatted_request = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow)

    buyer = db.relationship('User', foreign_keys=[buyer_id], backref='quote_requests')
    responses = db.relationship('QuoteResponse', back_populates='request', cascade='all, delete-orphan')

    def get_cities(self):
        return json.loads(self.cities or '[]')


class QuoteResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quote_request_id = db.Column(db.Integer, db.ForeignKey('quote_request.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    formatted_response = db.Column(db.Text)
    price_offered = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    request = db.relationship('QuoteRequest', back_populates='responses')
    seller = db.relationship('User', foreign_keys=[seller_id], backref='quote_responses_given')
