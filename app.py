from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, stream_with_context
from models import db, User, SellerProfile, Search, Conversation, Message, Transaction, QuoteRequest, QuoteResponse, MultiServiceBundle, get_existing_services
import base64
from llm import parse_seller, parse_buyer, parse_buyer_multi, match_sellers, format_quote_request, format_quote_response, description_from_website, summarise_to_profile, extract_contact_info, analyze_photo_for_service
import json
import logging
from logging.handlers import RotatingFileHandler

_setup_queues: dict = {}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///marketplace.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    from sqlalchemy import text as _text
    with db.engine.connect() as _conn:
        for _stmt in [
            "ALTER TABLE message ADD COLUMN quote_request_id INTEGER",
            "ALTER TABLE seller_profile ADD COLUMN profile_description TEXT",
            "ALTER TABLE seller_profile ADD COLUMN website_url TEXT",
            "ALTER TABLE seller_profile ADD COLUMN website_pages_json TEXT",
            "ALTER TABLE seller_profile ADD COLUMN website_page_count INTEGER DEFAULT 0",
            "ALTER TABLE seller_profile ADD COLUMN website_scraped_at DATETIME",
            "ALTER TABLE seller_profile ADD COLUMN contact_email TEXT",
            "ALTER TABLE quote_request ADD COLUMN bundle_id INTEGER REFERENCES multi_service_bundle(id)",
        ]:
            try:
                _conn.execute(_text(_stmt))
                _conn.commit()
            except Exception:
                pass


# ── logging ──────────────────────────────────────────────────────────────────

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
    return db.session.get(User, uid) if uid else None

