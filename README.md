# Crate Returns

Internal API for tracking reusable delivery crates issued to riders.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python seed.py
python app.py
```

Serves on `http://localhost:5000`.

## Endpoints

```
POST   /crates
GET    /crates?state=in_yard
GET    /crates/<id>
PATCH  /crates/<id>
DELETE /crates/<id>
GET    /crates/<id>/history

POST   /riders
GET    /riders
GET    /riders/<id>

POST   /movements/issue     {"crate_id": 1, "rider_id": 4}
POST   /movements/return    {"crate_id": 1, "rider_id": 4}
```

## The rule ops cares about

A crate is always in exactly one of three states: `in_yard`, `with_rider`, or
`retired`.
