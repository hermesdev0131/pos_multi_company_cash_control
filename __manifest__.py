# -*- coding: utf-8 -*-

{
    'name': 'POS Multi-Company Cash Control',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Dynamic fiscal/non-fiscal company switching for cash payments in POS #1',
    'description': """
POS Multi-Company Cash Control
===============================

This module enables dynamic fiscal and non-fiscal company switching for cash payments 
in Odoo Point of Sale. It allows businesses to route cash transactions to different 
companies based on configurable rules, supporting scenarios where fiscal and non-fiscal 
operations need to be managed separately within the same POS system.

Key Features:
-------------
* Configurable cash payment routing rules
* Support for fiscal and non-fiscal company assignment
* Integration with POS payment methods
* Conditional receipt generation based on company type

Future Enhancements:
--------------------
* Dynamic company switching logic
* Cash payment method filtering
* Percentage-based routing rules
* Enhanced receipt templates with company-specific QR codes
    """,
    'author': 'Hiroshi, WolfAIX',
    'website': 'https://www.wolfaix.com',
    'depends': [
        'point_of_sale',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rule.xml',
        'views/pos_cash_rule_views.xml',
        'views/pos_config_views.xml',
    ],
    'assets': {
        'point_of_sale.assets': [
            'pos_multi_company_cash_control/static/src/xml/pos_receipt_extension.xml',
        ],
        'web.assets_backend': [
            'pos_multi_company_cash_control/static/src/css/pos_config_cash_rules.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
