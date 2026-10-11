"""Smart Parking Lot Allocation & Billing System - prototype (Streamlit + MySQL)."""
import hashlib, math, os, re
from datetime import date, datetime, timedelta

import mysql.connector
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="Smart Parking", page_icon="🅿️", layout="wide")

st.markdown("""
<style>
.stButton>button[kind="primary"]{color:#070a12;font-weight:700}
.slot{display:inline-block;width:78px;margin:3px;padding:8px 0;text-align:center;border-radius:8px;
      font:600 12px monospace;border:1px solid rgba(255,255,255,.08)}
.AVAILABLE{background:rgba(16,185,129,.18);color:#34d399}
.RESERVED{background:rgba(245,158,11,.18);color:#fbbf24}
.OCCUPIED{background:rgba(244,63,94,.18);color:#fb7185}
.MAINTENANCE{background:rgba(148,163,184,.15);color:#94a3b8}
.zone-title{color:#00F2FE;font:700 13px monospace;letter-spacing:.1em;margin-top:10px}
.receipt-card{background:#0c1220;border:1px solid #1e293b;border-radius:12px;padding:24px;max-width:540px;margin:12px auto;color:#e2e8f0;font-family:monospace;box-shadow:0 8px 24px rgba(0,0,0,0.4)}
.receipt-header{text-align:center;border-bottom:1px dashed #334155;padding-bottom:16px;margin-bottom:16px}
.receipt-title{color:#00F2FE;font-size:20px;font-weight:700;margin-bottom:4px}
.receipt-row{display:flex;justify-content:space-between;margin:6px 0;font-size:13px}
.receipt-divider{border-top:1px dashed #334155;margin:14px 0}
.receipt-total{display:flex;justify-content:space-between;font-size:16px;font-weight:700;color:#00F2FE;margin:10px 0}
.receipt-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}
.badge-paid{background:rgba(16,185,129,.2);color:#34d399}
.badge-unpaid{background:rgba(244,63,94,.2);color:#fb7185}
@media print {
  body * { visibility: hidden !important; }
  .receipt-card, .receipt-card * { visibility: visible !important; }
  .receipt-card { position: absolute; left: 0; top: 0; width: 100%; border: none !important; box-shadow: none !important; color: #000 !important; }
  .receipt-title, .receipt-total { color: #000 !important; }
}
</style>""", unsafe_allow_html=True)

# ---------------------------------------------------------------- DB helpers
def conn():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "parking_db"))

def q(sql, params=()):
    c = conn()
    try:
        cur = c.cursor(dictionary=True); cur.execute(sql, params); return cur.fetchall()
    finally:
        c.close()

def ex(sql, params=()):
    c = conn()
    try:
        cur = c.cursor(); cur.execute(sql, params); c.commit(); return cur.lastrowid
    finally:
        c.close()

def tx(fn):
    """Run fn(cursor) in one transaction; returns (ok, result_or_error)."""
    c = conn()
    try:
        cur = c.cursor(dictionary=True)
        c.start_transaction()
        out = fn(cur)
        c.commit()
        return True, out
    except Exception as e:
        c.rollback()
        return False, str(e)
    finally:
        c.close()

def df(rows):
    return pd.DataFrame(rows)

def result(res, ok_msg):
    ok, out = res
    (st.success if ok else st.error)(ok_msg if ok else f"Failed: {out}")
    return ok

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.strip().encode()).hexdigest()

def authenticate(username, password):
    rows = q("SELECT user_id, username, password_hash, role, customer_id FROM app_user WHERE username=%s",
             (username.strip(),))
    if rows and rows[0]["password_hash"] == hash_password(password):
        return rows[0]
    return None

