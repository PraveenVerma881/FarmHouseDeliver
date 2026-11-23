# Run the Fresh Produce API locally

This guide starts the FastAPI service and its planning worker against PostgreSQL with PostGIS. The API needs a database; it does not start with an in-memory/demo database.

## 1. Prerequisites

- Python 3.11 or newer
- PostgreSQL with the PostGIS extension installed
- `psql`

On Ubuntu, install the server, client, and PostGIS package with:

```bash
sudo apt update
sudo apt install postgresql postgresql-client postgresql-18-postgis-3
```

The package version above matches Ubuntu 26.04. On another Ubuntu release,
check its PostgreSQL major version with `apt-cache policy postgresql` and install
the matching `postgresql-<version>-postgis-3` package. Confirm the server is
running with `pg_lsclusters` (start it with `sudo pg_ctlcluster <version> main
start` if needed), and verify the client with `psql --version`.

Create a database and user if you do not already have them. For example, from a PostgreSQL administrator session:

```sql
CREATE USER produce WITH PASSWORD 'change-me';
CREATE DATABASE produce OWNER produce;
```

Connect to the new database as a user allowed to install PostGIS, then enable the extension:

```sql
CREATE EXTENSION postgis;
```

## 2. Create the schema

From the project directory, apply the checked-in base schema and the platform operations migration:

```bash
psql "postgresql://produce:change-me@localhost:5432/produce" -f schema.sql
psql "postgresql://produce:change-me@localhost:5432/produce" -f migrations/003_platform_operations.sql
psql "postgresql://produce:change-me@localhost:5432/produce" -f migrations/004_frontend_accounts.sql
psql "postgresql://produce:change-me@localhost:5432/produce" -f migrations/005_customer_address_and_flexible_delivery.sql
psql "postgresql://produce:change-me@localhost:5432/produce" -f migrations/006_automatic_fulfillment.sql
psql "postgresql://produce:change-me@localhost:5432/produce" -f migrations/007_vendor_role_constraint.sql
```

`schema.sql` is the current full base schema, including the route-planning and pay/allocation tables. For a database created from an older version of this project, follow the migration sequence in [README.md](README.md), including migrations 001 and 002 as applicable.

If the database already exists, apply the address/order update after loading the environment:

```bash
set -a
source .env
set +a
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_customer_address_and_flexible_delivery.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/006_automatic_fulfillment.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_vendor_role_constraint.sql
```

## 3. Configure and install

Create a virtual environment, install dependencies, and configure the connection settings:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
test -f .env || cp .env.example .env
```

Edit `.env` and set `DATABASE_URL` and a private `API_KEY`. Load those values in the current shell before starting the app:

```bash
set -a
source .env
set +a
```

The API reads environment variables directly; it does not automatically load `.env` files. The local session key stays stable across `uvicorn --reload`; use a private `SESSION_SECRET` in production.

## 4. Start the app and set up its first owner

Start the API and planning worker in separate terminals:

```bash
uvicorn app.main:app --reload
python -m app.worker
```

Open <http://127.0.0.1:8000/ui>. Customers can browse without signing in, then create an account or sign in with email and phone to place an order. In local development, the one-time sign-in code is printed in the API terminal.

Open <http://127.0.0.1:8000/ui/setup> once to create the first platform owner. In production, set a private `OWNER_SETUP_KEY` in the environment before opening setup; the first owner must enter it. After setup, this page redirects to sign-in and cannot create another owner. The operations dashboard can create farms and team accounts. Farmers and vendors can add products, stock quantities, and prices from their dashboard. On checkout, the app reserves offer quantities and sends confirmation requests to the sellers. When every requested quantity is confirmed, the app queues route optimization and offers the completed route to a driver; a decline automatically tries another active driver.

Customers can type a delivery address or share a browser location pin. Address-only orders need an operator-configured `GEOCODER_URL` for automatic route planning; address lookup sends that address to the configured provider. Without one, the order remains visible to operations for address resolution. Configure `ROUTING_BASE_URL` to an OSRM-compatible road matrix server for road-based ETA and route estimates. Without it, the app uses a labelled straight-line development estimate (1.3× distance at 25 km/h). The pay offer uses the configured route rate card, not a trained acceptance model. Set `DRIVER_PAY_BASE_CENTS`, `DRIVER_PAY_PER_KM_CENTS`, and `DRIVER_PAY_PER_MINUTE_CENTS` for your market.

`GEOCODER_URL` should accept GET query parameters `q`, `format=json`, and `limit=1`, and return a first result with `lat`/`lon` (or GeoJSON `features[0].geometry.coordinates`). A URL containing `{address}` is also supported. Select a provider you trust before enabling address lookup. `ROUTING_BASE_URL` is the base URL of an OSRM-compatible service; its table response supplies duration and distance matrices for route planning.

For a database created from an older schema, apply migrations 005 and 006 after migrations 003 and 004.

For local development, the one-time code is printed in the API terminal. When both providers are configured, the same short-lived code is sent to the registered email and phone. For real delivery, set `APP_ENV=production`, generate a separate `SESSION_SECRET` with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`, and configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, optional `SMTP_USERNAME`/`SMTP_PASSWORD`, plus `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM`. Phone numbers must use international format, e.g. `+911234567890`. Production also requires HTTPS so the session cookie is secure. Do not use development console codes for a public deployment.

## 5. Check service health

The API and worker should still be running in the two terminals started above. Open <http://127.0.0.1:8000/docs> for the interactive API, or check database health:

```bash
curl http://127.0.0.1:8000/health
```

The worker is required to process submitted planning jobs. The customer storefront is at <http://127.0.0.1:8000/ui>.

## 6. Try the platform operations endpoints

Set the API key in your shell (replace the example value):

```bash
export API_KEY='replace-with-your-configured-key'
```

Generate reproducible demo farms and offers (products must exist first):

```bash
curl -X POST "http://127.0.0.1:8000/v1/operations/simulations?farms=5&offers_per_farm=3&seed=42" \
  -H "X-API-Key: $API_KEY"
```

Read platform demand and logistics summaries:

```bash
curl "http://127.0.0.1:8000/v1/operations/insights?days=28" \
  -H "X-API-Key: $API_KEY"
```

The simulation route creates data; call it again to create another batch. The insights demand value is a historical baseline, not a trained seasonal forecast. Quality-check, order lifecycle, and photo routes are documented in `/docs` and in [README.md](README.md).

## Troubleshooting

- **Database connection refused:** confirm PostgreSQL is running, the database exists, and `DATABASE_URL` uses the right host/port/user.
- **PostGIS extension missing:** install the PostGIS package for your PostgreSQL version and run `CREATE EXTENSION postgis;` in the target database.
- **API says `API_KEY is not configured`:** load `.env` in that terminal; `.env` is not loaded automatically.
- **Simulator says to add active products:** run the sample `INSERT` above or add products through your database process.
- **Photo form route fails to parse:** reinstall dependencies from `requirements.txt`; it includes `python-multipart`.
