#!/usr/bin/env python3
"""
Diagnostic script to check if pos_multi_company_cash_control module is properly loaded.
Run this from Odoo shell or as a standalone check.
"""

import sys
import os

# Add Odoo to path
sys.path.insert(0, '/opt/odoo18/odoo')

print("=" * 60)
print("POS Multi-Company Cash Control - Module Diagnostic")
print("=" * 60)

# Check 1: File exists
pos_order_file = '/opt/odoo18/custom_addons/pos_multi_company_cash_control/models/pos_order.py'
if os.path.exists(pos_order_file):
    print("✓ File exists:", pos_order_file)
    with open(pos_order_file, 'r') as f:
        content = f.read()
        if 'create_from_ui' in content:
            print("✓ Method 'create_from_ui' found in file")
        if 'POS MCC' in content:
            print("✓ Logging statements found in file")
else:
    print("✗ File NOT found:", pos_order_file)

# Check 2: Syntax
print("\nChecking Python syntax...")
try:
    compile(open(pos_order_file).read(), pos_order_file, 'exec')
    print("✓ Python syntax is valid")
except SyntaxError as e:
    print(f"✗ Syntax error: {e}")
    sys.exit(1)

# Check 3: Import chain
print("\nChecking import chain...")
init_files = [
    '/opt/odoo18/custom_addons/pos_multi_company_cash_control/__init__.py',
    '/opt/odoo18/custom_addons/pos_multi_company_cash_control/models/__init__.py',
]

for init_file in init_files:
    if os.path.exists(init_file):
        print(f"✓ Found: {init_file}")
        with open(init_file, 'r') as f:
            content = f.read()
            if 'pos_order' in content or 'models' in content:
                print(f"  → Contains import statement")
    else:
        print(f"✗ Missing: {init_file}")

print("\n" + "=" * 60)
print("IMPORTANT: To verify module is loaded in Odoo:")
print("1. Log into Odoo as admin")
print("2. Go to Apps → Remove 'Apps' filter")
print("3. Search for 'POS Multi-Company Cash Control'")
print("4. If it shows 'Upgrade', click it")
print("5. Restart Odoo: systemctl restart odoo18")
print("6. Process a POS order with cash payment")
print("7. Check logs: grep -i 'POS MCC' /var/log/odoo18/odoo.log")
print("=" * 60)
