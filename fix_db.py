import sqlite3

def fix_database():
    print("Fixing database columns...")
    conn = sqlite3.connect('data.db')
    c = conn.cursor()

    # Dictionary of table names and their expected columns with types
    expected_schema = {
        'user_credits': [
            ('credits', 'INTEGER DEFAULT 0'),
            ('free_credits_given', 'INTEGER DEFAULT 0'),
            ('membership_type', "TEXT DEFAULT 'none'"),
            ('membership_expiry', 'TIMESTAMP'),
            ('total_searches', 'INTEGER DEFAULT 0')
        ],
        'banned_users': [
            ('reason', "TEXT DEFAULT 'No reason provided'")
        ]
    }

    for table, columns in expected_schema.items():
        try:
            c.execute(f"PRAGMA table_info({table})")
            existing_columns = [row[1] for row in c.fetchall()]
            
            for col_name, col_type in columns:
                if col_name not in existing_columns:
                    try:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                        print(f"✅ Added missing column '{col_name}' to '{table}'")
                    except Exception as e:
                        print(f"⚠️ Could not add column '{col_name}': {e}")
        except Exception as e:
            print(f"Error checking table {table}: {e}")

    conn.commit()
    conn.close()
    print("Database fix completed! You can now start the bot.")

if __name__ == "__main__":
    fix_database()
