# -*- coding: utf-8 -*-

import logging
from odoo import api, models

_logger = logging.getLogger(__name__)

# Module-level log to verify file is loaded (this runs when module is imported)
try:
    _logger.warning("POS MCC: ========== Module pos_order.py LOADED ==========")
except Exception as e:
    print(f"POS MCC: Error in module-level logging: {e}")


class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model
    def create_from_ui(self, orders, draft=False):
        """
        Override create_from_ui to apply multi-company cash control logic.
        
        This method is called when orders are created from the POS UI.
        We intercept cash payments and route them to the appropriate company
        based on the configured rules.
        """
        # Log immediately at method entry - this should always appear
        _logger.warning(
            "POS MCC: ===== create_from_ui ENTERED ===== orders=%s, draft=%s",
            len(orders) if orders else 0, draft
        )

        # Defensive copy to avoid side effects
        orders = list(orders) if orders else []

        for order_dict in orders:
            data = order_dict.get('data') or {}

            # Skip draft or refund orders
            if draft or data.get('is_return'):
                _logger.info("POS MCC: Skipping draft/refund order")
                continue

            pos_session_id = data.get('pos_session_id')
            if not pos_session_id:
                _logger.info("POS MCC: No pos_session_id found in order data")
                continue

            try:
                session = self.env['pos.session'].browse(pos_session_id)
                if not session.exists():
                    _logger.warning("POS MCC: Session %s does not exist", pos_session_id)
                    continue
                
                pos_config = session.config_id
                if not pos_config:
                    _logger.warning("POS MCC: No config found for session %s", pos_session_id)
                    continue
                
                _logger.info("POS MCC: Processing order for POS config ID: %s", pos_config.id)
            except Exception as e:
                _logger.error("POS MCC: Error getting session/config: %s", str(e), exc_info=True)
                continue

            # Find active cash control rule
            rule = self.env['pos.cash.company.rule'].search([
                ('pos_config_id', '=', pos_config.id),
                ('active', '=', True),
            ], limit=1, order='sequence')

            if not rule:
                _logger.info("POS MCC: No active rule found for POS config %s", pos_config.id)
                continue

            _logger.info("POS MCC: Found rule: %s (ID: %s)", rule.name, rule.id)

            # Detect cash payments
            payments = data.get('statement_ids') or []
            
            # Get cash payment method IDs from rule, or all cash methods if none specified
            if rule.cash_payment_method_ids:
                cash_method_ids = set(rule.cash_payment_method_ids.ids)
                _logger.info("POS MCC: Using rule-specific cash methods: %s", cash_method_ids)
            else:
                # Rule applies to all cash payment methods in POS config
                all_cash_methods = self.env['pos.payment.method'].search([
                    ('is_cash_count', '=', True),
                    '|',
                    ('pos_config_ids', '=', False),
                    ('pos_config_ids', 'in', [pos_config.id])
                ])
                cash_method_ids = set(all_cash_methods.ids) if all_cash_methods else set()
                _logger.info("POS MCC: Using all cash methods from config: %s", cash_method_ids)

            if not cash_method_ids:
                _logger.warning("POS MCC: No cash payment methods found")
                continue

            # Check if any payment uses a cash method
            is_cash = False
            for payment in payments:
                if not payment or not isinstance(payment, (list, tuple)) or len(payment) < 3:
                    continue
                payment_data = payment[2] if isinstance(payment[2], dict) else {}
                payment_method_id = payment_data.get('payment_method_id')
                if payment_method_id and payment_method_id in cash_method_ids:
                    is_cash = True
                    _logger.info("POS MCC: Cash payment detected - method ID: %s", payment_method_id)
                    break

            if not is_cash:
                _logger.info("POS MCC: No cash payment detected in this order")
                continue

            # Apply decision engine
            amount_total = data.get('amount_total') or 0.0
            _logger.info("POS MCC: Processing cash order with amount: %.2f", amount_total)
            
            try:
                company = rule.decide_company_for_amount(amount_total)
                
                if company:
                    data['company_id'] = company.id
                    _logger.warning(
                        "POS MCC: *** COMPANY ASSIGNED *** Order amount %.2f -> Company: %s (ID: %s) "
                        "for POS config %s",
                        amount_total, company.name, company.id, pos_config.id
                    )
                else:
                    _logger.warning(
                        "POS MCC: Decision engine returned no company for amount %.2f",
                        amount_total
                    )
            except Exception as e:
                _logger.error(
                    "POS MCC: Error in decision engine: %s", str(e), exc_info=True
                )
                continue

        # Call super to proceed with standard order creation
        _logger.info("POS MCC: Calling super().create_from_ui()")
        result = super().create_from_ui(orders, draft=draft)
        _logger.warning("POS MCC: ===== create_from_ui EXITING ===== result: %s", result)
        return result
    
    def test_pos_mcc_module(self):
        """
        Test method to verify the module is loaded and working.
        Call this from Odoo shell or via a button to test.
        """
        _logger.warning("POS MCC: test_pos_mcc_module() called - Module is working!")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'POS MCC Test',
                'message': 'Module is loaded and working! Check logs for "POS MCC" entries.',
                'type': 'success',
                'sticky': False,
            }
        }
