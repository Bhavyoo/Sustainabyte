import hmac
import os
import secrets
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / '.env')
except ImportError:
    pass

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY') or os.urandom(32)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///mandi_queue.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

TIME_SLOTS = (
    '09:00 AM - 10:00 AM',
    '10:00 AM - 11:00 AM',
    '11:00 AM - 12:00 PM',
    '01:00 PM - 02:00 PM',
    '02:00 PM - 03:00 PM',
    '03:00 PM - 04:00 PM',
)

class Token(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    farmer_name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    crop = db.Column(db.String(50), nullable=False)
    token_number = db.Column(db.Integer, nullable=False)
    time_slot = db.Column(db.String(35), nullable=False, default=TIME_SLOTS[0])
    status = db.Column(db.String(20), default="Scheduled")


def csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_urlsafe(32)
    return session['_csrf_token']


def valid_csrf_token():
    submitted = request.form.get('csrf_token', '')
    expected = session.get('_csrf_token', '')
    return bool(expected) and hmac.compare_digest(submitted, expected)


app.jinja_env.globals['csrf_token'] = csrf_token

def send_booking_sms(token):
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    sender = os.getenv('TWILIO_PHONE_NUMBER')
    if not all((account_sid, auth_token, sender)):
        return False

    try:
        from twilio.rest import Client

        Client(account_sid, auth_token).messages.create(
            body=f"Your mandi token is #{token.token_number} for {token.crop}, {token.time_slot}.",
            from_=sender,
            to=token.phone,
        )
        return True
    except Exception:
        app.logger.exception('Could not send booking confirmation SMS')
        return False


@app.route('/')
def home():
    queue = Token.query.filter_by(status='Scheduled').all()
    slot_order = {slot: index for index, slot in enumerate(TIME_SLOTS)}
    queue.sort(key=lambda token: (slot_order.get(token.time_slot, len(TIME_SLOTS)), token.token_number))
    completed_queue = Token.query.filter_by(status='Completed').order_by(Token.id.desc()).limit(10).all()
    completed_count = Token.query.filter_by(status='Completed').count()

    return render_template(
        'index.html',
        queue=queue,
        active_token=queue[0] if queue else None,
        completed_queue=completed_queue,
        completed_count=completed_count,
        time_slots=TIME_SLOTS,
    )


@app.route('/book', methods=['POST'])
def book_slot():
    if not valid_csrf_token():
        abort(400)

    name = request.form.get('farmer_name', '').strip()
    phone = request.form.get('phone', '').strip()
    crop = request.form.get('crop', '').strip()
    time_slot = request.form.get('time_slot', '').strip()

    if not name or not phone or not crop or not time_slot:
        flash('Please complete all fields before booking.', 'error')
        return redirect(url_for('home'))
    if len(name) > 100 or len(phone) > 15 or len(crop) > 50 or time_slot not in TIME_SLOTS:
        flash('Please check the length of your name, phone number, and crop.', 'error')
        return redirect(url_for('home'))

    last_token = Token.query.order_by(Token.token_number.desc()).first()
    next_num = (last_token.token_number + 1) if last_token else 1

    new_token = Token(
        farmer_name=name,
        phone=phone,
        crop=crop,
        token_number=next_num,
        time_slot=time_slot,
    )
    db.session.add(new_token)
    db.session.commit()

    sms_sent = send_booking_sms(new_token)
    if sms_sent:
        flash(f'{name}, your {crop} booking is confirmed for {time_slot}. Token #{next_num}; details sent by SMS.', 'success')
    else:
        flash(f'{name}, your {crop} booking is confirmed for {time_slot}. Your token is #{next_num}.', 'success')
    return redirect(url_for('home'))


@app.route('/complete/<int:token_id>', methods=['POST'])
def complete_token(token_id):
    if not valid_csrf_token():
        abort(400)

    token = db.get_or_404(Token, token_id)
    if token.status != 'Scheduled':
        flash('This token is already completed.', 'error')
        return redirect(url_for('home'))

    token.status = 'Completed'
    db.session.commit()
    flash(f'{token.farmer_name} has moved on. Their queue position is free; remaining farmers were renumbered.', 'success')
    return redirect(url_for('home'))


def initialize_database():
    with app.app_context():
        db.create_all()
        column_names = {column['name'] for column in inspect(db.engine).get_columns(Token.__tablename__)}
        if 'time_slot' not in column_names:
            with db.engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE token ADD COLUMN time_slot VARCHAR(35) "
                    "NOT NULL DEFAULT '09:00 AM - 10:00 AM'"
                ))

if __name__ == '__main__':
    initialize_database()
    app.run(debug=True)