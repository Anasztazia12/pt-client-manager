import json
import os
from datetime import date, timedelta
from flask import Flask, render_template, redirect, url_for, flash, request
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from config import Config
from models import db, Client, WorkoutPlan, Exercise, PlanExercise, Session, FoodEntry, CheckIn
from forms import (
    ClientForm,
    WorkoutPlanForm,
    ExerciseForm,
    PlanExerciseForm,
    SessionForm,
    FoodEntryForm,
    CheckInForm,
    EXERCISE_CATEGORIES,
    MEALS,
)

SEED_FILE = os.path.join(os.path.dirname(__file__), "data", "exercises_seed.json")


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _seed_exercises()

    @app.context_processor
    def inject_today():
        return {"now_date": date.today()}

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
