# -*- coding: utf-8 -*-

from odoo import models


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.
    
    This class will later override methods to implement dynamic company switching
    for cash payments based on configured rules.
    """
    _inherit = 'pos.order'

    # TODO: Override create_from_ui method to implement:
    # - Cash payment detection
    # - Rule evaluation logic
    # - Dynamic company assignment
    # - Non-fiscal company switching for cash payments
    #
    # Example structure:
    # @api.model
    # def create_from_ui(self, orders, session_id):
    #     # Existing logic
    #     # Add cash payment routing logic here
    #     pass
    
    # TODO: Future implementation in create_from_ui override:
    # 
    # For each order in orders:
    #     1. Check if order has cash payments:
    #        - Get payment methods from order.payment_ids
    #        - Check if any payment method has is_cash_count = True
    #     
    #     2. If cash payment found:
    #        - Get active rule for the POS config:
    #          rule = env['pos.cash.company.rule'].search([
    #              ('pos_config_id', '=', pos_config_id),
    #              ('active', '=', True)
    #          ], limit=1, order='sequence')
    #        
    #        - If rule exists:
    #          company = rule.decide_company_for_amount(order.amount_total)
    #          order.company_id = company
    #          # Update related records (payments, lines, etc.) to use new company
    #     
    #     3. Continue with normal order creation
    #
    # Note: This logic will be implemented in a future update to avoid
    # modifying POS behavior during initial development phase.