@app.context_processor
def inject_unread():
    user = current_user()
    if user:
        convs = user.conversations_as_buyer + user.conversations_as_seller
        count = sum(c.unread_count(user.id) for c in convs)
        return {'unread_count': count}
    return {'unread_count': 0}

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            log.info("login_required: no session, redirecting to login")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def _sort_results(results, sort):
    """Sort (seller, listing, flags) tuples by price or rating."""
    if sort == "price":
        return sorted(results, key=lambda x: x[1].get("price_min") or float("inf"))
    # default: rating descending (no-rating sellers go last)
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
        import queue as _qmod
        import threading as _threading
        role  = request.form["role"]
        email = request.form["email"].lower().strip()
        setup = request.form.get("setup", "manual")  # "manual" | "website"
        log.info(f"REGISTER attempt  role={role}  email={email}  setup={setup}")

        if User.query.filter_by(email=email).first():
            flash("Email already registered.")
            return redirect(url_for("register"))

        if role == "seller" and setup == "website":
            website_url = _normalise_url(request.form.get("website_url", "").strip())
            if not website_url:
                flash("Please enter a website URL.")
                return redirect(url_for("register"))

            user = User(email=email, role="seller", name="")
            user.set_password(request.form["password"])
            db.session.add(user)
            db.session.flush()
            db.session.add(SellerProfile(user_id=user.id))
            db.session.commit()
            session["user_id"] = user.id
            session["role"] = "seller"
            log.info(f"REGISTER website-setup  user_id={user.id}  url={website_url}")

            q: _qmod.Queue = _qmod.Queue()
            _setup_queues[user.id] = q

            uid = user.id
            def run_setup(uid=uid, url=website_url):
                with app.app_context():
                    _q = _setup_queues.get(uid)
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        from scrape import scrape_website, pages_to_text
                        result = scrape_website(url, on_event=lambda evt: _q.put(evt))
                        if not result["pages"]:
                            _q.put({"type": "error", "message": "Could not read any pages from that URL."})
                            return
                        _prof = SellerProfile.query.filter_by(user_id=uid).first()
                        _prof.website_url = url
                        _prof.website_pages_json = json.dumps(result["pages"])
                        _prof.website_page_count = len(result["pages"])
                        _prof.website_scraped_at = _dt.now(_tz.utc).replace(tzinfo=None)
                        db.session.commit()
                        _q.put({"type": "analysing", "pages": len(result["pages"])})
                        text_pages = pages_to_text(result["pages"])
                        total_text = sum(len(p.get("text", "")) for p in text_pages)
                        log.info(f"SETUP pages_to_text: {len(text_pages)}/{len(result['pages'])} pages kept, {total_text} chars")
                        if not text_pages:
                            log.error(f"SETUP: pages_to_text returned empty — all content filtered/deduplicated")
                            _q.put({"type": "error", "message": "Could not extract text from scraped pages."})
                            return
                        try:
                            desc = "".join(description_from_website(text_pages))
                        except Exception as api_err:
                            log.error(f"SETUP: description_from_website exception: {api_err}")
                            _q.put({"type": "error", "message": f"AI analysis failed: {api_err}"})
                            return
                        log.info(f"SETUP: description generated, {len(desc)} chars")
                        if not desc:
                            log.error(f"SETUP: description empty — API returned nothing")
                            _q.put({"type": "error", "message": "AI returned an empty description. Try again."})
                            return
                        _q.put({"type": "parsing"})
                        _user = db.session.get(User, uid)
                        parsed = parse_seller(desc, "", get_existing_services())
                        _prof.raw_description = desc
                        cities = [c for c in (parsed.get("cities") or []) if c]
                        _prof.set_cities(cities)
                        if cities:
                            _prof.city = cities[0]
                        _prof.set_listings(parsed.get("listings", []))
                        _prof.profile_description = summarise_to_profile(desc)
                        contact = extract_contact_info(desc)
                        if contact.get("name"):
                            _user.name = contact["name"]
                        if contact.get("phone"):
                            _user.phone = contact["phone"]
                        if contact.get("email"):
                            _prof.contact_email = contact["email"].lower().strip()
                        db.session.commit()
                        _q.put({"type": "done"})
                    except Exception as e:
                        log.error(f"SETUP error uid={uid}: {e}")
                        if _q:
                            _q.put({"type": "error", "message": "Something went wrong during setup."})
                    finally:
                        if _q:
                            _q.put(None)
                        _setup_queues.pop(uid, None)

            _threading.Thread(target=run_setup, daemon=True).start()
            return redirect(url_for("register_loading"))

        else:
            # Manual path (buyers + manual sellers)
            name  = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            city  = request.form.get("city", "").strip()
            user = User(email=email, role=role, name=name, phone=phone, city=city)
            user.set_password(request.form["password"])
            db.session.add(user)
            db.session.flush()
            if role == "seller":
                db.session.add(SellerProfile(user_id=user.id))
            db.session.commit()
            session["user_id"] = user.id
            session["role"] = role
            log.info(f"REGISTER manual success  user_id={user.id}  role={role}")
            return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/register/loading")
@login_required
def register_loading():
    return render_template("register_loading.html")


@app.route("/register/progress")
@login_required
def register_progress():
    import queue as _qmod
    user = current_user()
    uid = user.id

    def generate():
        q = _setup_queues.get(uid)
        if q is None:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        while True:
            try:
                item = q.get(timeout=180)
            except _qmod.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Setup timed out.'})}\n\n"
                return
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(request.form["password"]):
            session["user_id"] = user.id
            session["role"] = user.role
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