def validate_license_plate(plate: str) -> bool:
    """Validate vehicle license plate format (e.g., TS09AB1234, KA01CD5678, DL12C1234)."""
    clean = plate.replace(" ", "").replace("-", "").upper()
    return bool(re.match(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}$", clean))

# ---------------------------------------------------------------- business logic
def expire_reservations(cur=None):
    """15-minute arrival buffer: auto-cancel late PENDING reservations, free their slots."""
    def f(cur):
        cur.execute("UPDATE reservation SET status='CANCELLED' WHERE status='PENDING' "
                    "AND (start_time + INTERVAL 15 MINUTE < NOW() OR end_time < NOW())")
        cur.execute("UPDATE slot s SET status='AVAILABLE' WHERE status='RESERVED' AND NOT EXISTS "
                    "(SELECT 1 FROM reservation r WHERE r.slot_id=s.slot_id AND r.status='PENDING')")
    tx(f)

def calc_amount(mins, base, hourly, grace, mult):
    """Within grace -> free. Else (base + hourly * billable_hours) * vehicle multiplier."""
    billable = max(0, mins - grace)
    if billable == 0:
        return 0.0
    return round((float(base) + float(hourly) * math.ceil(billable / 60)) * float(mult), 2)

def check_in(vehicle_id, reservation_id=None):
    def f(cur):
        cur.execute("SELECT session_id FROM parking_session WHERE vehicle_id=%s AND status='ACTIVE'", (vehicle_id,))
        if cur.fetchone():
            raise ValueError("Vehicle is already checked in with an active parking session")
        cur.execute("SELECT vehicle_type_id FROM vehicle WHERE vehicle_id=%s", (vehicle_id,))
        v_row = cur.fetchone()
        if not v_row: raise ValueError("Vehicle not found")
        vt = v_row["vehicle_type_id"]
        if reservation_id:
            cur.execute("SELECT slot_id FROM reservation WHERE reservation_id=%s AND vehicle_id=%s "
                        "AND status='PENDING' FOR UPDATE", (reservation_id, vehicle_id))
            r = cur.fetchone()
            if not r: raise ValueError("Reservation is not valid any more")
            slot_id = r["slot_id"]
            cur.execute("UPDATE reservation SET status='ACTIVE' WHERE reservation_id=%s", (reservation_id,))
        else:
            cur.execute("SELECT slot_id FROM slot WHERE status='AVAILABLE' AND vehicle_type_id=%s "
                        "ORDER BY slot_id LIMIT 1 FOR UPDATE", (vt,))
            r = cur.fetchone()
            if not r: raise ValueError("No free slot for this vehicle type")
            slot_id = r["slot_id"]
        cur.execute("INSERT INTO parking_session (vehicle_id, slot_id, reservation_id) VALUES (%s,%s,%s)",
                    (vehicle_id, slot_id, reservation_id))
        cur.execute("UPDATE slot SET status='OCCUPIED' WHERE slot_id=%s", (slot_id,))
        cur.execute("SELECT slot_number FROM slot WHERE slot_id=%s", (slot_id,))
        return cur.fetchone()["slot_number"]
    return tx(f)

def check_out(session_id):
    def f(cur):
        cur.execute("""SELECT s.*, vt.base_multiplier, t.tariff_id, t.base_rate, t.hourly_rate, t.grace_mins,
                       TIMESTAMPDIFF(MINUTE, s.entry_time, NOW()) AS mins
                       FROM parking_session s JOIN vehicle v ON v.vehicle_id=s.vehicle_id
                       JOIN vehicle_type vt JOIN tariff t ON vt.vehicle_type_id=v.vehicle_type_id AND t.vehicle_type_id=v.vehicle_type_id
                       WHERE s.session_id=%s AND s.status='ACTIVE' FOR UPDATE""", (session_id,))
        s = cur.fetchone()
        if not s: raise ValueError("Session not active")
        cur.execute("SELECT 1 FROM pass WHERE vehicle_id=%s AND CURDATE() BETWEEN start_date AND end_date LIMIT 1",
                    (s["vehicle_id"],))
        has_pass = cur.fetchone() is not None
        amount = 0.0 if has_pass else calc_amount(s["mins"], s["base_rate"], s["hourly_rate"],
                                                  s["grace_mins"], s["base_multiplier"])
        cur.execute("UPDATE parking_session SET exit_time=NOW(), status='COMPLETED' WHERE session_id=%s", (session_id,))
        cur.execute("UPDATE slot SET status='AVAILABLE' WHERE slot_id=%s", (s["slot_id"],))
        if s["reservation_id"]:
            cur.execute("UPDATE reservation SET status='COMPLETED' WHERE reservation_id=%s", (s["reservation_id"],))
        cur.execute("INSERT INTO bill (session_id, tariff_id, total_duration_mins, total_amount, status) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (session_id, s["tariff_id"], s["mins"], amount, "PAID" if amount == 0 else "UNPAID"))
        return {"mins": s["mins"], "amount": amount, "pass": has_pass}
    return tx(f)

def reserve(customer_id, vehicle_id, slot_id, start_in_min, hours):
    def f(cur):
        cur.execute("SELECT reservation_id FROM reservation WHERE vehicle_id=%s AND status IN ('PENDING','ACTIVE')", (vehicle_id,))
        if cur.fetchone():
            raise ValueError("This vehicle already has an active or pending reservation")
        cur.execute("SELECT session_id FROM parking_session WHERE vehicle_id=%s AND status='ACTIVE'", (vehicle_id,))
        if cur.fetchone():
            raise ValueError("Vehicle is currently parked inside the facility")
        cur.execute("SELECT status FROM slot WHERE slot_id=%s FOR UPDATE", (slot_id,))
        s_row = cur.fetchone()
        if not s_row or s_row["status"] != "AVAILABLE": raise ValueError("Slot is no longer available")
        # use MySQL's clock so Python/MySQL timezone differences can't cause early expiry
        cur.execute("INSERT INTO reservation (customer_id, vehicle_id, slot_id, start_time, end_time) "
                    "VALUES (%s,%s,%s,NOW() + INTERVAL %s MINUTE, NOW() + INTERVAL %s MINUTE)",
                    (customer_id, vehicle_id, slot_id, start_in_min, start_in_min + hours * 60))
        cur.execute("UPDATE slot SET status='RESERVED' WHERE slot_id=%s", (slot_id,))
    return tx(f)

def pay_bill(bill_id, method, ref):
    def f(cur):
        cur.execute("SELECT total_amount, status FROM bill WHERE bill_id=%s FOR UPDATE", (bill_id,))
        b = cur.fetchone()
        if not b: raise ValueError("Bill not found")
        if b["status"] == "PAID": raise ValueError("Already paid")
        cur.execute("INSERT INTO payment (bill_id, amount, payment_method, transaction_ref) VALUES (%s,%s,%s,%s)",
                    (bill_id, b["total_amount"], method, ref or None))
        cur.execute("UPDATE bill SET status='PAID' WHERE bill_id=%s", (bill_id,))
    return tx(f)

# ---------------------------------------------------------------- pages
@st.fragment(run_every=5)          # re-queries MySQL every 5 s -> live dashboard
def live_dashboard():
    expire_reservations()
    cnt = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM slot GROUP BY status")}
    total = sum(cnt.values()) or 1
    occ = cnt.get("OCCUPIED", 0)
    rev = q("SELECT COALESCE(SUM(amount),0) r FROM payment WHERE DATE(paid_at)=CURDATE()")[0]["r"]
    c = st.columns(5)
    c[0].metric("Total slots", total)
    c[1].metric("Available", cnt.get("AVAILABLE", 0))
    c[2].metric("Occupied", occ)
    c[3].metric("Occupancy", f"{occ * 100 // total}%")
    c[4].metric("Revenue today", f"₹{rev:,.0f}")
    st.caption(f"Live from MySQL · refreshed {datetime.now():%H:%M:%S}")
    slots = q("""SELECT f.floor_number, z.zone_name, s.slot_number, s.status, vt.type_name
                 FROM slot s JOIN zone z USING(zone_id) JOIN floor f USING(floor_id)
                 JOIN vehicle_type vt USING(vehicle_type_id) ORDER BY f.floor_number, z.zone_name, s.slot_id""")
    html, last = "", None
    for s in slots:
        key = (s["floor_number"], s["zone_name"])
        if key != last:
            html += f'<div class="zone-title">FLOOR {key[0]} · ZONE {key[1]}</div>'; last = key
        html += f'<span class="slot {s["status"]}" title="{s["type_name"]}">{s["slot_number"]}</span>'
    st.markdown(html, unsafe_allow_html=True)
    st.markdown("&nbsp;")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Peak-Hour Occupancy")
        hr_data = q("""SELECT HOUR(entry_time) AS hr, COUNT(*) AS n
                       FROM parking_session GROUP BY HOUR(entry_time) ORDER BY hr""")
        hr_map = {r["hr"]: r["n"] for r in hr_data}
        chart_df = pd.DataFrame([{"Hour": f"{h:02d}:00", "Sessions": hr_map.get(h, 0)} for h in range(24)]).set_index("Hour")
        st.bar_chart(chart_df, color="#00F2FE")
    with c2:
        st.subheader("Revenue per Floor")
        floor_data = q("""SELECT CONCAT('Floor ', f.floor_number) AS floor_name,
                                 COALESCE(SUM(b.total_amount), 0) AS revenue
                          FROM floor f
                          LEFT JOIN zone z ON z.floor_id = f.floor_id
                          LEFT JOIN slot sl ON sl.zone_id = z.zone_id
                          LEFT JOIN parking_session ps ON ps.slot_id = sl.slot_id
                          LEFT JOIN bill b ON b.session_id = ps.session_id AND b.status = 'PAID'
                          GROUP BY f.floor_id, f.floor_number
                          ORDER BY f.floor_number""")
        if floor_data:
            floor_df = pd.DataFrame(floor_data).set_index("floor_name")
            st.bar_chart(floor_df, color="#00F2FE")
        else:
            st.caption("No floor revenue data yet.")
    st.markdown("&nbsp;")
    left, right = st.columns(2)
    with left:
        st.subheader("Active sessions")
        st.dataframe(df(q("""SELECT s.session_id, v.license_plate, sl.slot_number, s.entry_time,
                   TIMESTAMPDIFF(MINUTE, s.entry_time, NOW()) AS mins FROM parking_session s
                   JOIN vehicle v USING(vehicle_id) JOIN slot sl USING(slot_id) WHERE s.status='ACTIVE'""")),
                     use_container_width=True, hide_index=True)
    with right:
        st.subheader("Recent payments")
        st.dataframe(df(q("SELECT payment_id, bill_id, amount, payment_method, paid_at FROM payment "
                          "ORDER BY paid_at DESC LIMIT 8")), use_container_width=True, hide_index=True)

def page_gate():
    vehicles = q("SELECT v.vehicle_id, v.license_plate, vt.type_name FROM vehicle v "
                 "JOIN vehicle_type vt USING(vehicle_type_id) ORDER BY v.license_plate")
    a, b = st.columns(2)
    with a:
        st.subheader("Check-in")
        if not vehicles:
            st.info("No vehicles registered in the system.")
        else:
            v = st.selectbox("Vehicle", vehicles, format_func=lambda x: f'{x["license_plate"]} ({x["type_name"]})' if x else "")
            resv = q("SELECT r.reservation_id, sl.slot_number, r.start_time FROM reservation r JOIN slot sl USING(slot_id) "
                     "WHERE r.vehicle_id=%s AND r.status='PENDING'", (v["vehicle_id"],)) if v else []
            r = st.selectbox("Use reservation (optional)", [None] + resv,
                             format_func=lambda x: "Walk-in (auto-allocate)" if x is None else f'{x["slot_number"]} @ {x["start_time"]:%H:%M}')
            if st.button("Check in", type="primary") and v:
                res = check_in(v["vehicle_id"], r["reservation_id"] if r else None)
                if result(res, f"Allocated slot {res[1]}"): st.rerun()
    with b:
        st.subheader("Check-out")
        act = q("SELECT s.session_id, v.license_plate, sl.slot_number FROM parking_session s "
                "JOIN vehicle v USING(vehicle_id) JOIN slot sl USING(slot_id) WHERE s.status='ACTIVE'")
        if not act:
            st.info("No active parking sessions right now.")
        else:
            s = st.selectbox("Active session", act, format_func=lambda x: f'{x["license_plate"]} · {x["slot_number"]}' if x else "")
            if st.button("Check out & bill", type="primary") and s:
                res = check_out(s["session_id"])
                if res[0]:
                    o = res[1]
                    st.success(f'{o["mins"]} min parked → bill ₹{o["amount"]:.2f}' + (" (covered by pass)" if o["pass"] else ""))
                else:
                    st.error(res[1])

def page_reservations():
    user = st.session_state.get("user") or {}
    if user.get("role") == "Customer" and user.get("customer_id"):
        custs = q("SELECT customer_id, full_name FROM customer WHERE customer_id=%s", (user["customer_id"],))
    else:
        custs = q("SELECT customer_id, full_name FROM customer ORDER BY full_name")

    st.subheader("New reservation")
    if not custs:
        st.info("No customer records found.")
        return
    c = st.selectbox("Customer", custs, format_func=lambda x: x["full_name"] if x else "")
    veh = q("SELECT v.vehicle_id, v.license_plate, v.vehicle_type_id, vt.type_name FROM vehicle v "
            "JOIN vehicle_type vt USING(vehicle_type_id) WHERE customer_id=%s", (c["customer_id"],)) if c else []
    if not veh:
        st.warning("Selected customer has no registered vehicles. Please register a vehicle under 'Customers & Passes'.")
        v = None
    else:
        v = st.selectbox("Vehicle", veh, format_func=lambda x: f'{x["license_plate"]} ({x["type_name"]})' if x else "")

    slots = q("SELECT slot_id, slot_number FROM slot WHERE status='AVAILABLE' AND vehicle_type_id=%s",
              (v["vehicle_type_id"],)) if v else []
    if v and not slots:
        st.warning(f"No available slots for vehicle type: {v['type_name']}")
        s = None
    elif v:
        s = st.selectbox("Matching free slot", slots, format_func=lambda x: x["slot_number"] if x else "")
    else:
        s = None

    c1, c2 = st.columns(2)
    start = c1.number_input("Arrive in (minutes)", 0, 1440, 10)
    hrs = c2.number_input("Duration (hours)", 1, 24, 2)
    if st.button("Reserve", type="primary") and v and s:
        if result(reserve(c["customer_id"], v["vehicle_id"], s["slot_id"], start, hrs), "Reserved"): st.rerun()

    st.subheader("All reservations")
    where_clause = f"WHERE r.customer_id={user['customer_id']}" if (user.get("role") == "Customer" and user.get("customer_id")) else ""
    st.dataframe(df(q(f"""SELECT r.reservation_id, cu.full_name, v.license_plate, sl.slot_number, r.start_time, r.end_time, r.status
                 FROM reservation r JOIN customer cu USING(customer_id) JOIN vehicle v USING(vehicle_id)
                 JOIN slot sl USING(slot_id) {where_clause} ORDER BY r.reservation_id DESC""")), use_container_width=True, hide_index=True)

def page_customers():
    user = st.session_state.get("user") or {}
    t1, t2, t3 = st.tabs(["Register customer", "Add vehicle", "Issue pass"])
    types = q("SELECT * FROM vehicle_type")
    if user.get("role") == "Customer" and user.get("customer_id"):
        custs = q("SELECT customer_id, full_name FROM customer WHERE customer_id=%s", (user["customer_id"],))
    else:
        custs = q("SELECT customer_id, full_name FROM customer ORDER BY full_name")

    with t1:
        name, mail, ph = st.text_input("Full name"), st.text_input("Email"), st.text_input("Phone")
        if st.button("Register", type="primary") and name and mail and ph:
            try:
                ex("INSERT INTO customer (full_name,email,phone) VALUES (%s,%s,%s)", (name, mail, ph))
                st.success("Registered customer successfully")
                st.rerun()
            except Exception as e: st.error(f"Failed to register customer: {e}")
    with t2:
        if not custs:
            st.info("Please register a customer first.")
        else:
            c = st.selectbox("Owner", custs, format_func=lambda x: x["full_name"] if x else "", key="vo")
            vt = st.selectbox("Type", types, format_func=lambda x: x["type_name"] if x else "")
            plate = st.text_input("License plate (e.g. TS09AB1234)").upper().strip()
            if st.button("Add vehicle", type="primary") and plate:
                if not validate_license_plate(plate):
                    st.error("Invalid license plate format. Expected format like TS09AB1234 or KA01CD5678.")
                else:
                    try:
                        ex("INSERT INTO vehicle (customer_id,vehicle_type_id,license_plate) VALUES (%s,%s,%s)",
                           (c["customer_id"], vt["vehicle_type_id"], plate))
                        st.success("Vehicle added successfully")
                        st.rerun()
                    except Exception as e: st.error(f"Failed to add vehicle: {e}")
    with t3:
        PRICE = {"DAILY": (1, 100), "MONTHLY": (30, 1500), "ANNUAL": (365, 15000)}
        if not custs:
            st.info("No customers found.")
        else:
            c = st.selectbox("Customer", custs, format_func=lambda x: x["full_name"] if x else "", key="po")
            veh = q("SELECT vehicle_id, license_plate FROM vehicle WHERE customer_id=%s", (c["customer_id"],)) if c else []
            if not veh:
                st.warning("Selected customer has no registered vehicles.")
            else:
                v = st.selectbox("Vehicle", veh, format_func=lambda x: x["license_plate"] if x else "", key="pv")
                pt = st.selectbox("Pass type", list(PRICE))
                st.caption(f"₹{PRICE[pt][1]} · valid {PRICE[pt][0]} day(s) from today")
                if st.button("Issue pass", type="primary") and v:
                    d = date.today()
                    ex("INSERT INTO pass (customer_id,vehicle_id,pass_type,start_date,end_date,amount) VALUES (%s,%s,%s,%s,%s,%s)",
                       (c["customer_id"], v["vehicle_id"], pt, d, d + timedelta(days=PRICE[pt][0] - 1), PRICE[pt][1]))
                    st.success("Pass issued successfully")
                    st.rerun()
    st.subheader("Customers & vehicles")
    where_c = f"WHERE cu.customer_id={user['customer_id']}" if (user.get("role") == "Customer" and user.get("customer_id")) else ""
    st.dataframe(df(q(f"SELECT cu.full_name, cu.phone, v.license_plate, vt.type_name FROM customer cu "
                      f"LEFT JOIN vehicle v USING(customer_id) LEFT JOIN vehicle_type vt USING(vehicle_type_id) {where_c}")),
                 use_container_width=True, hide_index=True)
    st.subheader("Passes")
    where_p = f"WHERE cu.customer_id={user['customer_id']}" if (user.get("role") == "Customer" and user.get("customer_id")) else ""
    st.dataframe(df(q(f"SELECT p.pass_id, cu.full_name, v.license_plate, p.pass_type, p.start_date, p.end_date, p.amount "
                      f"FROM pass p JOIN customer cu USING(customer_id) JOIN vehicle v USING(vehicle_id) {where_p}")),
                 use_container_width=True, hide_index=True)

def page_billing():
    t1, t2, t3 = st.tabs(["Collect Payment", "Digital Bill & Receipt", "All Bills"])
    with t1:
        unpaid = q("""SELECT b.bill_id, v.license_plate, b.total_duration_mins, b.total_amount FROM bill b
                      JOIN parking_session s USING(session_id) JOIN vehicle v USING(vehicle_id)
                      WHERE b.status='UNPAID' ORDER BY b.bill_id DESC""")
        st.subheader("Unpaid bills")
        if not unpaid:
            st.info("No unpaid bills pending.")
        else:
            b = st.selectbox("Bill", unpaid, format_func=lambda x: f'#{x["bill_id"]} · {x["license_plate"]} · ₹{x["total_amount"]}' if x else "")
            c1, c2 = st.columns(2)
            m = c1.selectbox("Method", ["CASH", "CARD", "UPI"])
            ref = c2.text_input("Transaction ref (optional)")
            if st.button("Take payment", type="primary") and b:
                if result(pay_bill(b["bill_id"], m, ref), "Payment recorded"): st.rerun()

    with t2:
        st.subheader("Digital Bill & Printable Receipt")
        all_b = q("""SELECT b.bill_id, v.license_plate, b.total_amount, b.status
                     FROM bill b JOIN parking_session s USING(session_id)
                     JOIN vehicle v USING(vehicle_id) ORDER BY b.bill_id DESC""")
        if not all_b:
            st.info("No bills recorded in the system yet.")
        else:
            sel = st.selectbox("Select Bill to View / Print", all_b,
                               format_func=lambda x: f'Bill #{x["bill_id"]} · {x["license_plate"]} · ₹{x["total_amount"]} [{x["status"]}]' if x else "")
            if sel:
                d_rows = q("""SELECT b.bill_id, b.total_duration_mins, b.total_amount, b.status AS bill_status, b.created_at,
                                     s.session_id, s.entry_time, s.exit_time,
                                     v.license_plate, vt.type_name, vt.base_multiplier,
                                     t.base_rate, t.hourly_rate, t.grace_mins,
                                     sl.slot_number, z.zone_name, f.floor_number, fa.name AS facility_name,
                                     cu.full_name, cu.phone,
                                     p.payment_method, p.transaction_ref, p.paid_at
                              FROM bill b
                              JOIN parking_session s USING(session_id)
                              JOIN vehicle v USING(vehicle_id)
                              JOIN vehicle_type vt USING(vehicle_type_id)
                              JOIN tariff t USING(tariff_id)
                              JOIN slot sl ON sl.slot_id = s.slot_id
                              JOIN zone z USING(zone_id)
                              JOIN floor f USING(floor_id)
                              JOIN facility fa USING(facility_id)
                              JOIN customer cu USING(customer_id)
                              LEFT JOIN payment p USING(bill_id)
                              WHERE b.bill_id=%s""", (sel["bill_id"],))
                if d_rows:
                    d = d_rows[0]
                    is_paid = d["bill_status"] == "PAID"
                    badge_cls = "badge-paid" if is_paid else "badge-unpaid"
                    exit_str = f'{d["exit_time"]:%Y-%m-%d %H:%M:%S}' if d.get("exit_time") else "Active / Pending"
                    paid_str = f'{d["paid_at"]:%Y-%m-%d %H:%M:%S} ({d["payment_method"]})' if d.get("paid_at") else ("Waived / Active Pass" if is_paid and d["total_amount"] == 0 else "Unpaid")
                    ref_str = f'<div class="receipt-row"><span>Payment Ref</span><span>{d["transaction_ref"]}</span></div>' if d.get("transaction_ref") else ""
                    billable_hrs = math.ceil(max(0, d["total_duration_mins"] - d["grace_mins"]) / 60)
                    receipt_html = f"""
<div class="receipt-card" id="printable-bill">
  <div class="receipt-header">
    <div class="receipt-title">🅿️ {d['facility_name']}</div>
    <div style="letter-spacing:0.1em;font-size:12px;color:#94a3b8">OFFICIAL PARKING INVOICE & RECEIPT</div>
    <div style="font-size:11px;color:#64748b;margin-top:4px">Invoice #{d['bill_id']:05d} · Generated {d['created_at']:%Y-%m-%d %H:%M}</div>
  </div>
  <div class="receipt-row"><span>Customer Name</span><span><b>{d['full_name']}</b></span></div>
  <div class="receipt-row"><span>Contact Phone</span><span>{d['phone']}</span></div>
  <div class="receipt-row"><span>Vehicle Plate</span><span><b>{d['license_plate']}</b> ({d['type_name']})</span></div>
  <div class="receipt-row"><span>Slot Allocated</span><span>Floor {d['floor_number']} · Zone {d['zone_name']} · Slot {d['slot_number']}</span></div>
  <div class="receipt-divider"></div>
  <div class="receipt-row"><span>Check-In Time</span><span>{d['entry_time']:%Y-%m-%d %H:%M:%S}</span></div>
  <div class="receipt-row"><span>Check-Out Time</span><span>{exit_str}</span></div>
  <div class="receipt-row"><span>Total Duration</span><span>{d['total_duration_mins']} mins ({billable_hrs} billable hr)</span></div>
  <div class="receipt-row"><span>Tariff Calculation</span><span>₹{d['base_rate']} base + ₹{d['hourly_rate']}/hr (Grace: {d['grace_mins']}m)</span></div>
  <div class="receipt-row"><span>Vehicle Multiplier</span><span>{d['base_multiplier']}x</span></div>
  <div class="receipt-divider"></div>
  <div class="receipt-total"><span>Amount Due</span><span>₹{d['total_amount']:.2f}</span></div>
  <div class="receipt-row"><span>Payment Status</span><span class="receipt-badge {badge_cls}">{d['bill_status']}</span></div>
  <div class="receipt-row"><span>Settlement</span><span>{paid_str}</span></div>
  {ref_str}
</div>
"""
                    st.markdown(receipt_html, unsafe_allow_html=True)
                    if st.button("🖨️ Print Digital Bill", type="primary"):
                        st.components.v1.html("<script>window.print();</script>", height=0)

    with t3:
        st.subheader("All bills")
        st.dataframe(df(q("""SELECT b.bill_id, v.license_plate, b.total_duration_mins, b.total_amount,
                             b.status, b.created_at FROM bill b
                             JOIN parking_session s USING(session_id)
                             JOIN vehicle v USING(vehicle_id)
                             ORDER BY b.bill_id DESC""")), use_container_width=True, hide_index=True)

def page_setup():
    t1, t2, t3 = st.tabs(["Slots", "Tariffs", "Facility structure"])
    with t1:
        slots = q("SELECT slot_id, slot_number, status FROM slot WHERE status IN ('AVAILABLE','MAINTENANCE') ORDER BY slot_number")
        if not slots:
            st.info("No slots available for maintenance toggle.")
        else:
            s = st.selectbox("Slot", slots, format_func=lambda x: f'{x["slot_number"]} ({x["status"]})' if x else "")
            if s and st.button("Toggle maintenance"):
                ex("UPDATE slot SET status=%s WHERE slot_id=%s",
                   ("AVAILABLE" if s["status"] == "MAINTENANCE" else "MAINTENANCE", s["slot_id"]))
                st.rerun()
    with t2:
        for t in q("SELECT t.*, vt.type_name FROM tariff t JOIN vehicle_type vt USING(vehicle_type_id)"):
            a, b, c, d = st.columns([2, 1, 1, 1])
            a.write(f'**{t["type_name"]}**')
            base = b.number_input("Base ₹", value=float(t["base_rate"]), key=f'b{t["tariff_id"]}')
            hr = c.number_input("Hourly ₹", value=float(t["hourly_rate"]), key=f'h{t["tariff_id"]}')
            gr = d.number_input("Grace min", value=int(t["grace_mins"]), key=f'g{t["tariff_id"]}')
            if (base, hr, gr) != (float(t["base_rate"]), float(t["hourly_rate"]), int(t["grace_mins"])):
                ex("UPDATE tariff SET base_rate=%s, hourly_rate=%s, grace_mins=%s WHERE tariff_id=%s", (base, hr, gr, t["tariff_id"]))
                st.toast(f'{t["type_name"]} tariff updated')
    with t3:
        st.dataframe(df(q("""SELECT fa.name AS facility, f.floor_number, z.zone_name, COUNT(s.slot_id) AS slots
                 FROM facility fa JOIN floor f USING(facility_id) JOIN zone z USING(floor_id)
                 LEFT JOIN slot s USING(zone_id) GROUP BY fa.name, f.floor_number, z.zone_name""")),
                     use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- shell & authentication
PAGES = {
    "Dashboard": None,
    "Gate Desk": page_gate,
    "Reservations": page_reservations,
    "Customers & Passes": page_customers,
    "Billing": page_billing,
    "Setup": page_setup,
}
ROLES = {
    "Admin": ["Dashboard", "Gate Desk", "Reservations", "Customers & Passes", "Billing", "Setup"],
    "Attendant": ["Dashboard", "Gate Desk", "Billing"],
    "Customer": ["Dashboard", "Reservations", "Customers & Passes"],
}

if "user" not in st.session_state:
    st.session_state.user = None

def login_screen():
    st.markdown("<div style='text-align:center;padding:24px 0'><h1 style='color:#00F2FE;margin-bottom:0'>🅿️ Smart Parking System</h1><p style='color:#94a3b8'>Please sign in with your role credentials</p></div>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        with st.form("login_form"):
            uname = st.text_input("Username").strip()
            upass = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)
            if submitted:
                if not uname or not upass:
                    st.error("Please provide both username and password.")
                else:
                    user_data = authenticate(uname, upass)
                    if user_data:
                        st.session_state.user = user_data
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")

        with st.expander("🔑 Demo Credentials (Stored in app_user)"):
            st.markdown("""
- **Admin**: `admin` / `admin123` *(Full access)*
- **Attendant**: `attendant` / `attendant123` *(Dashboard, Gate Desk, Billing)*
- **Customer**: `aarav` / `customer123` *(Dashboard, Reservations, Passes)*
""")

try:
    q("SELECT 1")
except Exception as e:
    st.error(f"Cannot reach MySQL: {e}\n\nCheck your .env and that schema.sql has been loaded.")
    st.stop()

if not st.session_state.get("user"):
    login_screen()
    st.stop()
else:
    current_user = st.session_state.user
    user_role = current_user["role"]
    allowed_pages = ROLES.get(user_role, ["Dashboard"])

    with st.sidebar:
        st.markdown("### 🅿️ Smart Parking")
        st.markdown(f"**👤 {current_user['username']}**")
        st.caption(f"Role: **{user_role}**")
        page = st.radio("Navigation", allowed_pages)
        st.markdown("---")
        if st.button("Log out", use_container_width=True):
            st.session_state.user = None
            st.rerun()

    st.title(page)

    if page == "Dashboard":
        live_dashboard()
    else:
        PAGES[page]()
