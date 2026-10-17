from database import connect_db


def main():
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_health (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute("INSERT INTO app_health () VALUES ()")
        connection.commit()
        print("Connected to MySQL; app_health table is ready.", flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