@app.route("/dashboard", methods=["GET", "POST"])  # POST kept for buyer search
@login_required
def dashboard():
    user = current_user()
    log.info(f"DASHBOARD  user_id={user.id}  role={user.role}  method={request.method}")

    if user.role == "seller":
        return render_template("dashboard_seller.html", user=user)

    # buyer
    if request.method == "POST":
        query = request.form["query"].strip()
        multi = parse_buyer_multi(query, user.city or "", get_existing_services())
        services = [s for s in (multi.get("services") or []) if s.get("service")]
        cities = multi.get("cities") or ([user.city] if user.city else [])
        unrecog = multi.get("unrecognized_cities") or []

        search = Search(
            buyer_id=user.id,
            raw_query=query,
            mapped_service=services[0]["service"] if services else None,
            city=cities[0] if cities else None,
            price_max=services[0].get("price_max") if services else None,
        )
        db.session.add(search)
        db.session.commit()

        sellers_all = User.query.filter_by(role="seller").join(SellerProfile).all()

        if len(services) > 1:
            # ── multi-service path ────────────────────────────────────────
            svc_results = []
            for svc in services:
                svc_parsed = {"service": svc["service"], "cities": cities,
                              "price_max": svc.get("price_max"),
                              "requested_day": svc.get("requested_day"),
                              "unrecognized_cities": unrecog}
                matched = match_sellers(svc_parsed, sellers_all)
                # collect ALL matched seller IDs for this service
                quote_sids, seen_q = [], set()
                for s, l, _ in ([item for r in matched["by_city"].values() for item in r]
                                 + matched["other"]):
                    if s.id not in seen_q:
                        quote_sids.append(s.id)
                        seen_q.add(s.id)
                svc_results.append({
                    "service": svc["service"],
                    "price_max": svc.get("price_max"),
                    "requested_day": svc.get("requested_day"),
                    "quote_seller_ids": quote_sids,
                    "by_city": {c: [(s.id, l, f) for s, l, f in r]
                                for c, r in matched["by_city"].items()},
                    "other": [(s.id, l, f) for s, l, f in matched["other"]],
                })
            session["last_multi_search"] = {
                "raw_query": query, "cities": cities, "unrecognized_cities": unrecog,
                "services": svc_results,
            }
            session.pop("last_search", None)
        else:
            # ── single-service path (existing behaviour) ──────────────────
            session.pop("last_multi_search", None)
            parsed = {
                "service": services[0]["service"] if services else None,
                "cities": cities,
                "price_max": services[0].get("price_max") if services else None,
                "requested_day": services[0].get("requested_day") if services else None,
                "unrecognized_cities": unrecog,
            }
            matched = match_sellers(parsed, sellers_all)
            session["last_search"] = {
                "parsed": {**parsed, "_raw_query": query},
                "by_city": {c: [(s.id, l, f) for s, l, f in r]
                            for c, r in matched["by_city"].items()},
                "other": [(s.id, l, f) for s, l, f in matched["other"]],
            }
        return redirect(url_for("dashboard"))

    # GET — retrieve from session (keep until next search)
    by_city = {}
    other_results = []
    search_data = {}
    quote_seller_ids = []
    sort = request.args.get("sort", "rating")
    last = session.get("last_search")
    if last:
        search_data = last["parsed"]
        by_city = {
            city: [(db.session.get(User, i), l, f) for i, l, f in results if db.session.get(User, i)]
            for city, results in last.get("by_city", {}).items()
        }
        other_results = [(db.session.get(User, i), l, f) for i, l, f in last.get("other", []) if db.session.get(User, i)]
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
                      .filter_by(buyer_id=user.id, bundle_id=None)
                      .order_by(QuoteRequest.created_at.desc())
                      .all())

    multi_bundles = (MultiServiceBundle.query
                     .filter_by(buyer_id=user.id)
                     .order_by(MultiServiceBundle.created_at.desc())
                     .all())

    # Reconstruct multi-service search results from session
    multi_search = {}
    multi_services_data = []
    last_multi = session.get("last_multi_search")
    if last_multi:
        multi_search = last_multi
        for svc in last_multi.get("services", []):
            by_city_r = {
                c: [(db.session.get(User, i), l, f) for i, l, f in rows if db.session.get(User, i)]
                for c, rows in svc.get("by_city", {}).items()
            }
            other_r = [(db.session.get(User, i), l, f)
                       for i, l, f in svc.get("other", []) if db.session.get(User, i)]
            total = sum(len(r) for r in by_city_r.values())
            multi_services_data.append({
                "service": svc["service"],
                "price_max": svc.get("price_max"),
                "requested_day": svc.get("requested_day"),
                "quote_seller_ids": svc.get("quote_seller_ids", []),
                "by_city": by_city_r,
                "other": other_r,
                "total": total,
            })

    # Slim JSON-serializable version for the JS modal (no User objects)
    multi_services_json = [
        {"service": s["service"], "price_max": s.get("price_max"),
         "requested_day": s.get("requested_day"),
         "quote_seller_ids": s.get("quote_seller_ids", [])}
        for s in multi_services_data
    ]

    return render_template("dashboard_buyer.html", user=user,
                           by_city=by_city,
                           all_local=all_local,
                           other_results=other_results,
                           search_data=search_data,
                           sort=sort,
                           quote_seller_ids=quote_seller_ids,
                           quote_requests=quote_requests,
                           multi_bundles=multi_bundles,
                           multi_search=multi_search,
                           multi_services_data=multi_services_data,
                           multi_services_json=multi_services_json)


