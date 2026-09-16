import re

with open('osintcrack2.py', 'r') as f:
    content = f.read()

# Add helper function
helper_func = """
def is_command_or_menu(message):
    if not message.text:
        return False
    if message.text.startswith('/'):
        bot.process_new_messages([message])
        return True
    
    menu_buttons = [
        "🔢 Num Info", "🆔 Adhar Info", "👨‍👩‍👧‍👦 Family Info", "🚗 Vehicle Info", 
        "💎 VIP Plans", "👤 My Account", "💬 Chat with Developer", "⚙️ Admin Panel"
    ]
    if message.text in menu_buttons:
        bot.process_new_messages([message])
        return True
    return False
"""

content = content.replace("def format_address(address_string):", helper_func + "\ndef format_address(address_string):")

# Replace all occurrences of:
# if message.text and message.text.startswith('/'):
#     bot.process_new_messages([message])
#     return
#
# with:
# if is_command_or_menu(message):
#     return

pattern = r"if message\.text and message\.text\.startswith\('/'\):\s*bot\.process_new_messages\(\[message\]\)\s*return"
content = re.sub(pattern, "if is_command_or_menu(message):\n        return", content)

with open('osintcrack2.py', 'w') as f:
    f.write(content)

import py_compile
py_compile.compile('osintcrack2.py', doraise=True)
print("Fix applied successfully!")
