"""Seed script: clears the database and creates 25 test sellers + 1 buyer."""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("SECRET_KEY", "seed-secret")

from app import app, db
from models import User, SellerProfile

SELLERS = [
    # ── PLUMBERS ────────────────────────────────────────────────────────────
    dict(name="Erik Rörnäs",         email="erik.rornas@example.com",       city="Lund",  service="plumber",         is_quote=True,  price=None, avg=4.6, cnt=28, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
    dict(name="Anna VVS Service",    email="anna.vvs@example.com",          city="Lund",  service="plumber",         is_quote=True,  price=None, avg=2.4, cnt=15, avail=["monday","tuesday","wednesday","thursday","friday"]),
    dict(name="Björn Sanitär AB",    email="bjorn.sanitar@example.com",     city="Lund",  service="plumber",         is_quote=True,  price=None, avg=4.1, cnt=34, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Sara Rörservice",     email="sara.ror@example.com",          city="Lund",  service="plumber",         is_quote=False, price=500,  avg=3.8, cnt=22, avail=["monday","wednesday","thursday","friday","saturday"]),
    dict(name="Malmö Rör & Värme",   email="malmo.ror@example.com",         city="Malmö", service="plumber",         is_quote=False, price=600,  avg=4.3, cnt=18, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),

    # ── ELECTRICIANS ────────────────────────────────────────────────────────
    dict(name="Lunds Elektriker AB", email="lunds.elektriker@example.com",  city="Lund",  service="electrician",     is_quote=True,  price=None, avg=4.7, cnt=38, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Sven El",             email="sven.el@example.com",           city="Lund",  service="electrician",     is_quote=True,  price=None, avg=2.2, cnt=12, avail=["monday","tuesday","wednesday","thursday","friday"]),
    dict(name="ElFix Lund",          email="elfix.lund@example.com",        city="Lund",  service="electrician",     is_quote=True,  price=None, avg=3.9, cnt=25, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
    dict(name="Petra Elservice",     email="petra.el@example.com",          city="Lund",  service="electrician",     is_quote=False, price=400,  avg=4.5, cnt=31, avail=["monday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Malmö Elektriska",    email="malmo.el@example.com",          city="Malmö", service="electrician",     is_quote=False, price=500,  avg=3.1, cnt=19, avail=["monday","tuesday","wednesday","thursday","friday"]),

    # ── PAINTERS ────────────────────────────────────────────────────────────
    dict(name="Lars Måleri",         email="lars.maleri@example.com",       city="Lund",  service="painter",         is_quote=True,  price=None, avg=4.8, cnt=40, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Ingrid Färg & Form",  email="ingrid.farg@example.com",       city="Lund",  service="painter",         is_quote=True,  price=None, avg=2.0, cnt=11, avail=["monday","tuesday","wednesday","thursday","friday"]),
    dict(name="Skånska Målare",      email="skanska.malare@example.com",    city="Lund",  service="painter",         is_quote=True,  price=None, avg=4.2, cnt=29, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
    dict(name="David Pensel",        email="david.pensel@example.com",      city="Lund",  service="painter",         is_quote=False, price=400,  avg=3.6, cnt=17, avail=["monday","wednesday","thursday","friday"]),
    dict(name="Malmö Måleri",        email="malmo.maleri@example.com",      city="Malmö", service="painter",         is_quote=False, price=600,  avg=4.4, cnt=23, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),

    # ── CARPENTERS ──────────────────────────────────────────────────────────
    dict(name="Johan Snickeri",      email="johan.snickeri@example.com",    city="Lund",  service="carpenter",       is_quote=True,  price=None, avg=4.4, cnt=33, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
    dict(name="Maria Trähantverk",   email="maria.tra@example.com",         city="Lund",  service="carpenter",       is_quote=True,  price=None, avg=1.9, cnt=10, avail=["monday","tuesday","wednesday","thursday","friday"]),
    dict(name="LundBygg Snickeri",   email="lundbygg@example.com",          city="Lund",  service="carpenter",       is_quote=True,  price=None, avg=4.0, cnt=27, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Klas Träarbeten",     email="klas.tra@example.com",          city="Lund",  service="carpenter",       is_quote=False, price=500,  avg=3.5, cnt=20, avail=["monday","wednesday","thursday","friday","saturday"]),
    dict(name="Malmö Snickeri",      email="malmo.snickeri@example.com",    city="Malmö", service="carpenter",       is_quote=False, price=600,  avg=4.6, cnt=36, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),

    # ── APPLIANCE REPAIR ────────────────────────────────────────────────────
    dict(name="Vitvaror Lund",       email="vitvaror.lund@example.com",     city="Lund",  service="appliance repair",is_quote=True,  price=None, avg=4.5, cnt=32, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
    dict(name="Stig Hushållsservice",email="stig.hush@example.com",         city="Lund",  service="appliance repair",is_quote=True,  price=None, avg=2.3, cnt=13, avail=["monday","tuesday","wednesday","thursday","friday"]),
    dict(name="Apparatfixarn",       email="apparatfixarn@example.com",     city="Lund",  service="appliance repair",is_quote=True,  price=None, avg=3.8, cnt=24, avail=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Hanna Vitvaruservice",email="hanna.vitvaror@example.com",    city="Lund",  service="appliance repair",is_quote=False, price=400,  avg=4.2, cnt=21, avail=["monday","wednesday","thursday","friday","saturday","sunday"]),
    dict(name="Malmö Vitvaror",      email="malmo.vitvaror@example.com",    city="Malmö", service="appliance repair",is_quote=False, price=500,  avg=3.4, cnt=16, avail=["monday","tuesday","wednesday","thursday","friday","saturday"]),
]

DESCRIPTIONS = {
    "plumber":         "{name} offers professional plumbing services in {city}. Available for installations, repairs, and emergency callouts.",
    "electrician":     "{name} is a certified electrician based in {city}, handling everything from rewiring to new installations.",
    "painter":         "{name} provides interior and exterior painting services in {city}. All surfaces and finishes.",
    "carpenter":       "{name} offers custom carpentry and woodwork in {city} — kitchens, built-ins, furniture, and renovations.",
    "appliance repair":"{name} repairs all major household appliances in {city}: washing machines, dishwashers, fridges, and ovens.",
}

with app.app_context():
    # ── Clear all data ───────────────────────────────────────────────────────
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()
    print("Database cleared.")

    # ── Buyer ────────────────────────────────────────────────────────────────
    buyer = User(email="buyer@test.com", role="buyer", name="Test Buyer", city="Lund")
    buyer.set_password("a")
    db.session.add(buyer)
    db.session.flush()
    print(f"  buyer:  {buyer.email}")

    # ── Sellers ──────────────────────────────────────────────────────────────
    for s in SELLERS:
        user = User(email=s["email"], role="seller", name=s["name"], city=s["city"])
        user.set_password("a")
        db.session.add(user)
        db.session.flush()

        listing = {
            "service": s["service"],
            "availability_days": s["avail"],
            "price_min": s["price"],
            "price_max": s["price"],
            "is_quote": s["is_quote"],
        }
        desc = DESCRIPTIONS[s["service"]].format(name=s["name"], city=s["city"])
        price_str = f"{s['price']} SEK/h" if s["price"] else "quote on request"
        profile_desc = f"{desc} Pricing: {price_str}."

        profile = SellerProfile(
            user_id=user.id,
            listings=json.dumps([listing]),
            cities=json.dumps([s["city"]]),
            city=s["city"],
            avg_rating=s["avg"],
            rating_count=s["cnt"],
            profile_description=profile_desc,
            raw_description=profile_desc,
        )
        db.session.add(profile)
        price_label = "quote" if s["is_quote"] else f"{s['price']} SEK/h"
        print(f"  seller: {s['name']:<26} {s['city']:<6} {s['service']:<16} {price_label:<14} ★{s['avg']} ({s['cnt']}r)")

    db.session.commit()
    print(f"\nDone — 1 buyer + {len(SELLERS)} sellers. All passwords: 'a'")