# ── seller public profile ─────────────────────────────────────────────────────

@app.route("/seller/<int:seller_id>")
@login_required
def seller_profile(seller_id):
    seller = User.query.get_or_404(seller_id)
    if seller.role != "seller":
        return redirect(url_for("dashboard"))
    return render_template("seller_profile.html", seller=seller)


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


# ── settings ──────────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    if user.role != "seller":
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        action = request.form.get("action", "analyse")

        if action == "analyse":
            desc = request.form.get("description", "").strip()
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
            profile.profile_description = summarise_to_profile(desc)
            contact = extract_contact_info(desc)
            if contact.get("name"):
                user.name = contact["name"]
            if contact.get("phone"):
                user.phone = contact["phone"]
            if contact.get("email"):
                profile.contact_email = contact["email"].lower().strip()
            db.session.commit()
            unrecognized = parsed.get("unrecognized_cities") or []
            if unrecognized:
                flash(f"Profile updated — but we couldn't recognise: {', '.join(unrecognized)}.")
            else:
                flash("Profile updated.")

        return redirect(url_for("settings"))

    return render_template("settings.html", user=user)


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
    """Seller sends a payment request into the chat."""
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
    log.info(f"PAY REQUEST  conv={conv_id}  txn={txn.id}  amount={amount}  desc={description}")
    return redirect(url_for("chat_view", conv_id=conv_id))


@app.route("/pay/<int:txn_id>")
@login_required
def pay_view(txn_id):
    """Dummy payment confirmation page shown to buyer."""
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
    """Buyer confirms the dummy payment."""
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
    if user.seller_profile:
        db.session.delete(user.seller_profile)
    for s in user.searches:
        db.session.delete(s)
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
        seller = db.session.get(User, sid)
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


# ── multi-service bundle ──────────────────────────────────────────────────────

@app.route("/multi_quote/send", methods=["POST"])
@login_required
def multi_quote_send():
    user = current_user()
    if user.role != "buyer":
        return redirect(url_for("dashboard"))
    raw_text = request.form.get("raw_text", "").strip()
    try:
        services = json.loads(request.form.get("services_json", "[]"))
    except Exception:
        services = []
    last = session.get("last_multi_search", {})
    cities = last.get("cities", [])
    if not services:
        flash("No services to send.")
        return redirect(url_for("dashboard"))

    bundle = MultiServiceBundle(buyer_id=user.id, raw_query=raw_text)
    db.session.add(bundle)
    db.session.flush()

    total_sent = 0
    for svc_data in services:
        service = svc_data.get("service", "")
        seller_ids = svc_data.get("quote_seller_ids", [])
        formatted = format_quote_request(raw_text, service, cities,
                                         svc_data.get("price_max"),
                                         svc_data.get("requested_day"))
        qr = QuoteRequest(buyer_id=user.id, service=service,
                          cities=json.dumps(cities),
                          formatted_request=formatted,
                          bundle_id=bundle.id)
        db.session.add(qr)
        db.session.flush()
        for sid in seller_ids:
            seller = db.session.get(User, sid)
            if not seller or not seller.seller_profile:
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
            total_sent += 1
            # Auto-respond for fixed-price sellers
            listing = next((l for l in seller.seller_profile.get_listings()
                            if l.get("service", "").lower() == service.lower()), None)
            if listing and not listing.get("is_quote") and listing.get("price_min"):
                price = listing["price_min"]
                price_max = listing.get("price_max")
                if price_max and price_max != price:
                    auto_body = f"Fixed price: {price}–{price_max} SEK"
                else:
                    auto_body = f"Fixed price: {price} SEK"
                db.session.add(QuoteResponse(
                    quote_request_id=qr.id, seller_id=sid,
                    formatted_response=auto_body, price_offered=price))
                db.session.add(Message(
                    conversation_id=conv.id, sender_id=sid,
                    body=auto_body, message_type="quote_response"))

    db.session.commit()
    log.info(f"MULTI_QUOTE_SEND  buyer={user.id}  bundle={bundle.id}  "
             f"services={[s['service'] for s in services]}  sent={total_sent}")
    flash(f"Bundle sent — {total_sent} message(s) across {len(services)} services.")
    return redirect(url_for("dashboard") + "?tab=quotes")


