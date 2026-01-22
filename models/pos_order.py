# -*- coding: utf-8 -*-

import logging
from odoo import api, models

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.

    Overrides create_from_ui to implement dynamic company switching
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

    @api.model
    def create_from_ui(self, orders, draft=False):
        """
        Override create_from_ui to modify company before order creation.

        This method is called when POS orders are sent from the frontend to backend.
        It intercepts the order data and injects the appropriate company_id based on
        configured cash routing rules.

        Args:
            orders (list): List of order dictionaries from POS frontend
            draft (bool): Whether to create draft orders

        Returns:
            list: Result from parent create_from_ui method
        """
        _logger.info("[POS MCC][COMPANY] create_from_ui called with %d orders", len(orders))

        for order_data in orders:
            if 'data' not in order_data:
                continue

            ui_order = order_data['data']
            order_name = ui_order.get('name', 'N/A')

            _logger.info("[POS MCC][COMPANY] Processing order: %s", order_name)

            # Guard 1: Skip returns/refunds (negative amounts)
            amount_total = ui_order.get('amount_total', 0)
            if amount_total < 0:
                _logger.info("[POS MCC][COMPANY] Skipping refund order")
                continue

            # Guard 2: Get session and config
            pos_session_id = ui_order.get('pos_session_id')
            if not pos_session_id:
                _logger.info("[POS MCC][COMPANY] No pos_session_id found")
                continue

            pos_session = self.env['pos.session'].browse(pos_session_id)
            if not pos_session or not pos_session.config_id:
                _logger.info("[POS MCC][COMPANY] POS session or config not found")
                continue

            pos_config = pos_session.config_id

            # Step 3: Find active rule for this POS config
            rule = self.env['pos.cash.company.rule'].search([
                ('pos_config_id', '=', pos_config.id),
                ('active', '=', True)
            ], limit=1, order='sequence')

            if not rule:
                _logger.info("[POS MCC][COMPANY] No active rule found for POS: %s", pos_config.name)
                continue

            # Step 4: Check for cash payment
            statement_ids = ui_order.get('statement_ids', [])
            if not statement_ids:
                _logger.info("[POS MCC][COMPANY] No payment statements found")
                continue

            # Determine which payment method IDs to check
            if rule.cash_payment_method_ids:
                target_payment_method_ids = set(rule.cash_payment_method_ids.ids)
            else:
                cash_methods = self.env['pos.payment.method'].search([
                    ('is_cash_count', '=', True)
                ])
                target_payment_method_ids = set(cash_methods.ids)

            # Check if any payment in the order matches our target cash methods
            has_cash_payment = False
            for statement in statement_ids:
                # statement is typically a tuple: (0, 0, {payment_data})
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
                _logger.info("[POS MCC][COMPANY] No cash payment found in order")
                continue

            # Step 5: Get today's totals and make decision
            totals = rule._get_today_cash_totals()
            fiscal_total = totals['fiscal']
            non_fiscal_total = totals['non_fiscal']
            total_today = fiscal_total + non_fiscal_total

            # Calculate current non-fiscal ratio
            if total_today == 0.0:
                current_non_fiscal_ratio = 0.0
            else:
                current_non_fiscal_ratio = (non_fiscal_total / total_today) * 100.0

            # Make the decision using the rule's logic
            selected_company = rule.decide_company_for_amount(amount_total)

            if selected_company:
                # INJECT COMPANY INTO ORDER DATA
                # This is the critical line that changes which company the order belongs to
                ui_order['company_id'] = selected_company.id

                # Mandatory logging with required format
                _logger.info(
                    "[POS MCC][COMPANY] Order: %s | Fiscal Total: %.2f | Non-Fiscal Total: %.2f | "
                    "Current Ratio: %.2f%% | Target: %.2f%% | Selected Company: %s (ID: %d) | "
                    "Amount: %.2f | Rule: '%s' | POS: '%s'",
                    order_name,
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
            else:
                _logger.warning("[POS MCC][COMPANY] Rule returned no company for order: %s", order_name)

        # Call parent method to actually create the orders with modified company_id
        return super().create_from_ui(orders, draft)
