import os
import uuid

import razorpay
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    raise RuntimeError("Razorpay API keys are missing")

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


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Suno UPI Razorpay Backend",
    })


@app.post("/create-order")
def create_order():
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")

    if plan not in PLANS:
        return jsonify({
            "success": False,
            "message": "Invalid plan",
        }), 400

    selected = PLANS[plan]

    try:
        # Unique receipt for every order.
        receipt = f"suno_{plan}_{uuid.uuid4().hex[:12]}"

        order_data = {
            "amount": selected["amount"],
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "plan": plan,
                "plan_name": selected["name"],
                "days": selected["days"],
            },
        }

        order = client.order.create(data=order_data)

        return jsonify({
            "success": True,
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

    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")

    if not all([
        razorpay_order_id,
        razorpay_payment_id,
        razorpay_signature,
    ]):
        return jsonify({
            "success": False,
            "verified": False,
            "captured": False,
            "message": "Missing payment verification data",
        }), 400

    try:
        # 1. Verify Razorpay's signature on the server.
        verification_data = {
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        }

        client.utility.verify_payment_signature(verification_data)

        # 2. Fetch the order from Razorpay, not from the Android app.
        order = client.order.fetch(razorpay_order_id)

        # Make sure the payment actually belongs to this order.
        if order.get("id") != razorpay_order_id:
            raise Exception("Order ID mismatch")

        # 3. Fetch the payment and require captured status.
        payment = client.payment.fetch(razorpay_payment_id)

        if payment.get("order_id") != razorpay_order_id:
            raise Exception("Payment does not belong to this order")

        captured = payment.get("status") == "captured"

        if not captured:
            return jsonify({
                "success": False,
                "verified": True,
                "captured": False,
                "message": "Payment verified but not captured yet",
                "payment_status": payment.get("status"),
            }), 400

        # 4. Read the plan and amount from the trusted Razorpay order.
        notes = order.get("notes") or {}
        plan = notes.get("plan")

        if plan not in PLANS:
            raise Exception("Invalid order plan")

        selected = PLANS[plan]

        if int(order.get("amount", 0)) != selected["amount"]:
            raise Exception("Order amount mismatch")

        return jsonify({
            "success": True,
            "verified": True,
            "captured": True,
            "payment_id": razorpay_payment_id,
            "order_id": razorpay_order_id,
            "plan": plan,
            "plan_name": selected["name"],
            "days": selected["days"],
            "amount": selected["amount"],
            "currency": "INR",
            "payment_status": payment.get("status"),
        })

    except Exception:
        return jsonify({
            "success": False,
            "verified": False,
            "captured": False,
            "message": "Payment signature/order verification failed",
        }), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )