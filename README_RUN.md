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
```

`schema.sql` is the current full base schema, including the route-planning and pay/allocation tables. For a database created from an older version of this project, follow the migration sequence in [README.md](README.md), including migrations 001 and 002 as applicable.

Add a few catalog products so the farm search and demo simulator have produce to work with:

```sql
INSERT INTO products (name, category, default_unit, max_transit_minutes)
VALUES
  ('Tomato', 'vegetable', 'kg', 240),
  ('Spinach', 'leafy green', 'kg', 120),
  ('Apple', 'fruit', 'kg', 720)
ON CONFLICT (name) DO NOTHING;
```

## 3. Configure and install

Create a virtual environment, install dependencies, and configure the connection settings:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `DATABASE_URL` and a private `API_KEY`. Load those values in the current shell before starting the app:

```bash
set -a
source .env
set +a
```

The API reads environment variables directly; it does not automatically load `.env` files.

## 4. Start the API and worker

Open two terminals in the project directory. Activate the virtual environment and load `.env` in each terminal.

Terminal 1, API:

```bash
uvicorn app.main:app --reload
```

Terminal 2, asynchronous route-planning worker:

```bash
python -m app.worker
```

Open `http://127.0.0.1:8000/docs` for the interactive API, or check database health:

```bash
curl http://127.0.0.1:8000/health
```

The worker is required to process submitted planning jobs. Other API routes can be explored while it is stopped.

## 5. Try the platform operations endpoints

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
