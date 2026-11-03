from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    goal = db.Column(db.String(200))
    injuries_notes = db.Column(db.Text)
    start_weight_kg = db.Column(db.Float)
    start_date = db.Column(db.Date, default=datetime.utcnow)
    daily_kcal_target = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    plans = db.relationship(
        "WorkoutPlan", backref="client", cascade="all, delete-orphan", lazy=True
    )
    sessions = db.relationship(
        "Session", backref="client", cascade="all, delete-orphan", lazy=True
    )
    food_entries = db.relationship(
        "FoodEntry", backref="client", cascade="all, delete-orphan", lazy=True
    )
    checkins = db.relationship(
        "CheckIn", backref="client", cascade="all, delete-orphan", lazy=True
    )

    def __repr__(self):
        return f"<Client {self.name}>"


class Exercise(db.Model):
    __tablename__ = "exercises"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    muscle_group = db.Column(db.String(60))  # primary muscle, kept for backwards compatibility
    description = db.Column(db.Text)  # instructions, joined into one text block
    equipment = db.Column(db.String(60))
    level = db.Column(db.String(20))  # beginner / intermediate / expert
    category = db.Column(db.String(30))  # strength / cardio / stretching ...
    secondary_muscles = db.Column(db.String(200))
    images = db.Column(db.String(255))  # "Name/0.jpg|Name/1.jpg", start and end of the movement

    IMAGE_BASE = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"

    @property
    def image_urls(self):
        return [self.IMAGE_BASE + p for p in (self.images or "").split("|") if p]

    def __repr__(self):
        return f"<Exercise {self.name}>"


class WorkoutPlan(db.Model):
    __tablename__ = "workout_plans"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    plan_exercises = db.relationship(
        "PlanExercise", backref="plan", cascade="all, delete-orphan", lazy=True
    )

    def __repr__(self):
        return f"<WorkoutPlan {self.name}>"


class PlanExercise(db.Model):
    """Join table: which exercises belong to a plan, with target sets/reps."""

    __tablename__ = "plan_exercises"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("workout_plans.id"), nullable=False)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercises.id"), nullable=False)
    sets = db.Column(db.Integer, default=3)
    reps = db.Column(db.Integer, default=10)
    target_weight_kg = db.Column(db.Float)

    exercise = db.relationship("Exercise")


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    date = db.Column(db.Date, default=datetime.utcnow, nullable=False)
    session_type = db.Column(db.String(30))  # strength / cardio / mobility / HIIT / assessment
    duration_minutes = db.Column(db.Integer)
    notes = db.Column(db.Text)

    def __repr__(self):
        return f"<Session {self.date} client={self.client_id}>"


class FoodEntry(db.Model):
    """One food or drink a client logged, with calories and macros."""

    __tablename__ = "food_entries"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    date = db.Column(db.Date, default=datetime.utcnow, nullable=False)
    meal = db.Column(db.String(20), nullable=False)  # breakfast / lunch / dinner / snack
    food = db.Column(db.String(120), nullable=False)
    kcal = db.Column(db.Integer, nullable=False)
    protein_g = db.Column(db.Float)
    carbs_g = db.Column(db.Float)
    fat_g = db.Column(db.Float)


class CheckIn(db.Model):
    """Body weight and waist measurement on a given day, for progress tracking."""

    __tablename__ = "checkins"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    date = db.Column(db.Date, default=datetime.utcnow, nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)
    waist_cm = db.Column(db.Float)
