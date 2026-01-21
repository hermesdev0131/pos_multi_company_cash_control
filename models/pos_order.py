# -*- coding: utf-8 -*-

import logging
from odoo import api, models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model
    def create_from_ui(self, orders, draft=False):
        _logger.info(
            "POS MCC: create_from_ui called with %s orders (draft=%s)",
            len(orders), draft
        )

        # Defensive copy to avoid side effects
        orders = list(orders)

        for order in orders:
            data = order.get('data') or {}

            # Skip draft or refund orders
            if data.get('is_return') or draft:
                continue

            pos_config_id = data.get('pos_session_id') and \
                self.env['pos.session'].browse(
                    data['pos_session_id']
                ).config_id

            if not pos_config_id:
                continue

            rule = self.env['pos.cash.company.rule'].search([
                ('pos_config_id', '=', pos_config_id.id),
                ('active', '=', True),
            ], limit=1)

            if not rule:
                continue

            # Detect cash payments
            payments = data.get('statement_ids') or []
            
            # Get cash payment method IDs from rule, or all cash methods if none specified
            if rule.cash_payment_method_ids:
                cash_method_ids = set(rule.cash_payment_method_ids.ids)
            else:
                # Rule applies to all cash payment methods in POS config
                all_cash_methods = self.env['pos.payment.method'].search([
                    ('is_cash_count', '=', True),
                    '|',
                    ('pos_config_ids', '=', False),
                    ('pos_config_ids', 'in', [pos_config_id.id])
                ])
                cash_method_ids = set(all_cash_methods.ids) if all_cash_methods else set()

            is_cash = any(
                p[2].get('payment_method_id') in cash_method_ids
                for p in payments if p and len(p) > 2
            )

            if not is_cash:
                continue

            amount_total = data.get('amount_total') or 0.0

            company = rule.decide_company_for_amount(amount_total)

            if company:
                data['company_id'] = company.id
                _logger.info(
                    "POS MCC: Assigned company %s (ID %s) for order amount %.2f",
                    company.name, company.id, amount_total
                )

        return super().create_from_ui(orders, draft=draft)
