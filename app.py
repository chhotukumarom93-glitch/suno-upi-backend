import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import firebase_admin
from firebase_admin import credentials, firestore
import razorpay
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    raise RuntimeError("Razorpay API keys are missing")

if not FIREBASE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is missing")

try:
    service_account_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(cred)
except Exception as e:
    raise RuntimeError(f"Firebase Admin initialization failed: {e}")

firestore_db = firestore.client()

client = razorpay.Client(
    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
)

PLANS = {
    "trial": {
        "amount": 100,
        "name": "₹1 Trial",
        "days": 30,
    },
    "vip15": {
        "amount": 2900,
        "name": "₹29 - 15 Days VIP",
        "days": 15,
    },
    "vip30": {
        "amount": 4900,
        "name": "₹49 - 30 Days VIP",
        "days": 30,
    },
}

TRIAL_LOCK_MINUTES = 30


def now_utc():
    return datetime.now(timezone.utc)


def clean_string(value, max_len=256):
    if value is None:
        return ""
    return str(value).strip()[:max_len]


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Suno UPI Razorpay Backend",
    })


@app.post("/create-order")
def create_order():
    data = request.get_json(silent=True) or {}

    plan = clean_string(data.get("plan"), 32)
    firebase_uid = clean_string(data.get("firebase_uid"), 256)
    device_hash = clean_string(data.get("device_hash"), 128)
    agent_code = clean_string(data.get("agent_code"), 64).upper()

    if plan not in PLANS:
        return jsonify({
            "success": False,
            "message": "Invalid plan",
        }), 400

    if not firebase_uid or not device_hash:
        return jsonify({
            "success": False,
            "message": "User/device verification data missing",
        }), 400

    selected = PLANS[plan]

    try:
        # ---------------------------------------------------------
        # ₹1 TRIAL: one device + one Firebase account
        # ---------------------------------------------------------
        if plan == "trial":
            device_ref = firestore_db.collection("suno_trial_claims").document(
                f"device_{device_hash}"
            )
            user_ref = firestore_db.collection("suno_trial_claims").document(
                f"user_{firebase_uid}"
            )

            device_doc = device_ref.get()
            user_doc = user_ref.get()

            if device_doc.exists or user_doc.exists:
                return jsonify({
                    "success": False,
                    "trial_allowed": False,
                    "message": "₹1 Trial is already used on this phone or account.",
                }), 409

            # Prevent multiple simultaneous ₹1 trial orders. This lock is only
            # temporary; it becomes irrelevant once the payment is processed.
            pending_ref = firestore_db.collection("suno_trial_pending").document(device_hash)
            pending_doc = pending_ref.get()
            if pending_doc.exists:
                pending = pending_doc.to_dict() or {}
                expires_at = pending.get("lock_expires_at")
                if expires_at and expires_at > now_utc():
                    return jsonify({
                        "success": False,
                        "trial_allowed": False,
                        "message": "A ₹1 Trial payment is already in progress. Please finish it or wait a few minutes before trying again.",
                    }), 409
                pending_ref.delete()

        # ---------------------------------------------------------
        # CREATE RAZORPAY ORDER
        # ---------------------------------------------------------
        receipt = f"suno_{plan}_{uuid.uuid4().hex[:12]}"

        order_data = {
            "amount": selected["amount"],
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "plan": plan,
                "plan_name": selected["name"],
                "days": selected["days"],
                "firebase_uid": firebase_uid,
                "device_hash": device_hash,
                "agent_code": agent_code,
            },
        }

        order = client.order.create(data=order_data)

        # For trial, save a short-lived pending lock so the same phone/account
        # cannot create many simultaneous trial orders.
        if plan == "trial":
            expires_at = now_utc() + timedelta(minutes=TRIAL_LOCK_MINUTES)
            pending_data = {
                "status": "pending",
                "order_id": order["id"],
                "firebase_uid": firebase_uid,
                "device_hash": device_hash,
                "created_at": firestore.SERVER_TIMESTAMP,
                "lock_expires_at": expires_at,
            }
            firestore_db.collection("suno_trial_pending").document(
                device_hash
            ).set(pending_data)

        return jsonify({
            "success": True,
            "trial_allowed": plan != "trial" or True,
            "order_id": order["id"],
            "amount": selected["amount"],
            "currency": "INR",
            "plan": plan,
            "plan_name": selected["name"],
            "days": selected["days"],
            "key_id": RAZORPAY_KEY_ID,
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e),
        }), 500


