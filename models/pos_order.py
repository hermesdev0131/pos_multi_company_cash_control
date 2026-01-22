# -*- coding: utf-8 -*-

import logging
from odoo import api, models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def _process_order(self, order, draft, existing_order):
        _logger.info("POS MCC: _process_order entered")

        data = order or {}

        # Call super early to get order values prepared
        result = super()._process_order(order, draft, existing_order)

        # Refunds or drafts should not be modified
        if draft or data.get('is_return'):
            return result

        pos_session_id = data.get('pos_session_id')
        if not pos_session_id:
            return result

        session = self.env['pos.session'].browse(pos_session_id)
        pos_config = session.config_id

        rule = self.env['pos.cash.company.rule'].search([
            ('pos_config_id', '=', pos_config.id),
            ('active', '=', True),
        ], limit=1)

        if not rule:
            return result

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
                ('pos_config_ids', 'in', [pos_config.id])
            ])
            cash_method_ids = set(all_cash_methods.ids) if all_cash_methods else set()

        is_cash = any(
            line[2].get('payment_method_id') in cash_method_ids
            for line in payments if line and len(line) > 2
        )

        if not is_cash:
            return result

        amount_total = data.get('amount_total') or 0.0
        company = rule.decide_company_for_amount(amount_total)

        if company and result:
            order_id = result.get('id')
            if order_id:
                self.browse(order_id).write({
                    'company_id': company.id
                })
                _logger.info(
                    "POS MCC: Order %s assigned to company %s",
                    order_id, company.name
                )

        return result
