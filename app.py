import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from flask import Flask, abort, current_app, render_template, redirect, url_for, flash, request
import stripe
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from config import Config
from models import db, Client, WorkoutPlan, Exercise, PlanExercise, Session, FoodEntry, CheckIn, Payment
from forms import (
    ClientForm,
    WorkoutPlanForm,
    ExerciseForm,
    PlanExerciseForm,
    SessionForm,
    FoodEntryForm,
    CheckInForm,
    PaymentForm,
    EXERCISE_CATEGORIES,
    MEALS,
)

SEED_FILE = os.path.join(os.path.dirname(__file__), "data", "exercises_seed.json")

# Currencies the amount form can charge in. Each one is sent to Stripe as amount x 100, which is
# what Stripe expects for these; a zero-decimal currency such as JPY would charge 100 times too much.
CURRENCY_FORMATS = {"gbp": "£{}", "eur": "€{}", "usd": "${}", "huf": "{} Ft"}


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    if app.config["STRIPE_CURRENCY"] not in CURRENCY_FORMATS:
        raise RuntimeError(f"STRIPE_CURRENCY must be one of: {', '.join(CURRENCY_FORMATS)}")
    if app.config["DEMO_MODE"] and app.config["STRIPE_SECRET_KEY"].startswith(("sk_live_", "rk_live_")):
        raise RuntimeError("DEMO_MODE is public and has no login: use a Stripe test key (sk_test_).")
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _seed_exercises()
        if app.config["DEMO_MODE"]:
            _seed_demo()

    @app.context_processor
    def inject_today():
        return {
            "now_date": date.today(),
            "demo_mode": app.config["DEMO_MODE"],
            "stripe_enabled": bool(app.config["STRIPE_SECRET_KEY"]),
            "currency": app.config["STRIPE_CURRENCY"],
        }

    app.add_template_filter(_money, "money")
    app.add_template_filter(_client_photo, "client_photo")

    register_routes(app)
    register_error_handlers(app)
    return app


def _add_missing_columns():
    """db.create_all() makes new tables but never changes existing ones, so columns
    added later are added here (works on both SQLite and MySQL)."""
    new_columns = {
        "clients": {"daily_kcal_target": "INTEGER"},
        "sessions": {"session_type": "VARCHAR(30)"},
        "exercises": {"images": "VARCHAR(255)"},
    }
    inspector = inspect(db.engine)
    for table, columns in new_columns.items():
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
    db.session.commit()


def _day_totals(entries):
    """Calories and macros added up for a list of food entries."""
    return {
        "kcal": sum(e.kcal or 0 for e in entries),
        "protein": round(sum(e.protein_g or 0 for e in entries)),
        "carbs": round(sum(e.carbs_g or 0 for e in entries)),
        "fat": round(sum(e.fat_g or 0 for e in entries)),
    }


def _money(amount_minor, currency):
    """1250, "gbp" -> "£12.50"; whole amounts drop the decimals."""
    value = amount_minor / 100
    number = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    return CURRENCY_FORMATS.get(currency, "{} " + currency.upper()).format(number)


