# Smart Parking Lot Allocation & Billing System (prototype)

Streamlit + MySQL. Matches the DBMS Review 1 deck: 13 tables, 1:1 slot lock,
tariff x vehicle multiplier billing with grace period, 15-min reservation buffer, passes, 3 roles.

## 1. Run it
```bash
mysql -u root -p < schema.sql          # creates parking_db + seed data
mysql -u root -p -e "CREATE USER 'parking_app'@'localhost' IDENTIFIED BY 'change_me';
                     GRANT SELECT,INSERT,UPDATE,DELETE ON parking_db.* TO 'parking_app'@'localhost';"
cp .env.example .env                   # put the same password in .env
pip install -r requirements.txt
streamlit run app.py
```

## 2. Two different kinds of "live"
| What | How |
|---|---|
| **The app stays live** (slots, revenue, sessions update by themselves) | `app.py` talks to MySQL directly and the dashboard re-queries every 5 s (`st.fragment(run_every=5)`). Nothing to configure in Antigravity. |
| **Antigravity's agent sees your real database** (so it writes correct SQL, checks data, fixes bugs) | Connect a MySQL **MCP server** (below). |

## 3. Connect MySQL to Antigravity (MCP)
1. In the Agent panel click the **"..."** menu -> **MCP Servers** -> **Manage MCP Servers** -> **View raw config** (opens `mcp_config.json`).
2. Create a **separate, read-only** MySQL user for the agent:
```sql
CREATE USER 'agent_ro'@'localhost' IDENTIFIED BY 'another_password';
GRANT SELECT ON parking_db.* TO 'agent_ro'@'localhost';
```
3. Add this (needs Node.js installed):
```json
{
  "mcpServers": {
    "parking-mysql": {
      "command": "npx",
      "args": ["-y", "@benborla29/mcp-server-mysql"],
      "env": {
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "agent_ro",
        "MYSQL_PASS": "another_password",
        "MYSQL_DB": "parking_db"
      }
    }
  }
}
```
4. Save, click **Refresh** in Manage MCP Servers, then ask the agent: *"List the tables in parking_db and show current slot occupancy."*

Notes
- Package name / env var names belong to a community server - confirm on its npm page; any MySQL MCP server works the same way.
- Keep the agent user read-only. For schema changes, tell the agent to edit `schema.sql` / add migration files and you run them.
- If the server shows unhealthy: check Node is on PATH, MySQL is running, and the JSON is valid.

## 4. Default Login Credentials
Stored with SHA-256 password hashing in `app_user`:

| Role | Username | Password | Accessible Views |
|---|---|---|---|
| **Admin** | `admin` | `admin123` | Dashboard, Gate Desk, Reservations, Customers & Passes, Billing, Setup |
| **Attendant** | `attendant` | `attendant123` | Dashboard, Gate Desk, Billing |
| **Customer** | `aarav` | `customer123` | Dashboard, Reservations, Customers & Passes |
| **Customer** | `diya` | `customer123` | Dashboard, Reservations, Customers & Passes |
| **Customer** | `kabir` | `customer123` | Dashboard, Reservations, Customers & Passes |

## 5. Key System Features
1. **Real Role-Based Authentication**: Secure login screen authenticating against `app_user` using SHA-256 password hashing, replacing the sidebar radio role switcher.
2. **Digital Printable Bill**: In Billing, select any bill to view a digital itemized receipt with tariff breakdown, and click `Print Digital Bill` to print.
3. **Analytics Dashboard**: Real-time Peak-Hour Occupancy chart and Revenue-per-Floor chart rendered dynamically from MySQL data alongside the live slot matrix.
4. **Validation & Integrity**:
   - License plate format validation (e.g. `TS09AB1234`, `KA01CD5678`).
   - Duplicate active/pending reservation prevention for vehicles.
   - Guard against duplicate check-in (enforced at both application and MySQL UNIQUE constraint level).
5. **Background Auto-Expiry**: A recurring MySQL EVENT `evt_expire_reservations` scheduled every 1 minute automatically cancels expired PENDING reservations and frees slots even when no user has the dashboard open.

## Schema notes
Your slides differ slightly (ERD lists base_hours/base_amount/tax/discount; data dictionary lists Base_Rate/Hourly_Rate/Grace_Mins).
This prototype follows the data dictionary. Billing: free within grace, else `(base + hourly x ceil(billable hrs)) x multiplier`; an active pass makes it Rs 0.
