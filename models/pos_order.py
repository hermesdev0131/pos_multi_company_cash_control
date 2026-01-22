# -*- coding: utf-8 -*-

import logging
from odoo import models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.

    Overrides _order_fields to implement dynamic company switching
    for cash payments based on configured rules.

    Business Rules:
    - POS orders are assigned to a company at creation time (ticket-level, never split)
    - Each ticket belongs to one and only one company
    - First order of the day → ALWAYS fiscal company
    - System evaluates ticket by ticket, tracking running daily totals per POS config
    - While non-fiscal % < target → assign to non-fiscal company
    - Once non-fiscal % >= target → assign to fiscal company
    - Overshooting due to high-value tickets is accepted
    - No retroactive changes, no end-of-day rebalancing
    - Totals calculated from today's orders (not stored counters)
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
        _logger.info("[POS MCC][COMPANY] _order_fields entered for order: %s", ui_order.get('name', 'N/A'))

        # Call super() first to get base vals
        vals = super()._order_fields(ui_order)

        # Guard 1: Empty ui_order
        if not ui_order:
            _logger.info("[POS MCC][COMPANY] Guard: ui_order is empty, skipping company assignment")
            return vals

        # Guard 2: Check if order is a return/refund
        # In Odoo POS, returns have amount_total < 0
        amount_total = ui_order.get('amount_total', 0)
        if amount_total < 0:
            _logger.info("[POS MCC][COMPANY] Guard: Order is a return/refund (amount_total < 0), skipping company assignment")
            return vals

        # Guard 3: Check for pos_session_id
        pos_session_id = ui_order.get('pos_session_id')
        if not pos_session_id:
            _logger.info("[POS MCC][COMPANY] Guard: pos_session_id missing, skipping company assignment")
            return vals

        # Rule lookup: Get pos.config from pos_session_id
        pos_session = self.env['pos.session'].browse(pos_session_id)
        if not pos_session or not pos_session.config_id:
            _logger.info("[POS MCC][COMPANY] Guard: POS session or config not found, skipping company assignment")
            return vals

        pos_config = pos_session.config_id

        # Find active rule for this POS config
        rule = self.env['pos.cash.company.rule'].search([
            ('pos_config_id', '=', pos_config.id),
            ('active', '=', True)
        ], limit=1, order='sequence')

        if not rule:
            _logger.info(
                "[POS MCC][COMPANY] No active cash company rule found for POS config '%s', skipping company assignment",
                pos_config.name
            )
            return vals

        # Cash payment detection: Inspect statement_ids (payments) from ui_order
        statement_ids = ui_order.get('statement_ids', [])
        if not statement_ids:
            _logger.info("[POS MCC][COMPANY] No payment statements found in order, skipping company assignment")
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
            _logger.info(
                "[POS MCC][COMPANY] Order has no cash payments matching rule criteria, skipping company assignment"
            )
            return vals

        # Company decision: Get today's totals before making the decision
        totals = rule._get_today_cash_totals()
        fiscal_total = totals['fiscal']
        non_fiscal_total = totals['non_fiscal']
        total_today = fiscal_total + non_fiscal_total

        # Calculate current non-fiscal ratio
        if total_today == 0.0:
            current_non_fiscal_ratio = 0.0
        else:
            current_non_fiscal_ratio = (non_fiscal_total / total_today) * 100.0

        # Make the decision
        selected_company = rule.decide_company_for_amount(amount_total)

        if not selected_company:
            _logger.warning(
                "[POS MCC][COMPANY] Rule.decide_company_for_amount returned no company, skipping company assignment"
            )
            return vals

        # Inject the company into vals
        vals['company_id'] = selected_company.id

        # Mandatory logging with required format
        _logger.info(
            "[POS MCC][COMPANY] Order: %s | Fiscal Total: %.2f | Non-Fiscal Total: %.2f | "
            "Current Ratio: %.2f%% | Target: %.2f%% | Selected Company: %s (ID: %d) | "
            "Amount: %.2f | Rule: '%s' | POS: '%s'",
            ui_order.get('name', 'N/A'),
            fiscal_total,
            non_fiscal_total,
            current_non_fiscal_ratio,
            rule.target_non_fiscal_percentage,
            selected_company.name,
            selected_company.id,
            amount_total,
            rule.name,
            pos_config.name
        )

        return vals