def _client_photo(name):
    """URL of static/avatars/<name-as-slug>.jpg, or an empty string when there is no such photo."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    filename = f"avatars/{slug}.jpg"
    if os.path.exists(os.path.join(current_app.static_folder, filename)):
        return url_for("static", filename=filename)
    return ""


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _apply_stripe_event(event_type, session):
    """Update our payment from a Checkout Session. Safe to run twice for the same event."""
    payment = Payment.query.filter_by(stripe_session_id=session["id"]).first()
    if payment is None or payment.status == "paid":
        return
    if event_type == "checkout.session.async_payment_succeeded" or (
        event_type == "checkout.session.completed" and session.get("payment_status") == "paid"
    ):
        payment.status = "paid"
        payment.paid_at = _utc_now()
    elif event_type == "checkout.session.async_payment_failed":
        payment.status = "failed"
    elif event_type == "checkout.session.expired":
        payment.status = "expired"
    db.session.commit()


def _seed_demo():
    """Sample clients for the public try-out copy, only when the database has no clients."""
    if Client.query.first():
        return
    today = date.today()
    # Each client shows something different on the overview: on track, nothing logged today,
    # over their calories, and not seen for over a week.
    samples = [
        {
            "name": "Emma Clarke", "goal": "Lose 6 kg and build strength", "phone": "07700 900142",
            "start": 72.5, "target": 1800, "scale": 0.9, "checkins": [71.8, 71.1, 70.6, 70.4],
            "meals_today": ("breakfast", "lunch", "snack"),
            "sessions": [(1, "strength", 60), (3, "strength", 60), (5, "cardio", 40), (8, "strength", 60), (10, "strength", 60)],
            "note": "Squat depth much better. Add 2.5 kg next time.",
            "plan": ("Strength base", "Three full-body sessions a week"),
            "paid": [(2, "Monthly coaching", 24000), (33, "Monthly coaching", 24000)],
        },
        {
            "name": "James Patel", "goal": "First pull-up and better posture", "phone": "07700 900318",
            "injuries": "Old left shoulder strain. Avoid heavy overhead pressing.",
            "start": 84.0, "target": 2300, "scale": 1.1, "checkins": [83.6, 83.1, 82.9],
            "meals_today": (),
            "sessions": [(2, "mobility", 45), (4, "strength", 60), (7, "strength", 60), (11, "assessment", 30)],
            "note": "Shoulder pain free. Band-assisted pull-ups, 3 sets of 5.",
            "plan": ("Pull-up progression", "Back and shoulder strength, twice a week"),
            "paid": [(6, "Personal training, 8 sessions", 32000)],
        },
        {
            "name": "Michael Brown", "goal": "Build muscle, 3 sessions a week", "phone": "07700 900527",
            "start": 76.0, "target": 2500, "scale": 1.32, "scale_today": 1.8, "checkins": [76.6, 77.3, 77.6, 77.9],
            "meals_today": ("breakfast", "lunch", "dinner", "snack"),
            "sessions": [(0, "strength", 75), (2, "strength", 75), (4, "strength", 75), (7, "hiit", 30), (9, "strength", 75)],
            "note": "New best on bench press. Keep rest at 2 minutes.",
            "plan": ("Hypertrophy block", "Push, pull and legs across three days"),
            "paid": [(1, "Personal training, 12 sessions", 45000)],
        },
        {
            "name": "Sophie Turner", "goal": "Run a 10k in under an hour", "phone": "07700 900864",
            "start": 63.0, "target": 2000, "scale": 1.0, "checkins": [62.8, 62.5],
            "meals_today": ("breakfast", "lunch"),
            "sessions": [(9, "cardio", 45), (12, "cardio", 50), (14, "mobility", 30)],
            "note": "5 km in 29 minutes, comfortable pace.",
            "plan": ("10k build", "Running support and leg strength"),
            "paid": [(12, "Personal training, 4 sessions", 18000)],
        },
        {
            "name": "Amara Osei", "goal": "Fitter for a two-week hiking trip", "phone": "07700 900236",
            "start": 68.0, "target": 2100, "scale": 1.0, "checkins": [67.6, 67.2, 66.9],
            "meals_today": ("breakfast", "lunch", "snack"),
            "sessions": [(0, "cardio", 45), (3, "strength", 60), (6, "mobility", 30), (10, "strength", 60)],
            "note": "Weighted step-ups went well. Longer walk this weekend.",
            "plan": ("Trail ready", "Legs, core and long easy cardio"),
            "paid": [(4, "Monthly coaching", 24000)],
        },
        {
            "name": "Daniel Novak", "goal": "Deadlift 180 kg by spring", "phone": "07700 900671",
            "start": 91.0, "target": 2800, "scale": 1.15, "checkins": [91.2, 91.5, 91.4],
            "meals_today": ("breakfast", "lunch"),
            "sessions": [(1, "powerlifting", 80), (5, "powerlifting", 80), (8, "strength", 70)],
            "note": "155 kg for a clean triple. Hold the same weight next week.",
            "plan": ("Deadlift cycle", "Heavy pulls, accessories and bracing work"),
            "paid": [(9, "Personal training, 8 sessions", 32000)],
        },
        {
            "name": "Lucy Hart", "goal": "Back to running after a knee injury", "phone": "07700 900493",
            "injuries": "Right knee, cleared by her physio. No jumping for now.",
            "start": 59.5, "target": 1900, "scale": 0.95, "checkins": [59.4, 59.2],
            "meals_today": ("breakfast", "lunch", "dinner"),
            "sessions": [(0, "stretching", 40), (2, "strength", 50), (6, "strength", 50)],
            "note": "No knee pain on split squats. Start walk-run intervals.",
            "plan": ("Knee rebuild", "Single-leg strength and gradual running"),
            "paid": [(15, "Personal training, 4 sessions", 18000)],
        },
    ]
    menu = {
        "breakfast": [("Porridge with berries", 420, 16, 68, 9), ("Eggs on wholemeal toast", 460, 26, 34, 22), ("Greek yoghurt and granola", 390, 24, 48, 11)],
        "lunch": [("Chicken and rice bowl", 620, 46, 72, 14), ("Tuna salad wrap", 510, 34, 48, 18), ("Lentil soup with bread", 480, 22, 70, 10)],
        "dinner": [("Salmon, potatoes and greens", 640, 42, 48, 28), ("Turkey chilli with rice", 680, 48, 76, 16), ("Tofu and vegetable stir fry", 560, 28, 62, 20)],
        "snack": [("Protein shake", 170, 28, 6, 3), ("Apple and peanut butter", 240, 7, 26, 13), ("Cottage cheese", 150, 20, 6, 4)],
    }
    weights = {"Barbell Squat": 60, "Pushups": None, "Bent Over Barbell Row": 40, "Romanian Deadlift": 50, "Plank": None}
    exercises = Exercise.query.filter(Exercise.name.in_(weights)).all()
    for number, sample in enumerate(samples):
        client = Client(
            name=sample["name"],
            email=f"{sample['name'].split()[0].lower()}@example.com",
            phone=sample["phone"],
            goal=sample["goal"],
            injuries_notes=sample.get("injuries"),
            start_weight_kg=sample["start"],
            start_date=today - timedelta(days=70),
            daily_kcal_target=sample["target"],
        )
        db.session.add(client)
        for weeks_ago, kg in zip(range(len(sample["checkins"]), 0, -1), sample["checkins"]):
            client.checkins.append(CheckIn(date=today - timedelta(weeks=weeks_ago, days=number), weight_kg=kg))
        for position, (days_ago, kind, minutes) in enumerate(sample["sessions"]):
            client.sessions.append(Session(
                date=today - timedelta(days=days_ago), session_type=kind, duration_minutes=minutes,
                notes=sample["note"] if position == 0 else None,
            ))
        for days_ago in range(14):
            if days_ago and (days_ago + number) % 6 == 5:
                continue  # a realistic gap in the diary
            for meal, options in menu.items():
                if days_ago == 0 and meal not in sample["meals_today"]:
                    continue
                food, kcal, protein, carbs, fat = options[(number + days_ago) % len(options)]
                scale = sample["scale"] + (days_ago % 3 - 1) * 0.04
                if days_ago == 0:
                    scale = sample.get("scale_today", scale)
                client.food_entries.append(FoodEntry(
                    date=today - timedelta(days=days_ago), meal=meal, food=food,
                    kcal=round(kcal * scale / 10) * 10,
                    protein_g=round(protein * scale), carbs_g=round(carbs * scale), fat_g=round(fat * scale),
                ))
        plan_name, plan_description = sample["plan"]
        plan = WorkoutPlan(name=plan_name, description=plan_description)
        for position, exercise in enumerate(exercises):
            plan.plan_exercises.append(PlanExercise(
                exercise_id=exercise.id, sets=3 + position % 2, reps=8 + 2 * (position % 3),
                target_weight_kg=weights[exercise.name],
            ))
        client.plans.append(plan)
        for days_ago, description, amount in sample["paid"]:
            paid_at = _utc_now() - timedelta(days=days_ago)
            client.payments.append(Payment(
                description=description,
                amount_minor=amount,
                currency=current_app.config["STRIPE_CURRENCY"],
                status="paid",
                created_at=paid_at,
                paid_at=paid_at,
            ))
    db.session.commit()


def _seed_exercises():
    """Populate the exercise library from the Free Exercise DB dataset
    (https://github.com/yuhonas/free-exercise-db, Unlicense / public domain)
    if the table is empty.
    """
    if not os.path.exists(SEED_FILE):
        return

    with open(SEED_FILE, encoding="utf-8") as f:
        data = json.load(f)

    if Exercise.query.first():
        # Library already seeded: fill in photos for exercises that don't have them yet
        photos = {
            (item.get("name") or "").strip().lower(): "|".join(item.get("images") or [])
            for item in data
            if item.get("images")
        }
        changed = False
        for ex in Exercise.query.filter(Exercise.images.is_(None)).all():
            if ex.name.lower() in photos:
                ex.images = photos[ex.name.lower()][:255]
                changed = True
        if changed:
            db.session.commit()
        return

    batch = []
    for item in data:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        primary = item.get("primaryMuscles") or []
        secondary = item.get("secondaryMuscles") or []
        instructions = item.get("instructions") or []
        batch.append(
            Exercise(
                name=name[:150],
                muscle_group=", ".join(primary)[:60] if primary else None,
                secondary_muscles=", ".join(secondary)[:200] if secondary else None,
                equipment=(item.get("equipment") or None),
                level=(item.get("level") or None),
                category=(item.get("category") or None),
                images="|".join(item.get("images") or [])[:255] or None,
                description="\n".join(instructions) if instructions else None,
            )
        )
    # Some entries share a name after trimming/variants; keep first occurrence only
    seen = set()
    unique_batch = []
    for ex in batch:
        key = ex.name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_batch.append(ex)

    db.session.bulk_save_objects(unique_batch)
    db.session.commit()


def register_routes(app):
    @app.route("/")
    def index():
        q = request.args.get("q", "").strip()
        query = Client.query
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Client.name.ilike(like), Client.goal.ilike(like)))
        clients = query.order_by(Client.name).all()
        return render_template("index.html", clients=clients, q=q)

    # ---------- CLIENTS ----------
    @app.route("/clients/new", methods=["GET", "POST"])
    def client_new():
        form = ClientForm()
        if form.validate_on_submit():
            client = Client(
                name=form.name.data,
                email=form.email.data,
                phone=form.phone.data,
                goal=form.goal.data,
                injuries_notes=form.injuries_notes.data,
                start_weight_kg=form.start_weight_kg.data,
                start_date=form.start_date.data or date.today(),
                daily_kcal_target=form.daily_kcal_target.data,
            )
            try:
                db.session.add(client)
                db.session.commit()
                flash(f"Client '{client.name}' added.", "success")
                return redirect(url_for("client_detail", client_id=client.id))
            except SQLAlchemyError:
                db.session.rollback()
                flash("Something went wrong saving this client. Please try again.", "danger")
        return render_template("client_form.html", form=form, title="Add client")

    @app.route("/clients/<int:client_id>")
    def client_detail(client_id):
        client = Client.query.get_or_404(client_id)
        today = date.today()
        today_totals = _day_totals([e for e in client.food_entries if e.date == today])
        # last 7 days of calories, oldest first, for the small bar chart
        week = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            week.append({"day": day, "kcal": sum(e.kcal for e in client.food_entries if e.date == day)})
        checkins = sorted(client.checkins, key=lambda c: c.date)
        checkin_form = CheckInForm(date=today)
        return render_template(
            "client_detail.html",
            client=client,
            today=today,
            today_totals=today_totals,
            week=week,
            week_max=max([d["kcal"] for d in week] + [client.daily_kcal_target or 0, 1]),
            checkins=checkins,
            checkin_form=checkin_form,
            payment_form=PaymentForm(),
            payments=sorted(client.payments, key=lambda p: p.created_at, reverse=True),
        )

    # ---------- OVERVIEW ----------
    @app.route("/overview")
    def overview():
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
        clients = Client.query.order_by(Client.name).all()
        week_sessions = Session.query.filter(Session.date >= week_start).order_by(Session.date.desc()).all()
        minutes = sum(s.duration_minutes or 0 for s in week_sessions)
        rows = []
        for c in clients:
            kcal = sum(e.kcal for e in c.food_entries if e.date == today)
            last = max((s.date for s in c.sessions), default=None)
            checkins = sorted(c.checkins, key=lambda x: x.date)
            change = None
            if checkins and c.start_weight_kg:
                change = round(checkins[-1].weight_kg - c.start_weight_kg, 1)
            pct = round(kcal / c.daily_kcal_target * 100) if c.daily_kcal_target else None
            rows.append({
                "client": c,
                "kcal": kcal,
                "pct": pct,
                "last": last,
                "days_since": (today - last).days if last else None,
                "change": change,
                "logged_today": any(e.date == today for e in c.food_entries),
            })
        # who needs a message from the trainer today, and why
        attention = []
        for r in rows:
            if r["days_since"] is None:
                attention.append((r, "No session booked yet"))
            elif r["days_since"] > 7:
                attention.append((r, f"No session for {r['days_since']} days"))
            elif r["pct"] is not None and r["pct"] > 110:
                attention.append((r, "Over today's calories"))
            elif not r["logged_today"]:
                attention.append((r, "No food logged today"))
        # Mon-Sun strip: how many sessions on each day of this week
        week = []
        for i in range(7):
            day = week_start + timedelta(days=i)
            todays = [s for s in week_sessions if s.date == day]
            week.append({"day": day, "sessions": todays})
        by_type = {}
        for s in week_sessions:
            key = s.session_type or "other"
            by_type[key] = by_type.get(key, 0) + 1
        return render_template(
            "overview.html",
            today=today,
            clients=clients,
            week_sessions=week_sessions,
            minutes=minutes,
            rows=rows,
            attention=attention,
            week=week,
            by_type=sorted(by_type.items(), key=lambda kv: -kv[1]),
            collected_month=sum(
                p.amount_minor
                for p in Payment.query.filter_by(status="paid", currency=app.config["STRIPE_CURRENCY"])
                if p.paid_at and (p.paid_at.year, p.paid_at.month) == (today.year, today.month)
            ),
        )

    @app.route("/day/<day_iso>")
    def day_view(day_iso):
        try:
            day = date.fromisoformat(day_iso)
        except ValueError:
            return redirect(url_for("overview"))
        sessions = Session.query.filter(Session.date == day).all()
        clients = Client.query.order_by(Client.name).all()
        trained_ids = {s.client_id for s in sessions}
        return render_template(
            "day.html",
            day=day,
            today=date.today(),
            sessions=sorted(sessions, key=lambda s: s.client.name),
            minutes=sum(s.duration_minutes or 0 for s in sessions),
            not_trained=[c for c in clients if c.id not in trained_ids],
            prev_day=day - timedelta(days=1),
            next_day=day + timedelta(days=1),
        )

    # ---------- CALORIE CALCULATOR ----------
    ACTIVITY = [
        ("1.2", "Desk job, little exercise"),
        ("1.375", "Light exercise 1 to 3 days a week"),
        ("1.55", "Moderate exercise 3 to 5 days a week"),
        ("1.725", "Hard exercise 6 to 7 days a week"),
        ("1.9", "Very hard training or a physical job"),
    ]
    GOALS = [("-500", "Lose about 0.5 kg a week"), ("-250", "Lose slowly"), ("0", "Maintain"), ("250", "Gain slowly"), ("400", "Build muscle")]

    @app.route("/calculator", methods=["GET", "POST"])
    def calculator():
        clients = Client.query.order_by(Client.name).all()
        v = request.values
        result = None
        errors = []
        def number(key):
            # accepts "72,5" as well as "72.5"
            return float((v.get(key) or "").strip().replace(",", "."))

        if v.get("weight"):
            try:
                sex = v.get("sex", "female")
                age, height, weight = int(number("age")), number("height"), number("weight")
                if height < 3:  # entered in metres, e.g. 1.68
                    height *= 100
                activity, goal = float(v.get("activity", "1.55")), int(v.get("goal", "0"))
                if not (14 <= age <= 100 and 120 <= height <= 230 and 30 <= weight <= 300):
                    raise ValueError
                # Mifflin-St Jeor equation
                bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "male" else -161)
                tdee = bmr * activity
                target = max(1200 if sex == "female" else 1500, round((tdee + goal) / 10) * 10)
                protein = round(weight * (2.0 if goal < 0 or goal >= 400 else 1.6))
                fat = round(target * 0.25 / 9)
                carbs = max(0, round((target - protein * 4 - fat * 9) / 4))
                result = {"bmr": round(bmr), "tdee": round(tdee), "target": target, "protein": protein, "fat": fat, "carbs": carbs}
            except (KeyError, ValueError):
                errors.append("Please enter an age between 14 and 100, height in cm and weight in kg.")
        if request.method == "POST" and result and v.get("client_id"):
            client = Client.query.get_or_404(int(v["client_id"]))
            client.daily_kcal_target = result["target"]
            db.session.commit()
            flash(f"Daily target for {client.name} set to {result['target']} kcal.", "success")
            return redirect(url_for("client_detail", client_id=client.id))
        return render_template(
            "calculator.html", v=v, result=result, errors=errors, clients=clients, activity=ACTIVITY, goals=GOALS
        )

    @app.route("/nutrition")
    def nutrition_overview():
        today = date.today()
        rows = []
        for c in Client.query.order_by(Client.name).all():
            entries = [e for e in c.food_entries if e.date == today]
            rows.append({"client": c, "totals": _day_totals(entries), "logged": bool(entries)})
        return render_template("nutrition_overview.html", rows=rows, today=today)

    # ---------- NUTRITION ----------
    @app.route("/clients/<int:client_id>/nutrition", methods=["GET", "POST"])
    def nutrition(client_id):
        client = Client.query.get_or_404(client_id)
        try:
            day = date.fromisoformat(request.args.get("day", ""))
        except ValueError:
            day = date.today()
        form = FoodEntryForm()
        if form.validate_on_submit():
            entry = FoodEntry(
                client_id=client.id,
                date=day,
                meal=form.meal.data,
                food=form.food.data,
                kcal=form.kcal.data,
                protein_g=form.protein_g.data,
                carbs_g=form.carbs_g.data,
                fat_g=form.fat_g.data,
            )
            db.session.add(entry)
            db.session.commit()
            flash(f"{entry.food} added to {entry.meal}.", "success")
            return redirect(url_for("nutrition", client_id=client.id, day=day.isoformat()))
        entries = [e for e in client.food_entries if e.date == day]
        by_meal = [(key, label, [e for e in entries if e.meal == key]) for key, label in MEALS]
        return render_template(
            "nutrition.html",
            client=client,
            form=form,
            day=day,
            prev_day=day - timedelta(days=1),
            next_day=day + timedelta(days=1),
            totals=_day_totals(entries),
            by_meal=by_meal,
        )

    @app.route("/food/<int:entry_id>/delete", methods=["POST"])
    def food_delete(entry_id):
        entry = FoodEntry.query.get_or_404(entry_id)
        client_id, day = entry.client_id, entry.date
        db.session.delete(entry)
        db.session.commit()
        flash("Entry removed.", "success")
        return redirect(url_for("nutrition", client_id=client_id, day=day.isoformat()))

    # ---------- PROGRESS CHECK-INS ----------
    @app.route("/clients/<int:client_id>/checkins", methods=["POST"])
    def checkin_add(client_id):
        client = Client.query.get_or_404(client_id)
        form = CheckInForm()
        if form.validate_on_submit():
            db.session.add(
                CheckIn(
                    client_id=client.id,
                    date=form.date.data,
                    weight_kg=form.weight_kg.data,
                    waist_cm=form.waist_cm.data,
                )
            )
            db.session.commit()
            flash("Check-in saved.", "success")
        else:
            flash("Could not save the check-in. Please check the weight and date.", "danger")
        return redirect(url_for("client_detail", client_id=client.id) + "#progress")

    @app.route("/checkins/<int:checkin_id>/delete", methods=["POST"])
    def checkin_delete(checkin_id):
        checkin = CheckIn.query.get_or_404(checkin_id)
        client_id = checkin.client_id
        db.session.delete(checkin)
        db.session.commit()
        flash("Check-in removed.", "success")
        return redirect(url_for("client_detail", client_id=client_id) + "#progress")

    # ---------- PAYMENTS (STRIPE CHECKOUT) ----------
    @app.route("/payments")
    def payment_list():
        currency = app.config["STRIPE_CURRENCY"]
        payments = Payment.query.order_by(Payment.created_at.desc()).all()
        in_currency = [p for p in payments if p.currency == currency]
        return render_template(
            "payments.html",
            payments=payments,
            collected=sum(p.amount_minor for p in in_currency if p.current_status == "paid"),
            outstanding=sum(p.amount_minor for p in in_currency if p.current_status == "pending"),
        )

    @app.route("/clients/<int:client_id>/payments", methods=["POST"])
    def payment_new(client_id):
        client = Client.query.get_or_404(client_id)
        back = redirect(url_for("client_detail", client_id=client.id) + "#payments")
        secret_key = app.config["STRIPE_SECRET_KEY"]
        if not secret_key:
            flash("Stripe is not set up yet: add STRIPE_SECRET_KEY first.", "danger")
            return back
        form = PaymentForm()
        if not form.validate_on_submit():
            flash("Could not create the link. Please check the description and amount.", "danger")
            return back

        currency = app.config["STRIPE_CURRENCY"]
        amount_minor = int(form.amount.data * 100)
        base_url = (app.config["PUBLIC_URL"] or request.host_url).rstrip("/")
        params = {
            "mode": "payment",
            "line_items": [{
                "quantity": 1,
                "price_data": {
                    "currency": currency,
                    "unit_amount": amount_minor,
                    "product_data": {"name": form.description.data},
                },
            }],
            # {CHECKOUT_SESSION_ID} is filled in by Stripe, so it has to stay unencoded
            "success_url": base_url + url_for("payment_result") + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": base_url + url_for("payment_cancelled"),
        }
        if client.email:
            params["customer_email"] = client.email
        try:
            session = stripe.checkout.Session.create(api_key=secret_key, **params)
        except stripe.StripeError as error:
            flash(f"Stripe could not create the link: {error.user_message or 'please try again.'}", "danger")
            return back

        db.session.add(Payment(
            client_id=client.id,
            description=form.description.data,
            amount_minor=amount_minor,
            currency=currency,
            stripe_session_id=session.id,
            checkout_url=session.url,
            expires_at=datetime.fromtimestamp(session.expires_at, timezone.utc).replace(tzinfo=None),
        ))
        db.session.commit()
        flash("Payment link created. Copy it and send it to your client.", "success")
        return back

    @app.route("/payments/<int:payment_id>/cancel", methods=["POST"])
    def payment_cancel(payment_id):
        payment = Payment.query.get_or_404(payment_id)
        if payment.current_status == "pending":
            try:
                stripe.checkout.Session.expire(payment.stripe_session_id, api_key=app.config["STRIPE_SECRET_KEY"])
                payment.status = "expired"
                db.session.commit()
                flash("Payment link cancelled.", "success")
            except stripe.StripeError:
                flash("Stripe could not cancel this link. It may already be paid.", "danger")
        return redirect(url_for("client_detail", client_id=payment.client_id) + "#payments")

    # The next two pages are what a paying client lands on, so they stand alone, without the trainer's menu.
    @app.route("/payments/result")
    def payment_result():
        payment = Payment.query.filter_by(stripe_session_id=request.args.get("session_id", "")).first_or_404()
        if payment.status != "paid":
            # The client can get here before the webhook arrives, so ask Stripe directly
            try:
                session = stripe.checkout.Session.retrieve(
                    payment.stripe_session_id, api_key=app.config["STRIPE_SECRET_KEY"]
                )
                _apply_stripe_event(
                    "checkout.session.completed",
                    {"id": session.id, "payment_status": session.payment_status},
                )
            except stripe.StripeError:
                pass  # shown as "processing"; the webhook will still update it
        return render_template("payment_result.html", payment=payment)

    @app.route("/payments/cancelled")
    def payment_cancelled():
        return render_template("payment_result.html", payment=None)

    @app.route("/stripe/webhook", methods=["POST"])
    def stripe_webhook():
        secret = app.config["STRIPE_WEBHOOK_SECRET"]
        if not secret:
            abort(503)
        payload = request.get_data()
        try:
            # Only used to check the signature; the event is then read from the verified payload
            stripe.Webhook.construct_event(payload, request.headers.get("Stripe-Signature", ""), secret)
        except (ValueError, stripe.SignatureVerificationError):
            abort(400)
        event = json.loads(payload)
        if event["type"].startswith("checkout.session."):
            _apply_stripe_event(event["type"], event["data"]["object"])
        return "", 200

    @app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
    def client_edit(client_id):
        client = Client.query.get_or_404(client_id)
        form = ClientForm(obj=client)
        if form.validate_on_submit():
            form.populate_obj(client)
            try:
                db.session.commit()
                flash(f"Client '{client.name}' updated.", "success")
                return redirect(url_for("client_detail", client_id=client.id))
            except SQLAlchemyError:
                db.session.rollback()
                flash("Something went wrong updating this client.", "danger")
        return render_template("client_form.html", form=form, title="Edit client")

    @app.route("/clients/<int:client_id>/delete", methods=["POST"])
    def client_delete(client_id):
        client = Client.query.get_or_404(client_id)
        name = client.name
        try:
            db.session.delete(client)
            db.session.commit()
            flash(f"Client '{name}' deleted.", "success")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Could not delete this client.", "danger")
        return redirect(url_for("index"))

    # ---------- WORKOUT PLANS ----------
    @app.route("/clients/<int:client_id>/plans/new", methods=["GET", "POST"])
    def plan_new(client_id):
        client = Client.query.get_or_404(client_id)
        form = WorkoutPlanForm()
        if form.validate_on_submit():
            plan = WorkoutPlan(
                client_id=client.id, name=form.name.data, description=form.description.data
            )
            db.session.add(plan)
            db.session.commit()
            flash(f"Plan '{plan.name}' created.", "success")
            return redirect(url_for("plan_detail", plan_id=plan.id))
        return render_template("plan_form.html", form=form, client=client, title="New plan")

    @app.route("/plans/<int:plan_id>")
    def plan_detail(plan_id):
        plan = WorkoutPlan.query.get_or_404(plan_id)
        add_form = PlanExerciseForm()
        add_form.exercise_id.choices = [
            (e.id, f"{e.name} ({e.muscle_group}, {e.category})" if e.category else f"{e.name} ({e.muscle_group})")
            for e in Exercise.query.order_by(Exercise.name)
        ]
        return render_template("plan_detail.html", plan=plan, add_form=add_form)

    @app.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
    def plan_edit(plan_id):
        plan = WorkoutPlan.query.get_or_404(plan_id)
        form = WorkoutPlanForm(obj=plan)
        if form.validate_on_submit():
            form.populate_obj(plan)
            db.session.commit()
            flash(f"Plan '{plan.name}' updated.", "success")
            return redirect(url_for("plan_detail", plan_id=plan.id))
        return render_template("plan_form.html", form=form, client=plan.client, title="Edit plan")

    @app.route("/plans/<int:plan_id>/delete", methods=["POST"])
    def plan_delete(plan_id):
        plan = WorkoutPlan.query.get_or_404(plan_id)
        client_id = plan.client_id
        db.session.delete(plan)
        db.session.commit()
        flash("Plan deleted.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    # ---------- EXERCISES WITHIN A PLAN ----------
    @app.route("/plans/<int:plan_id>/exercises/add", methods=["POST"])
    def plan_exercise_add(plan_id):
        plan = WorkoutPlan.query.get_or_404(plan_id)
        form = PlanExerciseForm()
        form.exercise_id.choices = [
            (e.id, e.name) for e in Exercise.query.order_by(Exercise.name)
        ]
        if form.validate_on_submit():
            pe = PlanExercise(
                plan_id=plan.id,
                exercise_id=form.exercise_id.data,
                sets=form.sets.data,
                reps=form.reps.data,
                target_weight_kg=form.target_weight_kg.data,
            )
            db.session.add(pe)
            db.session.commit()
            flash("Exercise added to plan.", "success")
        else:
            flash("Could not add the exercise. Please check the values entered.", "danger")
        return redirect(url_for("plan_detail", plan_id=plan.id))

    @app.route("/plan-exercises/<int:pe_id>/delete", methods=["POST"])
    def plan_exercise_delete(pe_id):
        pe = PlanExercise.query.get_or_404(pe_id)
        plan_id = pe.plan_id
        db.session.delete(pe)
        db.session.commit()
        flash("Exercise removed from plan.", "success")
        return redirect(url_for("plan_detail", plan_id=plan_id))

    # ---------- EXERCISE LIBRARY ----------
    @app.route("/exercises")
    def exercise_list():
        q = request.args.get("q", "").strip()
        category = request.args.get("type", "").strip()
        muscle = request.args.get("muscle", "").strip()
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 24
        query = Exercise.query
        if q:
            like = f"%{q}%"
            query = query.filter(
                db.or_(Exercise.name.ilike(like), Exercise.muscle_group.ilike(like))
            )
        if category:
            query = query.filter(Exercise.category == category)
        if muscle:
            query = query.filter(Exercise.muscle_group.ilike(f"%{muscle}%"))
        found = query.count()
        exercises = query.order_by(Exercise.name).offset((page - 1) * per_page).limit(per_page).all()
        total = Exercise.query.count()
        counts = dict(
            db.session.query(Exercise.category, db.func.count(Exercise.id)).group_by(Exercise.category).all()
        )
        types = [(key, label, counts.get(key, 0)) for key, label in EXERCISE_CATEGORIES if key]
        muscles = ["chest", "shoulders", "biceps", "triceps", "lats", "middle back", "lower back",
                   "abdominals", "glutes", "quadriceps", "hamstrings", "calves"]
        return render_template(
            "exercises.html", exercises=exercises, q=q, total=total, found=found, types=types,
            active_type=category, muscles=muscles, active_muscle=muscle,
            page=page, pages=max(1, -(-found // per_page)),
        )

    @app.route("/exercises/new", methods=["GET", "POST"])
    def exercise_new():
        form = ExerciseForm()
        if form.validate_on_submit():
            ex = Exercise(
                name=form.name.data,
                muscle_group=form.muscle_group.data,
                equipment=form.equipment.data,
                level=form.level.data or None,
                category=form.category.data or None,
                description=form.description.data,
            )
            db.session.add(ex)
            db.session.commit()
            flash(f"Exercise '{ex.name}' added.", "success")
            return redirect(url_for("exercise_list"))
        return render_template("exercise_form.html", form=form, title="Add exercise")

    @app.route("/exercises/<int:exercise_id>/delete", methods=["POST"])
    def exercise_delete(exercise_id):
        ex = Exercise.query.get_or_404(exercise_id)
        db.session.delete(ex)
        db.session.commit()
        flash("Exercise removed from library.", "success")
        return redirect(url_for("exercise_list"))

    # ---------- SESSIONS ----------
    @app.route("/clients/<int:client_id>/sessions/new", methods=["GET", "POST"])
    def session_new(client_id):
        client = Client.query.get_or_404(client_id)
        form = SessionForm()
        if form.validate_on_submit():
            session_log = Session(
                client_id=client.id,
                date=form.date.data,
                session_type=form.session_type.data or None,
                duration_minutes=form.duration_minutes.data,
                notes=form.notes.data,
            )
            db.session.add(session_log)
            db.session.commit()
            flash("Session logged.", "success")
            return redirect(url_for("client_detail", client_id=client.id))
        return render_template("session_form.html", form=form, client=client, title="Log session")

    @app.route("/sessions/<int:session_id>/edit", methods=["GET", "POST"])
    def session_edit(session_id):
        session_log = Session.query.get_or_404(session_id)
        form = SessionForm(obj=session_log)
        if form.validate_on_submit():
            form.populate_obj(session_log)
            db.session.commit()
            flash("Session updated.", "success")
            return redirect(url_for("client_detail", client_id=session_log.client_id))
        return render_template(
            "session_form.html", form=form, client=session_log.client, title="Edit session"
        )

    @app.route("/sessions/<int:session_id>/delete", methods=["POST"])
    def session_delete(session_id):
        session_log = Session.query.get_or_404(session_id)
        client_id = session_log.client_id
        db.session.delete(session_log)
        db.session.commit()
        flash("Session deleted.", "success")
        return redirect(url_for("client_detail", client_id=client_id))


def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template("500.html"), 500


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
