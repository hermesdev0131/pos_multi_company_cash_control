# -*- coding: utf-8 -*-

import logging
from odoo import models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.

    Overrides _order_fields to implement dynamic company switching
    for cash payments based on configured rules.
    """
    _inherit = 'pos.order'

    def _order_fields(self, ui_order):
        """
        Override _order_fields to inject fiscal/non-fiscal company assignment
        before order record creation.

        This method is guaranteed to run for every POS order in Odoo 18.

        Args:
            ui_order (dict): Order data from the POS frontend

        Returns:
            dict: Order field values with potentially modified company_id
        """
        _logger.info("=== _order_fields entered for order: %s", ui_order.get('name', 'N/A'))

        # Call super() first to get base vals
        vals = super()._order_fields(ui_order)

        # Guard 1: Empty ui_order
        if not ui_order:
            _logger.info("Guard: ui_order is empty, skipping company assignment")
            return vals

        # Guard 2: Check if order is a return/refund
        # In Odoo POS, returns have amount_total < 0 or a specific flag
        amount_total = ui_order.get('amount_total', 0)
        if amount_total < 0:
            _logger.info("Guard: Order is a return/refund (amount_total < 0), skipping company assignment")
            return vals

        # Guard 3: Check for pos_session_id
        pos_session_id = ui_order.get('pos_session_id')
        if not pos_session_id:
            _logger.info("Guard: pos_session_id missing, skipping company assignment")
            return vals

        # Step 3: Rule lookup
        # Get pos.config from pos_session_id
        pos_session = self.env['pos.session'].browse(pos_session_id)
        if not pos_session or not pos_session.config_id:
            _logger.info("Guard: POS session or config not found, skipping company assignment")
            return vals

        pos_config = pos_session.config_id

        # Find active rule for this POS config
        rule = self.env['pos.cash.company.rule'].search([
            ('pos_config_id', '=', pos_config.id),
            ('active', '=', True)
        ], limit=1, order='sequence')

        if not rule:
            _logger.info("No active cash company rule found for POS config '%s', skipping company assignment", pos_config.name)
            return vals

        # Step 4: Cash payment detection
        # Inspect statement_ids (payments) from ui_order
        statement_ids = ui_order.get('statement_ids', [])
        if not statement_ids:
            _logger.info("No payment statements found in order, skipping company assignment")
            return vals

        # Determine which payment method IDs to check
        if rule.cash_payment_method_ids:
            # Use specific payment methods from the rule
            target_payment_method_ids = set(rule.cash_payment_method_ids.ids)
        else:
            # Use all cash payment methods
            cash_methods = self.env['pos.payment.method'].search([
                ('is_cash_count', '=', True)
            ])
            target_payment_method_ids = set(cash_methods.ids)

        # Check if any payment in the order matches our target cash methods
        has_cash_payment = False
        for statement in statement_ids:
            # statement is typically a tuple: (0, 0, {payment_data})
            # Extract payment data
            if isinstance(statement, (list, tuple)) and len(statement) >= 3:
                payment_data = statement[2]
            elif isinstance(statement, dict):
                payment_data = statement
            else:
                continue

            payment_method_id = payment_data.get('payment_method_id')
            if payment_method_id and payment_method_id in target_payment_method_ids:
                has_cash_payment = True
                break

        if not has_cash_payment:
            _logger.info("Order has no cash payments matching rule criteria, skipping company assignment")
            return vals

        # Step 5: Company decision
        # Call rule.decide_company_for_amount with the order amount
        selected_company = rule.decide_company_for_amount(amount_total)

        if not selected_company:
            _logger.warning("Rule.decide_company_for_amount returned no company, skipping company assignment")
            return vals

        # Inject the company into vals
        vals['company_id'] = selected_company.id

        # Step 6: Logging
        _logger.info(
            "Company selected for order '%s': %s (ID: %d) | Rule: '%s' | Amount: %.2f | POS: '%s'",
            ui_order.get('name', 'N/A'),
            selected_company.name,
            selected_company.id,
            rule.name,
            amount_total,
            pos_config.name
        )

        return vals
