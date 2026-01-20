# -*- coding: utf-8 -*-

from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.
    
    This class implements dynamic company switching for cash payments based on
    configured rules. When a POS order is created from the UI with cash payments,
    the decision engine automatically routes the order to either the fiscal or
    non-fiscal company based on today's routing ratio.
    """
    _inherit = 'pos.order'

    @api.model
    def create_from_ui(self, orders, draft=False):
        """
        Override create_from_ui to apply multi-company cash control logic.
        
        This method intercepts orders created from the POS UI and applies
        the cash control routing rules before the order is saved to the database.
        
        Args:
            orders (list): List of order dictionaries from POS UI. Each dict contains:
                - 'data': Order data including pos_session_id, amount_total, statement_ids, etc.
                - 'to_invoice': Boolean indicating if order should be invoiced
            draft (bool): Whether orders are in draft state (not paid yet)
        
        Returns:
            list: List of order IDs created (from super().create_from_ui)
        
        Logic Flow:
            1. For each order, extract POS session and configuration
            2. Find active cash control rule for the POS config
            3. Detect if order contains cash payments
            4. If cash payment found, apply decision engine to determine target company
            5. Set company_id in order data before calling super()
            6. Preserve standard behavior for non-cash orders and drafts
        """
        # Process each order to apply cash control routing
        for order_dict in orders:
            # Only process paid orders (draft=False means orders are paid)
            # Skip draft orders (draft=True means orders are not yet paid)
            if draft:
                continue
            
            # Extract order data
            order_data = order_dict.get('data', {})
            if not order_data:
                continue
            
            # Get POS session ID from order data
            pos_session_id = order_data.get('pos_session_id')
            if not pos_session_id:
                continue
            
            # Get POS session and configuration
            try:
                session = self.env['pos.session'].browse(pos_session_id)
                if not session.exists():
                    continue
                
                pos_config = session.config_id
                if not pos_config:
                    continue
            except Exception as e:
                _logger.warning(
                    "POS Multi-Company Cash Control: Failed to get POS session/config "
                    "for session_id %s: %s", pos_session_id, str(e)
                )
                continue
            
            # Step 1: Find active cash control rule for this POS configuration
            rule = self.env['pos.cash.company.rule'].search([
                ('pos_config_id', '=', pos_config.id),
                ('active', '=', True)
            ], limit=1, order='sequence')
            
            # If no rule exists, skip this order (fallback to default behavior)
            if not rule:
                continue
            
            # Step 2: Detect cash payments in the order
            # Payments are stored in statement_ids (list of payment dictionaries)
            statement_ids = order_data.get('statement_ids', [])
            if not statement_ids:
                continue
            
            # Check if any payment method is a cash payment method
            has_cash_payment = False
            cash_payment_method_ids = []
            
            # Get cash payment method IDs from the rule
            if rule.cash_payment_method_ids:
                # Rule has specific cash payment methods
                rule_cash_method_ids = rule.cash_payment_method_ids.ids
            else:
                # Rule applies to all cash payment methods in POS config
                # Get all cash payment methods from the POS config
                all_cash_methods = self.env['pos.payment.method'].search([
                    ('is_cash_count', '=', True),
                    '|',
                    ('pos_config_ids', '=', False),
                    ('pos_config_ids', 'in', [pos_config.id])
                ])
                rule_cash_method_ids = all_cash_methods.ids if all_cash_methods else []
            
            # If no cash methods defined, skip
            if not rule_cash_method_ids:
                continue
            
            # Check each payment statement to see if it uses a cash method
            for statement in statement_ids:
                # statement_ids can be in different formats:
                # - List of dictionaries: [{'payment_method_id': 1, 'amount': 100}, ...]
                # - List of tuples: [(0, 0, {'payment_method_id': 1, 'amount': 100}), ...]
                payment_method_id = None
                
                if isinstance(statement, dict):
                    payment_method_id = statement.get('payment_method_id')
                elif isinstance(statement, (list, tuple)) and len(statement) > 2:
                    # Odoo ORM format: (0, 0, {...})
                    payment_data = statement[2] if isinstance(statement[2], dict) else {}
                    payment_method_id = payment_data.get('payment_method_id')
                
                if payment_method_id:
                    # Check if this payment method is in the rule's cash methods
                    if payment_method_id in rule_cash_method_ids:
                        has_cash_payment = True
                        cash_payment_method_ids.append(payment_method_id)
                        break  # Found cash payment, no need to check further
            
            # Step 3: Apply decision engine if cash payment found
            if has_cash_payment:
                # Get order total amount
                order_amount = order_data.get('amount_total', 0.0)
                
                # Call decision engine to determine target company
                try:
                    target_company = rule.decide_company_for_amount(order_amount)
                    
                    if target_company:
                        # Step 4: Force the order's company_id
                        order_data['company_id'] = target_company.id
                        
                        # Logging for debugging and verification
                        _logger.info(
                            "POS Multi-Company Cash Control: "
                            "[POS Config ID: %s] | "
                            "[Order Amount: %.2f] | "
                            "[Cash Payment Methods: %s] | "
                            "[Decision: Assigned to %s (ID: %s)]",
                            pos_config.id,
                            order_amount,
                            cash_payment_method_ids,
                            target_company.name,
                            target_company.id
                        )
                    else:
                        _logger.warning(
                            "POS Multi-Company Cash Control: "
                            "Decision engine returned no company for POS Config ID: %s, "
                            "Order Amount: %.2f",
                            pos_config.id,
                            order_amount
                        )
                except Exception as e:
                    _logger.error(
                        "POS Multi-Company Cash Control: Error in decision engine "
                        "for POS Config ID: %s, Order Amount: %.2f. Error: %s",
                        pos_config.id,
                        order_amount,
                        str(e)
                    )
                    # Continue with default behavior on error
                    continue
        
        # Step 5: Call super() to proceed with standard order creation
        # The modified order_data dictionaries (with company_id set) will be used
        # by the standard Odoo logic to create orders with the correct company
        return super().create_from_ui(orders, draft)
