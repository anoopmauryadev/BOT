import sqlite3

def reset_all_credits():
    print("⏳ Sabhi users ke credits reset ho rahe hain...")
    
    # Database connect karein
    conn = sqlite3.connect('data.db')
    c = conn.cursor()
    
    # Sabhi users ke credits 2 (default) set kar do
    # Agar aap chahein toh ise 0 bhi kar sakte hain line mein change karke
    c.execute("UPDATE user_credits SET credits = 2")
    
    # Admin ke credits safe rakhne ke liye ya unlimited karne ke liye (Optional)
    admin_id = 8637412597
    c.execute("UPDATE user_credits SET credits = 999999 WHERE user_id = ?", (admin_id,))
    
    # Changes ko save karein
    conn.commit()
    
    # Check karein kitne users update hue
    c.execute("SELECT COUNT(*) FROM user_credits")
    total = c.fetchone()[0]
    
    conn.close()
    
    print(f"✅ Success! Total {total} users ke credits reset karke 2 kar diye gaye hain.")

if __name__ == "__main__":
    reset_all_credits()
