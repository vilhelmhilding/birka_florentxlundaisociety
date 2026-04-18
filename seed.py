"""
Seed the existing database with mock data:
  - 20 sellers across 10 service categories
  - 5 buyers
  - Realistic ratings backed by Transaction records
Skips any email that already exists — safe to run multiple times.
"""

from dotenv import load_dotenv
import os, random, json
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from app import app, db
from models import User, SellerProfile, Conversation, Transaction

SELLERS = [
    # ── Painters ─────────────────────────────────────────────────────
    {
        "name": "Erik Lindström",
        "email": "erik.lindstrom@example.com",
        "phone": "070-123 45 67",
        "city": "Stockholm",
        "listings": [{"service": "painter", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday"], "price_min": 3000, "price_max": 8000}],
        "ratings": [5, 5, 4, 5, 4],
    },
    {
        "name": "Sofia Bergman",
        "email": "sofia.bergman@example.com",
        "phone": "073-987 65 43",
        "city": "Göteborg",
        "listings": [{"service": "painter", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 2500, "price_max": 6000}],
        "ratings": [4, 3, 4],
    },
    # ── Electricians ─────────────────────────────────────────────────
    {
        "name": "Anders Johansson",
        "email": "anders.el@example.com",
        "phone": "076-111 22 33",
        "city": "Malmö",
        "listings": [{"service": "electrician", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 1500, "price_max": 6000}],
        "ratings": [5, 5, 5, 4, 5],
    },
    {
        "name": "Maria Nilsson",
        "email": "maria.el@example.com",
        "phone": "072-444 55 66",
        "city": "Uppsala",
        "listings": [{"service": "electrician", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 1800, "price_max": 5000}],
        "ratings": [3, 4, 3, 4],
    },
    # ── Plumbers ─────────────────────────────────────────────────────
    {
        "name": "Karl Svensson",
        "email": "karl.ror@example.com",
        "phone": "070-777 88 99",
        "city": "Stockholm",
        "listings": [{"service": "plumber", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 1200, "price_max": 9000}],
        "ratings": [4, 5, 4, 4, 3, 5],
    },
    {
        "name": "Lena Persson",
        "email": "lena.ror@example.com",
        "phone": "073-321 65 43",
        "city": "Linköping",
        "listings": [{"service": "plumber", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday"], "price_min": 1500, "price_max": 7000}],
        "ratings": [5, 5, 4],
    },
    # ── Cleaners ─────────────────────────────────────────────────────
    {
        "name": "Fatima Al-Hassan",
        "email": "fatima.stad@example.com",
        "phone": "076-222 33 44",
        "city": "Stockholm",
        "listings": [{"service": "cleaning", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 350, "price_max": 700}],
        "ratings": [5, 4, 5, 5, 4, 4, 5],
    },
    {
        "name": "Jonas Ek",
        "email": "jonas.stad@example.com",
        "phone": "072-555 66 77",
        "city": "Västerås",
        "listings": [{"service": "cleaning", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 400, "price_max": 600}],
        "ratings": [3, 4, 3],
    },
    # ── Carpenters ───────────────────────────────────────────────────
    {
        "name": "Mikael Gustafsson",
        "email": "mikael.snickare@example.com",
        "phone": "070-888 99 00",
        "city": "Göteborg",
        "listings": [{"service": "carpenter", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday"], "price_min": 500, "price_max": 1200}],
        "ratings": [4, 5, 4, 5],
    },
    {
        "name": "Anna Larsson",
        "email": "anna.snickare@example.com",
        "phone": "073-101 11 12",
        "city": "Örebro",
        "listings": [{"service": "carpenter", "availability_days": ["tuesday","wednesday","thursday","friday","saturday"], "price_min": 600, "price_max": 1000}],
        "ratings": [],
    },
    # ── Gardeners ────────────────────────────────────────────────────
    {
        "name": "Peter Magnusson",
        "email": "peter.trad@example.com",
        "phone": "076-131 41 51",
        "city": "Lund",
        "listings": [{"service": "gardening", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 400, "price_max": 800}],
        "ratings": [4, 4, 5, 4],
    },
    {
        "name": "Helena Strand",
        "email": "helena.trad@example.com",
        "phone": "072-161 71 81",
        "city": "Helsingborg",
        "listings": [{"service": "gardening", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 350, "price_max": 700}],
        "ratings": [2, 3, 2],
    },
    # ── Moving ───────────────────────────────────────────────────────
    {
        "name": "Oscar Lindqvist",
        "email": "oscar.flytt@example.com",
        "phone": "070-191 20 21",
        "city": "Stockholm",
        "listings": [{"service": "moving", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 600, "price_max": 900}],
        "ratings": [5, 4, 5, 3, 4],
    },
    {
        "name": "Britta Holm",
        "email": "britta.flytt@example.com",
        "phone": "073-222 32 42",
        "city": "Norrköping",
        "listings": [{"service": "moving", "availability_days": ["friday","saturday","sunday"], "price_min": 500, "price_max": 800}],
        "ratings": [3, 3, 4],
    },
    # ── Personal trainers ────────────────────────────────────────────
    {
        "name": "Victor Håkansson",
        "email": "victor.pt@example.com",
        "phone": "076-252 62 72",
        "city": "Stockholm",
        "listings": [{"service": "personal trainer", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 700, "price_max": 1200}],
        "ratings": [5, 5, 5, 4, 5],
    },
    {
        "name": "Emma Sjöberg",
        "email": "emma.pt@example.com",
        "phone": "072-282 92 02",
        "city": "Malmö",
        "listings": [{"service": "personal trainer", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 600, "price_max": 1000}],
        "ratings": [4, 4, 5, 3],
    },
    # ── IT support ───────────────────────────────────────────────────
    {
        "name": "Daniel Åberg",
        "email": "daniel.it@example.com",
        "phone": "070-303 13 23",
        "city": "Umeå",
        "listings": [{"service": "IT support", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday"], "price_min": 600, "price_max": 1000}],
        "ratings": [4, 3, 4, 4],
    },
    {
        "name": "Camilla Nord",
        "email": "camilla.it@example.com",
        "phone": "073-333 43 53",
        "city": "Jönköping",
        "listings": [{"service": "IT support", "availability_days": ["monday","tuesday","wednesday","thursday","friday"], "price_min": 500, "price_max": 800}],
        "ratings": [],
    },
    # ── Dog walkers ──────────────────────────────────────────────────
    {
        "name": "Maja Friberg",
        "email": "maja.hund@example.com",
        "phone": "076-363 73 83",
        "city": "Göteborg",
        "listings": [{"service": "dog walker", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 200, "price_max": 350}],
        "ratings": [5, 5, 4, 5, 5, 4, 5],
    },
    {
        "name": "Tobias Engström",
        "email": "tobias.hund@example.com",
        "phone": "072-393 03 13",
        "city": "Stockholm",
        "listings": [{"service": "dog walker", "availability_days": ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"], "price_min": 250, "price_max": 400}],
        "ratings": [4, 3, 4, 4],
    },
]

BUYERS = [
    {"name": "Johanna Lindberg", "email": "johanna@example.com",  "city": "Stockholm", "phone": "070-400 10 20"},
    {"name": "Rasmus Thorén",    "email": "rasmus@example.com",   "city": "Göteborg",  "phone": "073-400 30 40"},
    {"name": "Isabella Wijk",    "email": "isabella@example.com", "city": "Malmö",     "phone": "076-400 50 60"},
    {"name": "Hugo Sandström",   "email": "hugo@example.com",     "city": "Uppsala",   "phone": "072-400 70 80"},
    {"name": "Alicia Blom",      "email": "alicia@example.com",   "city": "Stockholm", "phone": "070-400 90 00"},
]

with app.app_context():

    # Buyers
    buyer_objs = []
    for b in BUYERS:
        u = User.query.filter_by(email=b["email"]).first()
        if u:
            buyer_objs.append(u)
        else:
            u = User(email=b["email"], role="buyer", name=b["name"],
                     phone=b["phone"], city=b["city"])
            u.set_password("password123")
            db.session.add(u)
            db.session.flush()
            buyer_objs.append(u)
            print(f"  + buyer  {b['name']}")

    # Sellers
    added = 0
    for s in SELLERS:
        if User.query.filter_by(email=s["email"]).first():
            continue

        u = User(email=s["email"], role="seller", name=s["name"],
                 phone=s["phone"], city=s["city"])
        u.set_password("password123")
        db.session.add(u)
        db.session.flush()

        profile = SellerProfile(
            user_id=u.id,
            city=s["city"],
            listings=json.dumps(s["listings"]),
        )
        db.session.add(profile)
        db.session.flush()

        for i, stars in enumerate(s.get("ratings", [])):
            buyer = buyer_objs[i % len(buyer_objs)]
            conv = Conversation(buyer_id=buyer.id, seller_id=u.id)
            db.session.add(conv)
            db.session.flush()
            txn = Transaction(
                conversation_id=conv.id,
                seller_id=u.id,
                buyer_id=buyer.id,
                amount=random.choice([1500, 2000, 3000, 4500, 6000]),
                description=s["listings"][0]["service"].capitalize() + " service",
                status="completed",
                rated=True,
                rating=stars,
            )
            db.session.add(txn)

        db.session.flush()
        profile.recalculate_rating()
        rating_str = f"{profile.avg_rating:.1f} ({profile.rating_count})" if profile.avg_rating else "New"
        print(f"  + seller {s['name']:<22} {s['city']:<14} {s['listings'][0]['service']:<18} {rating_str}")
        added += 1

    db.session.commit()
    print(f"\nDone — {added} sellers added.")