@app.route("/multi_bundle/delete/<int:bid>", methods=["POST"])
@login_required
def multi_bundle_delete(bid):
    user = current_user()
    bundle = MultiServiceBundle.query.get_or_404(bid)
    if bundle.buyer_id != user.id:
        return redirect(url_for("dashboard"))
    from sqlalchemy import text as _t
    for qr in bundle.quote_requests:
        db.session.execute(_t(
            "UPDATE message SET quote_request_id=NULL, message_type='text' WHERE quote_request_id=:id"
        ), {"id": qr.id})
        db.session.delete(qr)
    db.session.delete(bundle)
    db.session.commit()
    log.info(f"MULTI_BUNDLE_DELETE  buyer={user.id}  bundle={bid}")
    return redirect(url_for("dashboard") + "?tab=quotes")


# ── website import ───────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


@app.route("/scrape")
@login_required
def scrape_url():
    from datetime import datetime as _dt, timezone as _tz
    import queue as _queue
    import threading

    user = current_user()
    if user.role != "seller":
        return jsonify({"error": "Only sellers can use this feature."}), 403
    url = _normalise_url(request.args.get("url", "").strip())
    if not url:
        return jsonify({"error": "No URL provided."}), 400

    log.info(f"SCRAPE start  user_id={user.id}  url={url}")

    profile_id = user.seller_profile.id
    progress_q: _queue.Queue = _queue.Queue()

    def run_fresh():
        try:
            from scrape import scrape_website

            result = scrape_website(url, on_event=lambda evt: progress_q.put(evt))

            if not result["pages"]:
                progress_q.put({"type": "error",
                                "message": "Could not read any content from that URL."})
                return

            with app.app_context():
                p = SellerProfile.query.get(profile_id)
                p.website_url = url
                p.website_pages_json = json.dumps(result["pages"])
                p.website_page_count = len(result["pages"])
                p.website_scraped_at = _dt.now(_tz.utc).replace(tzinfo=None)
                db.session.commit()
                log.info(f"SCRAPE saved  profile={profile_id}  pages={len(result['pages'])}  url={url}")

            progress_q.put({"type": "done", "pages_saved": len(result["pages"])})
        except Exception as e:
            log.error(f"SCRAPE error  user_id={user.id}  url={url}  error={e}")
            progress_q.put({"type": "error",
                            "message": "Something went wrong while reading the website."})
        finally:
            progress_q.put(None)

    threading.Thread(target=run_fresh, daemon=True).start()

    def generate():
        while True:
            item = progress_q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/website/describe")
