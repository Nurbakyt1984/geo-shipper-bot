import os
from typing import Optional
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS repair_shops (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            phone TEXT NOT NULL,
            speaks_language TEXT NOT NULL DEFAULT 'English',
            service_type TEXT NOT NULL,
            comment TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_shops (
            id SERIAL PRIMARY KEY,
            submitted_by_id BIGINT NOT NULL,
            submitted_by_name TEXT NOT NULL,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            phone TEXT NOT NULL,
            speaks_language TEXT NOT NULL DEFAULT 'English',
            service_type TEXT NOT NULL,
            comment TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            submitted_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            shop_id INTEGER NOT NULL REFERENCES repair_shops(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            user_name TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment TEXT DEFAULT '',
            submitted_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TIMESTAMP DEFAULT NOW(),
            last_seen TIMESTAMP DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def maps_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"


# ─── User tracking ────────────────────────────────────────────────────────────

def upsert_user(user_id: int, username: str, full_name: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bot_users (user_id, username, full_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                last_seen = NOW()
    """, (user_id, username, full_name))
    conn.commit()
    cur.close()
    conn.close()


def get_all_user_ids() -> list[int]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM bot_users")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


# ─── Approved shops ───────────────────────────────────────────────────────────

def add_shop(name: str, latitude: float, longitude: float,
             phone: str, speaks_language: str, service_type: str, comment: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO repair_shops (name, latitude, longitude, phone, speaks_language, service_type, comment)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (name, latitude, longitude, phone, speaks_language, service_type, comment))
    shop_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return shop_id


def remove_shop(shop_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM repair_shops WHERE id = %s", (shop_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return deleted


def get_all_shops():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, latitude, longitude, phone, speaks_language, service_type, comment
        FROM repair_shops ORDER BY id
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [_shop_row(r) for r in rows]


def get_shop_by_id(shop_id: int) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, latitude, longitude, phone, speaks_language, service_type, comment
        FROM repair_shops WHERE id = %s
    """, (shop_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return _shop_row(row) if row else None


def find_shops_within_radius(user_lat: float, user_lng: float, radius_miles: float = 100.0):
    from math import radians, sin, cos, sqrt, atan2

    def haversine(lat1, lng1, lat2, lng2):
        R = 3958.8
        dlat = radians(lat2 - lat1)
        dlng = radians(lng2 - lng1)
        a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c

    shops = get_all_shops()
    results = []
    for shop in shops:
        dist = haversine(user_lat, user_lng, shop["latitude"], shop["longitude"])
        if dist <= radius_miles:
            shop["distance_miles"] = round(dist, 1)
            results.append(shop)
    results.sort(key=lambda x: x["distance_miles"])
    return results


def _shop_row(row) -> dict:
    return {
        "id": row[0], "name": row[1],
        "latitude": row[2], "longitude": row[3],
        "phone": row[4], "speaks_language": row[5],
        "service_type": row[6], "comment": row[7],
    }


# ─── Pending suggestions ──────────────────────────────────────────────────────

def add_pending_shop(submitted_by_id: int, submitted_by_name: str, name: str,
                     latitude: float, longitude: float, phone: str,
                     speaks_language: str, service_type: str, comment: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending_shops
            (submitted_by_id, submitted_by_name, name, latitude, longitude, phone, speaks_language, service_type, comment)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (submitted_by_id, submitted_by_name, name, latitude, longitude,
          phone, speaks_language, service_type, comment))
    pending_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pending_id


def get_pending_shop(pending_id: int) -> Optional[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, submitted_by_id, submitted_by_name, name, latitude, longitude,
               phone, speaks_language, service_type, comment, status, submitted_at
        FROM pending_shops WHERE id = %s
    """, (pending_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return _pending_row(row) if row else None


def get_user_pending_shops(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, submitted_by_id, submitted_by_name, name, latitude, longitude,
               phone, speaks_language, service_type, comment, status, submitted_at
        FROM pending_shops WHERE submitted_by_id = %s ORDER BY submitted_at DESC
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [_pending_row(r) for r in rows]


def approve_pending_shop(pending_id: int) -> Optional[int]:
    shop = get_pending_shop(pending_id)
    if not shop or shop["status"] != "pending":
        return None
    new_id = add_shop(
        name=shop["name"], latitude=shop["latitude"], longitude=shop["longitude"],
        phone=shop["phone"], speaks_language=shop["speaks_language"],
        service_type=shop["service_type"], comment=shop["comment"],
    )
    set_pending_status(pending_id, "approved")
    return new_id


def reject_pending_shop(pending_id: int) -> bool:
    shop = get_pending_shop(pending_id)
    if not shop or shop["status"] != "pending":
        return False
    set_pending_status(pending_id, "rejected")
    return True


def set_pending_status(pending_id: int, status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE pending_shops SET status = %s WHERE id = %s", (status, pending_id))
    conn.commit()
    cur.close()
    conn.close()


def _pending_row(row) -> dict:
    return {
        "id": row[0], "submitted_by_id": row[1], "submitted_by_name": row[2],
        "name": row[3], "latitude": row[4], "longitude": row[5],
        "phone": row[6], "speaks_language": row[7], "service_type": row[8],
        "comment": row[9], "status": row[10], "submitted_at": str(row[11]),
    }


# ─── Reviews ──────────────────────────────────────────────────────────────────

def add_review(shop_id: int, user_id: int, user_name: str, rating: int, comment: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reviews (shop_id, user_id, user_name, rating, comment)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (shop_id, user_id, user_name, rating, comment))
    review_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return review_id


def get_shop_reviews(shop_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, shop_id, user_id, user_name, rating, comment, submitted_at
        FROM reviews WHERE shop_id = %s ORDER BY submitted_at DESC
    """, (shop_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [_review_row(r) for r in rows]


def get_shop_avg_rating(shop_id: int) -> Optional[float]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT AVG(rating), COUNT(*) FROM reviews WHERE shop_id = %s", (shop_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[1] > 0:
        return round(float(row[0]), 1)
    return None


def user_has_reviewed(shop_id: int, user_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM reviews WHERE shop_id = %s AND user_id = %s", (shop_id, user_id))
    result = cur.fetchone() is not None
    cur.close()
    conn.close()
    return result


def _review_row(row) -> dict:
    return {
        "id": row[0], "shop_id": row[1], "user_id": row[2],
        "user_name": row[3], "rating": row[4], "comment": row[5],
        "submitted_at": str(row[6]),
    }