@app.post("/verify-payment")
def verify_payment():
    data = request.get_json(silent=True) or {}

    razorpay_order_id = clean_string(data.get("razorpay_order_id"), 128)
    razorpay_payment_id = clean_string(data.get("razorpay_payment_id"), 128)
    razorpay_signature = clean_string(data.get("razorpay_signature"), 512)
    firebase_uid = clean_string(data.get("firebase_uid"), 256)
    device_hash = clean_string(data.get("device_hash"), 128)

    if not all([
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature,
        firebase_uid,
        device_hash,
    ]):
        return jsonify({
            "success": False,
            "verified": False,
            "captured": False,
            "message": "Missing payment/user verification data",
        }), 400

    try:
        # ---------------------------------------------------------
        # 1. VERIFY RAZORPAY SIGNATURE
        # ---------------------------------------------------------
        verification_data = {
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        }

        client.utility.verify_payment_signature(verification_data)

        # ---------------------------------------------------------
        # 2. FETCH TRUSTED ORDER
        # ---------------------------------------------------------
        order = client.order.fetch(razorpay_order_id)

        if order.get("id") != razorpay_order_id:
            raise Exception("Order ID mismatch")

        notes = order.get("notes") or {}
        plan = notes.get("plan")

        if plan not in PLANS:
            raise Exception("Invalid order plan")

        selected = PLANS[plan]

        # Order must belong to the same Firebase user/device that created it.
        if clean_string(notes.get("firebase_uid")) != firebase_uid:
            raise Exception("Firebase user mismatch")

        if clean_string(notes.get("device_hash")) != device_hash:
            raise Exception("Device verification mismatch")

        if int(order.get("amount", 0)) != selected["amount"]:
            raise Exception("Order amount mismatch")

        # ---------------------------------------------------------
        # 3. FETCH PAYMENT AND REQUIRE CAPTURED
        # ---------------------------------------------------------
        payment = client.payment.fetch(razorpay_payment_id)

        if payment.get("order_id") != razorpay_order_id:
            raise Exception("Payment does not belong to this order")

        captured = payment.get("status") == "captured"

        if not captured:
            return jsonify({
                "success": False,
                "verified": True,
                "captured": False,
                "already_processed": False,
                "message": "Payment verified but not captured yet",
                "payment_status": payment.get("status"),
            }), 400

        # ---------------------------------------------------------
        # 4. SERVER-SIDE IDEMPOTENCY
        # ---------------------------------------------------------
        payment_ref = firestore_db.collection("suno_processed_payments").document(
            razorpay_payment_id
        )

        transaction = firestore_db.transaction()

        @firestore.transactional
        def process_unique_payment(tx):
            existing = payment_ref.get(transaction=tx)

            if existing.exists:
                return False

            # For the ₹1 trial, atomically claim BOTH the phone and account.
            if plan == "trial":
                device_claim_ref = firestore_db.collection("suno_trial_claims").document(
                    f"device_{device_hash}"
                )
                user_claim_ref = firestore_db.collection("suno_trial_claims").document(
                    f"user_{firebase_uid}"
                )

                device_claim = device_claim_ref.get(transaction=tx)
                user_claim = user_claim_ref.get(transaction=tx)

                if device_claim.exists or user_claim.exists:
                    raise ValueError("TRIAL_ALREADY_USED")

                claim_data = {
                    "firebase_uid": firebase_uid,
                    "device_hash": device_hash,
                    "payment_id": razorpay_payment_id,
                    "order_id": razorpay_order_id,
                    "created_at": firestore.SERVER_TIMESTAMP,
                }

                tx.set(device_claim_ref, claim_data)
                tx.set(user_claim_ref, claim_data)

            payment_record = {
                "payment_id": razorpay_payment_id,
                "order_id": razorpay_order_id,
                "firebase_uid": firebase_uid,
                "device_hash": device_hash,
                "plan": plan,
                "plan_name": selected["name"],
                "days": selected["days"],
                "amount": selected["amount"],
                "currency": "INR",
                "payment_status": payment.get("status"),
                "processed_at": firestore.SERVER_TIMESTAMP,
            }

            tx.set(payment_ref, payment_record)
            return True

        try:
            unique_payment = process_unique_payment(transaction)
        except ValueError as e:
            if str(e) == "TRIAL_ALREADY_USED":
                return jsonify({
                    "success": False,
                    "verified": True,
                    "captured": True,
                    "already_processed": False,
                    "trial_allowed": False,
                    "message": "₹1 Trial is already used on this phone or account.",
                }), 409
            raise

        if not unique_payment:
            return jsonify({
                "success": True,
                "verified": True,
                "captured": True,
                "already_processed": True,
                "payment_id": razorpay_payment_id,
                "order_id": razorpay_order_id,
                "plan": plan,
                "plan_name": selected["name"],
                "days": selected["days"],
                "amount": selected["amount"],
                "currency": "INR",
                "payment_status": payment.get("status"),
                "message": "Payment already processed",
            })

        # ---------------------------------------------------------
        # 5. CLEAN PENDING TRIAL LOCK
        # ---------------------------------------------------------
        if plan == "trial":
            firestore_db.collection("suno_trial_pending").document(
                device_hash
            ).delete()

        # ---------------------------------------------------------
        # 6. SERVER-SIDE AGENT COMMISSION, ONLY ONCE
        # ---------------------------------------------------------
        agent_code = clean_string(notes.get("agent_code"), 64).upper()
        if agent_code:
            agent_ref = firestore_db.collection("agents").document(agent_code)
            agent_snapshot = agent_ref.get()

            if agent_snapshot.exists:
                commission = selected["amount"] / 100.0 * 0.30
                try:
                    agent_ref.update({
                        "balance": firestore.Increment(commission)
                    })
                except Exception:
                    # Payment remains processed. Commission can be reconciled later.
                    pass

        return jsonify({
            "success": True,
            "verified": True,
            "captured": True,
            "already_processed": False,
            "payment_id": razorpay_payment_id,
            "order_id": razorpay_order_id,
            "plan": plan,
            "plan_name": selected["name"],
            "days": selected["days"],
            "amount": selected["amount"],
            "currency": "INR",
            "payment_status": payment.get("status"),
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "verified": False,
            "captured": False,
            "message": "Payment verification failed",
        }), 400


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
