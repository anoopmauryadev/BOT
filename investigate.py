import sqlite3
import pprint

def investigate_user(user_id):
    print(f"🔍 Investigating User ID: {user_id}\n")
    conn = sqlite3.connect('data.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Check current credits and membership
    print("--- 👤 USER CREDITS & VIP STATUS ---")
    c.execute('SELECT * FROM user_credits WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    if user:
        print(dict(user))
    else:
        print("User not found in user_credits.")
        
    # 2. Check if they used any redeem codes
    print("\n--- 🎟️ REDEEM CODES USED ---")
    c.execute('SELECT * FROM redeem_codes WHERE used_by = ?', (user_id,))
    codes = c.fetchall()
    if codes:
        for code in codes:
            print(dict(code))
    else:
        print("No redeem codes used.")
        
    # 3. Check payment requests
    print("\n--- 💳 PAYMENT REQUESTS (UPI/USDT) ---")
    c.execute('SELECT * FROM payment_requests WHERE user_id = ? AND status = "approved"', (user_id,))
    payments = c.fetchall()
    if payments:
        for payment in payments:
            print(dict(payment))
    else:
        print("No approved payment requests found.")
        
    conn.close()
    print("\n✅ Investigation complete.")

if __name__ == "__main__":
    investigate_user(5021717459)