@login_required
def website_describe():
    user = current_user()
    if user.role != "seller":
        return jsonify({"error": "Only sellers can use this feature."}), 403

    profile = user.seller_profile
    if not profile.website_pages_json:
        return jsonify({"error": "No stored pages. Scrape a website first."}), 400

    pages = json.loads(profile.website_pages_json)

    def generate():
        try:
            from scrape import pages_to_text
            text_pages = pages_to_text(pages)
            total_text = sum(len(p.get("text", "")) for p in text_pages)
            log.info(f"DESCRIBE pages_to_text: {len(text_pages)}/{len(pages)} pages kept, {total_text} chars  user={user.id}")
            if not text_pages:
                log.error(f"DESCRIBE: pages_to_text empty  user={user.id}")
                yield f"data: {json.dumps({'type': 'error', 'message': 'Could not extract text from stored pages.'})}\n\n"
                return
            chunk_count = 0
            for chunk in description_from_website(text_pages):
                chunk_count += 1
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"
            log.info(f"DESCRIBE done: {chunk_count} chunks  user={user.id}")
            if chunk_count == 0:
                log.error(f"DESCRIBE: API returned 0 chunks  user={user.id}")
                yield f"data: {json.dumps({'type': 'error', 'message': 'AI returned an empty description. Try again.'})}\n\n"
                return
            yield f"data: {json.dumps({'type': 'done', 'pages_read': len(pages)})}\n\n"
        except Exception as e:
            log.error(f"WEBSITE DESCRIBE error  user_id={user.id}  error={e}")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Something went wrong.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/website/delete", methods=["POST"])
@login_required
def website_delete():
    user = current_user()
    if user.role != "seller":
        return redirect(url_for("dashboard"))
    p = user.seller_profile
    p.website_url = None
    p.website_pages_json = None
    p.website_page_count = 0
    p.website_scraped_at = None
    db.session.commit()
    log.info(f"WEBSITE DELETE  user_id={user.id}")
    flash("Stored website data deleted.")
    return redirect(url_for("dashboard"))


_PHOTO_MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}
_MAX_PHOTO_BYTES = 5 * 1024 * 1024

@app.route("/search/photo", methods=["POST"])
@login_required
def search_photo():
    user = current_user()
    if user.role != "buyer":
        return jsonify({"error": "buyers only"}), 403
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    media_type = _PHOTO_MEDIA_TYPES.get(ext)
    if not media_type:
        return jsonify({"error": "unsupported file type"}), 400
    data = file.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        return jsonify({"error": "file too large (max 5 MB)"}), 400
    image_b64 = base64.b64encode(data).decode("utf-8")
    uploads_dir = os.path.join(app.static_folder, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    photo_filename = f"search_{user.id}.{ext}"
    with open(os.path.join(uploads_dir, photo_filename), "wb") as f:
        f.write(data)
    photo_url = f"/static/uploads/{photo_filename}"
    analysis = analyze_photo_for_service(image_b64, media_type, user.city or "", get_existing_services())
    log.info(f"PHOTO_SEARCH  user={user.id}  service={analysis.get('service')}")
    if not analysis.get("service"):
        return jsonify({"ok": False, "description": analysis.get("description", "Could not identify a service from this photo.")})
    parsed = {
        "service": analysis["service"],
        "cities": analysis.get("cities") or ([user.city] if user.city else []),
        "price_max": None, "requested_day": None, "unrecognized_cities": [],
        "_raw_query": "", "_photo_description": analysis.get("description", ""),
        "_photo_url": photo_url,
    }
    db.session.add(Search(
        buyer_id=user.id,
        raw_query=f"[photo] {analysis.get('description', '')}",
        mapped_service=parsed["service"],
        city=parsed["cities"][0] if parsed["cities"] else None,
    ))
    db.session.commit()
    sellers = User.query.filter_by(role="seller").join(SellerProfile).all()
    matched = match_sellers(parsed, sellers)
    session["last_search"] = {
        "parsed": parsed,
        "by_city": {c: [(s.id, l, f) for s, l, f in r] for c, r in matched["by_city"].items()},
        "other": [(s.id, l, f) for s, l, f in matched["other"]],
    }
    session.pop("last_multi_search", None)
    return jsonify({"ok": True, "service": analysis["service"], "description": analysis.get("description", "")})


if __name__ == "__main__":
    log.info("=== Birka starting on port 5002 ===")
    app.run(debug=True, port=5002)
