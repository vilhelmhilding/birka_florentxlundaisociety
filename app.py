from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from models import db, User, SellerProfile, Search, Conversation, Message, Transaction, QuoteRequest, QuoteResponse, get_existing_services
from llm import parse_seller, parse_buyer, match_sellers, format_quote_request, format_quote_response
import json
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "birka-dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///marketplace.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    from sqlalchemy import text as _text
    with db.engine.connect() as _conn:
        for _stmt in [
            "ALTER TABLE message ADD COLUMN quote_request_id INTEGER",
        ]:
            try:
                _conn.execute(_text(_stmt))
                _conn.commit()
            except Exception:
                pass


# ── logging ───────────────────────────────────────────────────────────────────

LOG_PATH = os.path.join(os.path.dirname(__file__), "birka.log")
handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
log = logging.getLogger("birka")
log.setLevel(logging.DEBUG)
log.addHandler(handler)
log.addHandler(logging.StreamHandler())


@app.before_request
def log_request():
    log.info(f"→ {request.method} {request.path}  session_user={session.get('user_id')}  ip={request.remote_addr}")

@app.after_request
def log_response(response):
    log.info(f"← {response.status_code} {request.method} {request.path}")
    return response

@app.errorhandler(Exception)
def log_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        log.debug(f"HTTP {e.code} {request.method} {request.path}")
        return e
    log.exception(f"Unhandled exception on {request.method} {request.path}: {e}")
    return f"<pre>Internal error: {e}</pre>", 500


# ── helpers ───────────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None

@app.context_processor
def inject_unread():
    user = current_user()
    if user:
        convs = user.conversations_as_buyer + user.conversations_as_seller
        count = sum(c.unread_count(user.id) for c in convs)
        return {"unread_count": count}
    return {"unread_count": 0}

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def _sort_results(results, sort):
    if sort == "price":
        return sorted(results, key=lambda x: x[1].get("price_min") or float("inf"))
    return sorted(results,
                  key=lambda x: x[0].seller_profile.avg_rating or 0,
                  reverse=True)


# ── auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        role  = request.form["role"]
        email = request.form["email"].lower().strip()
        name  = request.form["name"].strip()
        phone = request.form.get("phone", "").strip()
        city  = request.form.get("city", "").strip()
        log.info(f"REGISTER attempt  role={role}  email={email}  name={name}  city={city}")

        if User.query.filter_by(email=email).first():
            flash("Email already registered.")
            return redirect(url_for("register"))

        user = User(email=email, role=role, name=name, phone=phone, city=city)
        user.set_password(request.form["password"])
        db.session.add(user)
        db.session.flush()

        if role == "seller":
            db.session.add(SellerProfile(user_id=user.id))

        db.session.commit()
        session["user_id"] = user.id
        log.info(f"REGISTER success  user_id={user.id}  role={role}")
        return redirect(url_for("dashboard"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(request.form["password"]):
            session["user_id"] = user.id
            log.info(f"LOGIN success  user_id={user.id}  role={user.role}")
            return redirect(url_for("dashboard"))
        log.warning(f"LOGIN failed  email={email}")
        flash("Invalid credentials.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    uid = session.pop("user_id", None)
    session.clear()
    log.info(f"LOGOUT  user_id={uid}")
    return redirect(url_for("index"))


# ── dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    user = current_user()
    log.info(f"DASHBOARD  user_id={user.id}  role={user.role}  method={request.method}")

    if user.role == "seller":
        if request.method == "POST":
            desc = request.form["description"].strip()
            parsed = parse_seller(desc, user.city or "", get_existing_services())
            profile = user.seller_profile
            profile.raw_description = desc
            cities = parsed.get("cities") or ([parsed.get("city")] if parsed.get("city") else [])
            cities = [c for c in cities if c]
            if not cities and user.city:
                cities = [user.city]
            profile.city = cities[0] if cities else user.city
            profile.set_cities(cities)
            profile.set_listings(parsed.get("listings", []))
            db.session.commit()
            unrecognized = parsed.get("unrecognized_cities") or []
            if unrecognized:
                flash(f"Profile updated — but we couldn't recognise these locations: {', '.join(unrecognized)}. Please check the spelling.")
            else:
                flash("Profile updated.")
        return render_template("dashboard_seller.html", user=user)

    # buyer
    if request.method == "POST":
        query = request.form["query"].strip()
        parsed = parse_buyer(query, user.city or "", get_existing_services())
        search = Search(
            buyer_id=user.id,
            raw_query=query,
            mapped_service=parsed.get("service"),
            city=parsed.get("city"),
            price_max=parsed.get("price_max"),
        )
        db.session.add(search)
        db.session.commit()
        sellers = User.query.filter_by(role="seller").join(SellerProfile).all()
        matched = match_sellers(parsed, sellers)
        session["last_search"] = {
            "parsed": {**parsed, "_raw_query": query,
                       "unrecognized_cities": parsed.get("unrecognized_cities") or []},
            "by_city": {
                city: [(s.id, listing, flags) for s, listing, flags in results]
                for city, results in matched["by_city"].items()
            },
            "other": [(s.id, listing, flags) for s, listing, flags in matched["other"]],
        }
        return redirect(url_for("dashboard"))

    by_city = {}
    other_results = []
    search_data = {}
    quote_seller_ids = []
    sort = request.args.get("sort", "rating")
    last = session.get("last_search")
    if last:
        search_data = last["parsed"]
        by_city = {
            city: [(User.query.get(i), l, f) for i, l, f in results if User.query.get(i)]
            for city, results in last.get("by_city", {}).items()
        }
        other_results = [(User.query.get(i), l, f) for i, l, f in last.get("other", []) if User.query.get(i)]
        by_city = {city: _sort_results(results, sort) for city, results in by_city.items()}
        other_results = _sort_results(other_results, sort)
        all_local = _sort_results(
            [item for results in by_city.values() for item in results], sort
        )
        seen = set()
        for sid, listing, _ in (list(item for lst in last.get("by_city", {}).values() for item in lst)
                                 + last.get("other", [])):
            if listing.get("is_quote") and sid not in seen:
                quote_seller_ids.append(sid)
                seen.add(sid)
    else:
        all_local = []

    quote_requests = (QuoteRequest.query
                      .filter_by(buyer_id=user.id)
                      .order_by(QuoteRequest.created_at.desc())
                      .all())

    return render_template("dashboard_buyer.html", user=user,
                           by_city=by_city,
                           all_local=all_local,
                           other_results=other_results,
                           search_data=search_data,
                           sort=sort,
                           quote_seller_ids=quote_seller_ids,
                           quote_requests=quote_requests)


# ── chat ──────────────────────────────────────────────────────────────────────

@app.route("/chat/start/<int:seller_id>", methods=["POST"])
@login_required
def chat_start(seller_id):
    user = current_user()
    if user.role != "buyer":
        flash("Only buyers can initiate conversations.")
        return redirect(url_for("dashboard"))
    seller = User.query.get_or_404(seller_id)
    conv = Conversation.query.filter_by(buyer_id=user.id, seller_id=seller_id).first()
    if not conv:
        conv = Conversation(buyer_id=user.id, seller_id=seller_id)
        db.session.add(conv)
        db.session.commit()
        log.info(f"CHAT new conversation  buyer={user.id}  seller={seller_id}  conv_id={conv.id}")
    return redirect(url_for("chat_view", conv_id=conv.id))

@app.route("/chat")
@login_required
def chat_list():
    user = current_user()
    convs = sorted(
        user.conversations_as_buyer + user.conversations_as_seller,
        key=lambda c: c.messages[-1].created_at if c.messages else c.created_at,
        reverse=True
    )
    return render_template("chat.html", user=user, conversations=convs, active_conv=None)

@app.route("/chat/<int:conv_id>")
@login_required
def chat_view(conv_id):
    user = current_user()
    conv = Conversation.query.get_or_404(conv_id)
    if conv.buyer_id != user.id and conv.seller_id != user.id:
        return redirect(url_for("chat_list"))
    for msg in conv.messages:
        if msg.sender_id != user.id and not msg.is_read:
            msg.is_read = True
    db.session.commit()
    convs = sorted(
        user.conversations_as_buyer + user.conversations_as_seller,
        key=lambda c: c.messages[-1].created_at if c.messages else c.created_at,
        reverse=True
    )
    return render_template("chat.html", user=user, conversations=convs, active_conv=conv)

@app.route("/chat/<int:conv_id>/send", methods=["POST"])
@login_required
def chat_send(conv_id):
    user = current_user()
    conv = Conversation.query.get_or_404(conv_id)
    if conv.buyer_id != user.id and conv.seller_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    body = request.form.get("body", "").strip()
    if not body:
        return jsonify({"error": "empty"}), 400
    msg = Message(conversation_id=conv.id, sender_id=user.id, body=body)
    db.session.add(msg)
    db.session.commit()
    return jsonify({"ok": True, "id": msg.id, "time": msg.created_at.strftime("%H:%M"),
                    "type": "text"})

@app.route("/chat/<int:conv_id>/messages")
@login_required
def chat_messages(conv_id):
    user = current_user()
    conv = Conversation.query.get_or_404(conv_id)
    if conv.buyer_id != user.id and conv.seller_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    after_id = request.args.get("after", 0, type=int)
    new_msgs = [m for m in conv.messages if m.id > after_id]
    for m in new_msgs:
        if m.sender_id != user.id and not m.is_read:
            m.is_read = True
    db.session.commit()
    out = []
    for m in new_msgs:
        entry = {"id": m.id, "body": m.body, "is_mine": m.sender_id == user.id,
                 "time": m.created_at.strftime("%H:%M"), "type": m.message_type}
        if m.message_type == "payment" and m.transaction:
            t = m.transaction
            entry["txn"] = {"id": t.id, "amount": t.amount,
                            "description": t.description, "status": t.status}
        if m.message_type == "quote_request" and m.quote_request_id:
            entry["quote_request_id"] = m.quote_request_id
        out.append(entry)
    return jsonify({"messages": out})


# ── payment ───────────────────────────────────────────────────────────────────

@app.route("/chat/<int:conv_id>/pay_request", methods=["POST"])
@login_required
def pay_request(conv_id):
    user = current_user()
    conv = Conversation.query.get_or_404(conv_id)
    if conv.seller_id != user.id:
        flash("Only the seller can request payment.")
        return redirect(url_for("chat_view", conv_id=conv_id))
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        amount = 0
    description = request.form.get("description", "").strip()
    if amount <= 0 or not description:
        flash("Please enter a valid amount and description.")
        return redirect(url_for("chat_view", conv_id=conv_id))

    txn = Transaction(
        conversation_id=conv.id,
        seller_id=conv.seller_id,
        buyer_id=conv.buyer_id,
        amount=amount,
        description=description,
    )
    db.session.add(txn)
    db.session.flush()

    body = f"Payment request: {description} — {amount} SEK"
    msg = Message(
        conversation_id=conv.id,
        sender_id=user.id,
        body=body,
        message_type="payment",
        transaction_id=txn.id,
    )
    db.session.add(msg)
    db.session.commit()
    log.info(f"PAY REQUEST  conv={conv_id}  txn={txn.id}  amount={amount}")
    return redirect(url_for("chat_view", conv_id=conv_id))

@app.route("/pay/<int:txn_id>")
@login_required
def pay_view(txn_id):
    user = current_user()
    txn = Transaction.query.get_or_404(txn_id)
    if txn.buyer_id != user.id:
        return redirect(url_for("chat_list"))
    if txn.status != "pending":
        return redirect(url_for("chat_view", conv_id=txn.conversation_id))
    return render_template("payment.html", user=user, txn=txn)

@app.route("/pay/<int:txn_id>/confirm", methods=["POST"])
@login_required
def pay_confirm(txn_id):
    user = current_user()
    txn = Transaction.query.get_or_404(txn_id)
    if txn.buyer_id != user.id or txn.status != "pending":
        return redirect(url_for("chat_list"))
    txn.status = "completed"
    db.session.commit()
    log.info(f"PAY CONFIRMED  txn={txn_id}  buyer={user.id}  amount={txn.amount}")
    return redirect(url_for("rate_view", txn_id=txn_id))


# ── rating ────────────────────────────────────────────────────────────────────

@app.route("/rate/<int:txn_id>")
@login_required
def rate_view(txn_id):
    user = current_user()
    txn = Transaction.query.get_or_404(txn_id)
    if txn.buyer_id != user.id or txn.status != "completed":
        return redirect(url_for("chat_list"))
    if txn.rated:
        flash("You have already rated this transaction.")
        return redirect(url_for("chat_view", conv_id=txn.conversation_id))
    return render_template("rate.html", user=user, txn=txn)

@app.route("/rate/<int:txn_id>", methods=["POST"])
@login_required
def rate_submit(txn_id):
    user = current_user()
    txn = Transaction.query.get_or_404(txn_id)
    if txn.buyer_id != user.id or txn.status != "completed" or txn.rated:
        return redirect(url_for("chat_list"))
    try:
        stars = int(request.form.get("rating", 0))
    except ValueError:
        stars = 0
    if not 1 <= stars <= 5:
        flash("Please select a rating between 1 and 5 stars.")
        return redirect(url_for("rate_view", txn_id=txn_id))

    txn.rating = stars
    txn.rated = True
    seller_profile = txn.seller.seller_profile
    seller_profile.recalculate_rating()
    db.session.commit()
    log.info(f"RATING  txn={txn_id}  seller={txn.seller_id}  stars={stars}  new_avg={seller_profile.avg_rating}")
    flash("Thanks for your rating!")
    return redirect(url_for("chat_view", conv_id=txn.conversation_id))


# ── quotes ────────────────────────────────────────────────────────────────────

@app.route("/quote/send", methods=["POST"])
@login_required
def quote_send():
    user = current_user()
    if user.role != "buyer":
        return redirect(url_for("dashboard"))
    raw_text = request.form.get("raw_text", "").strip()
    seller_ids = json.loads(request.form.get("seller_ids", "[]"))
    last = session.get("last_search", {})
    parsed = last.get("parsed", {})
    service = parsed.get("service", "")
    cities = parsed.get("cities", [])
    formatted = format_quote_request(raw_text, service, cities,
                                     parsed.get("price_max"), parsed.get("requested_day"))
    qr = QuoteRequest(buyer_id=user.id, service=service,
                      cities=json.dumps(cities), formatted_request=formatted)
    db.session.add(qr)
    db.session.flush()
    sent = 0
    for sid in seller_ids:
        seller = User.query.get(sid)
        if not seller:
            continue
        conv = Conversation.query.filter_by(buyer_id=user.id, seller_id=sid).first()
        if not conv:
            conv = Conversation(buyer_id=user.id, seller_id=sid)
            db.session.add(conv)
            db.session.flush()
        msg = Message(conversation_id=conv.id, sender_id=user.id,
                      body=formatted, message_type="quote_request",
                      quote_request_id=qr.id)
        db.session.add(msg)
        sent += 1
    db.session.commit()
    log.info(f"QUOTE_SEND  buyer={user.id}  service={service}  sellers={seller_ids}  qr_id={qr.id}")
    flash(f"Quote request sent to {sent} seller(s).")
    return redirect(url_for("dashboard") + "?tab=quotes")

@app.route("/quote/<int:qr_id>/respond", methods=["POST"])
@login_required
def quote_respond(qr_id):
    user = current_user()
    if user.role != "seller":
        return jsonify({"error": "forbidden"}), 403
    qr = QuoteRequest.query.get_or_404(qr_id)
    conv_id = request.form.get("conv_id", type=int)
    conv = Conversation.query.get_or_404(conv_id)
    if conv.seller_id != user.id:
        return jsonify({"error": "forbidden"}), 403
    raw_text = request.form.get("raw_text", "").strip()
    formatted, price = format_quote_response(raw_text, qr.formatted_request)
    qresp = QuoteResponse(quote_request_id=qr.id, seller_id=user.id,
                          formatted_response=formatted, price_offered=price)
    db.session.add(qresp)
    db.session.flush()
    msg = Message(conversation_id=conv.id, sender_id=user.id,
                  body=formatted, message_type="quote_response")
    db.session.add(msg)
    db.session.commit()
    log.info(f"QUOTE_RESPOND  seller={user.id}  qr_id={qr_id}  price={price}")
    return jsonify({"ok": True, "formatted": formatted,
                    "msg_id": msg.id, "time": msg.created_at.strftime("%H:%M")})

@app.route("/quote/<int:qr_id>/delete", methods=["POST"])
@login_required
def quote_delete(qr_id):
    user = current_user()
    qr = QuoteRequest.query.get_or_404(qr_id)
    if qr.buyer_id != user.id:
        return redirect(url_for("dashboard"))
    from sqlalchemy import text as _text
    db.session.execute(_text(
        "UPDATE message SET quote_request_id=NULL, message_type='text' WHERE quote_request_id=:id"
    ), {"id": qr_id})
    db.session.delete(qr)
    db.session.commit()
    log.info(f"QUOTE_DELETE  buyer={user.id}  qr_id={qr_id}")
    return redirect(url_for("dashboard") + "?tab=quotes")


# ── admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == os.environ.get("ADMIN_PASSWORD", "admin123"):
            session["admin"] = True
            log.info("ADMIN login success")
        else:
            log.warning("ADMIN login failed: wrong password")
            flash("Wrong password.")
    if not session.get("admin"):
        return render_template("admin_login.html")

    users    = User.query.order_by(User.created_at.desc()).all()
    searches = Search.query.order_by(Search.created_at.desc()).limit(50).all()
    services = get_existing_services()
    return render_template("admin.html", users=users, searches=searches, services=services)

@app.route("/admin/delete_user/<int:uid>", methods=["POST"])
def admin_delete_user(uid):
    if not session.get("admin"):
        return redirect(url_for("admin"))
    user = User.query.get_or_404(uid)
    log.info(f"ADMIN DELETE user  user_id={uid}  email={user.email}")
    for c in user.conversations_as_buyer + user.conversations_as_seller:
        db.session.delete(c)
    for t in user.transactions_as_buyer + user.transactions_as_seller:
        db.session.delete(t)
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    log.info("=== Birka starting ===")
    app.run(debug=True)
