from flask_wtf import FlaskForm
from wtforms import (
    StringField,
    TextAreaField,
    DecimalField,
    FloatField,
    IntegerField,
    DateField,
    SelectField,
)
from wtforms.validators import DataRequired, Email, Optional, Length, NumberRange


class ClientForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(max=100)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    goal = StringField("Goal", validators=[Optional(), Length(max=200)])
    injuries_notes = TextAreaField("Injuries / notes", validators=[Optional()])
    start_weight_kg = FloatField(
        "Start weight (kg)", validators=[Optional(), NumberRange(min=0, max=500)]
    )
    start_date = DateField("Start date", validators=[Optional()])
    daily_kcal_target = IntegerField(
        "Daily calorie target (kcal)", validators=[Optional(), NumberRange(min=800, max=6000)]
    )


SESSION_TYPES = [
    ("", "Not set"),
    ("strength", "Strength"),
    ("cardio", "Cardio"),
    ("hiit", "HIIT"),
    ("mobility", "Mobility & stretching"),
    ("assessment", "Assessment"),
]

EXERCISE_CATEGORIES = [
    ("", "Not set"),
    ("strength", "Strength"),
    ("cardio", "Cardio"),
    ("stretching", "Stretching"),
    ("plyometrics", "Plyometrics"),
    ("powerlifting", "Powerlifting"),
    ("olympic weightlifting", "Olympic weightlifting"),
    ("strongman", "Strongman"),
]

MEALS = [("breakfast", "Breakfast"), ("lunch", "Lunch"), ("dinner", "Dinner"), ("snack", "Snack")]


class WorkoutPlanForm(FlaskForm):
    name = StringField("Plan name", validators=[DataRequired(), Length(max=100)])
    description = TextAreaField("Description", validators=[Optional()])


class ExerciseForm(FlaskForm):
    name = StringField("Exercise name", validators=[DataRequired(), Length(max=150)])
    muscle_group = StringField("Primary muscle", validators=[Optional(), Length(max=60)])
    equipment = StringField("Equipment", validators=[Optional(), Length(max=60)])
    level = SelectField(
        "Level",
        choices=[("", "—"), ("beginner", "Beginner"), ("intermediate", "Intermediate"), ("expert", "Expert")],
        validators=[Optional()],
    )
    category = SelectField("Type", choices=EXERCISE_CATEGORIES, validators=[Optional()])
    description = TextAreaField("Instructions / description", validators=[Optional()])


class PlanExerciseForm(FlaskForm):
    exercise_id = SelectField("Exercise", coerce=int, validators=[DataRequired()])
    sets = IntegerField(
        "Sets", default=3, validators=[DataRequired(), NumberRange(min=1, max=20)]
    )
    reps = IntegerField(
        "Reps", default=10, validators=[DataRequired(), NumberRange(min=1, max=100)]
    )
    target_weight_kg = FloatField(
        "Target weight (kg)", validators=[Optional(), NumberRange(min=0, max=500)]
    )


class SessionForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()])
    session_type = SelectField("Session type", choices=SESSION_TYPES, validators=[Optional()])
    duration_minutes = IntegerField(
        "Duration (minutes)", validators=[Optional(), NumberRange(min=1, max=600)]
    )
    notes = TextAreaField("Notes", validators=[Optional()])


class FoodEntryForm(FlaskForm):
    meal = SelectField("Meal", choices=MEALS, validators=[DataRequired()])
    food = StringField("Food or drink", validators=[DataRequired(), Length(max=120)])
    kcal = IntegerField("kcal", validators=[DataRequired(), NumberRange(min=0, max=5000)])
    protein_g = FloatField("Protein (g)", validators=[Optional(), NumberRange(min=0, max=500)])
    carbs_g = FloatField("Carbs (g)", validators=[Optional(), NumberRange(min=0, max=1000)])
    fat_g = FloatField("Fat (g)", validators=[Optional(), NumberRange(min=0, max=500)])


class PaymentForm(FlaskForm):
    description = StringField("What it's for", validators=[DataRequired(), Length(max=120)])
    amount = DecimalField("Amount", places=2, validators=[DataRequired(), NumberRange(min=1, max=999999)])


class CheckInForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()])
    weight_kg = FloatField("Weight (kg)", validators=[DataRequired(), NumberRange(min=20, max=500)])
    waist_cm = FloatField("Waist (cm)", validators=[Optional(), NumberRange(min=30, max=300)])
