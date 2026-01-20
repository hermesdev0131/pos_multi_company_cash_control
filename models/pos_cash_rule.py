# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import ValidationError
from datetime import datetime
from pytz import timezone, UTC


class PosCashCompanyRule(models.Model):
    """
    Model to define rules for routing cash payments to fiscal or non-fiscal companies.
    
    This model contains logic to determine which company should receive
    a cash payment based on configurable criteria such as payment method, amount,
    percentage thresholds, and POS configuration.
    """
    _name = 'pos.cash.company.rule'
    _description = 'POS Cash Company Routing Rule'
    _order = 'sequence, id'

    name = fields.Char(
        string='Rule Name',
        required=True,
        help='Descriptive name for this routing rule'
    )
    
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help='Order in which rules are evaluated'
    )
    
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Uncheck to disable this rule without deleting it'
    )
    
    pos_config_id = fields.Many2one(
        'pos.config',
        string='POS Configuration',
        required=True,
        ondelete='cascade',
        help='POS configuration this rule applies to'
    )
    
    fiscal_company_id = fields.Many2one(
        'res.company',
        string='Fiscal Company',
        required=True,
        help='Company to use for fiscal cash transactions'
    )
    
    non_fiscal_company_id = fields.Many2one(
        'res.company',
        string='Non-Fiscal Company',
        required=True,
        help='Company to use for non-fiscal cash transactions'
    )
    
    target_non_fiscal_percentage = fields.Float(
        string='Target Non-Fiscal Percentage',
        required=True,
        digits=(16, 2),
        help='Percentage of cash payments to route to non-fiscal company (0-100)'
    )
    
    cash_payment_method_ids = fields.Many2many(
        'pos.payment.method',
        string='Cash Payment Methods',
        help='Payment methods this rule applies to. If empty, applies to all cash payment methods.'
    )

    @api.constrains('fiscal_company_id', 'non_fiscal_company_id')
    def _check_companies_different(self):
        """
        Ensure fiscal and non-fiscal companies are different.
        
        This constraint prevents routing errors in the decision logic.
        """
        for rule in self:
            if rule.fiscal_company_id and rule.non_fiscal_company_id:
                if rule.fiscal_company_id == rule.non_fiscal_company_id:
                    raise ValidationError(
                        'Fiscal company and non-fiscal company cannot be the same.'
                    )

    @api.constrains('target_non_fiscal_percentage')
    def _check_percentage_range(self):
        """
        Ensure percentage is between 0 and 100.
        
        This percentage is used in the decision logic to determine routing probability.
        """
        for rule in self:
            if rule.target_non_fiscal_percentage < 0.0 or rule.target_non_fiscal_percentage > 100.0:
                raise ValidationError(
                    'Target non-fiscal percentage must be between 0 and 100.'
                )

    def _get_today_date_range(self):
        """
        Get the start and end datetime for "today" in the POS company's timezone.
        
        Returns:
            tuple: (start_datetime, end_datetime) where:
                - start_datetime: Beginning of today (00:00:00) in company timezone
                - end_datetime: Current datetime (now) in company timezone
        
        The datetimes are returned as UTC-aware datetime objects for use in ORM queries.
        """
        self.ensure_one()
        
        # Get the company from POS config (fallback to fiscal company if not set)
        company = self.pos_config_id.company_id or self.fiscal_company_id
        if not company:
            # Fallback to user's company if no company found
            company = self.env.company
        
        # Get company timezone or default to UTC
        tz_name = company.partner_id.tz or 'UTC'
        try:
            tz = timezone(tz_name)
        except Exception:
            # Fallback to UTC if timezone is invalid
            tz = UTC
        
        # Get current time in company timezone
        now_tz = datetime.now(tz)
        
        # Start of today in company timezone (00:00:00)
        start_today = now_tz.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # End of today is current time
        end_today = now_tz
        
        # Convert to UTC for ORM queries (Odoo stores datetimes in UTC)
        start_utc = start_today.astimezone(UTC)
        end_utc = end_today.astimezone(UTC)
        
        return (start_utc, end_utc)

    def _get_today_cash_totals(self):
        """
        Compute today's cash totals for fiscal and non-fiscal companies.
        
        Searches for paid POS orders that:
        - Are in 'paid' state
        - Belong to either fiscal or non-fiscal company
        - Were created today (in company timezone)
        - Include cash payment methods specified in the rule
          (or all cash payment methods if rule has no specific methods)
        
        Returns:
            dict: {
                'fiscal': float,      # Total amount for fiscal company
                'non_fiscal': float    # Total amount for non-fiscal company
            }
        """
        self.ensure_one()
        
        # Get today's date range
        start_datetime, end_datetime = self._get_today_date_range()
        
        # Get cash payment method IDs to filter by
        # If rule has specific methods, use those; otherwise get all cash methods
        if self.cash_payment_method_ids:
            cash_method_ids = self.cash_payment_method_ids.ids
        else:
            # Get all cash payment methods from the POS config
            cash_methods = self.env['pos.payment.method'].search([
                ('is_cash_count', '=', True),
                '|',
                ('pos_config_ids', '=', False),
                ('pos_config_ids', 'in', [self.pos_config_id.id])
            ])
            cash_method_ids = cash_methods.ids if cash_methods else []
        
        # If no cash methods found, return zeros
        if not cash_method_ids:
            return {'fiscal': 0.0, 'non_fiscal': 0.0}
        
        # Build domain for orders with cash payments
        # Use a more efficient query by joining with pos.payment
        # Find orders that have at least one payment with a cash method
        cash_payment_ids = self.env['pos.payment'].search([
            ('payment_method_id', 'in', cash_method_ids)
        ]).mapped('pos_order_id').ids
        
        if not cash_payment_ids:
            return {'fiscal': 0.0, 'non_fiscal': 0.0}
        
        # Search for paid orders in today's range for fiscal company
        fiscal_orders = self.env['pos.order'].search([
            ('id', 'in', cash_payment_ids),
            ('state', '=', 'paid'),
            ('company_id', '=', self.fiscal_company_id.id),
            ('date_order', '>=', start_datetime),
            ('date_order', '<=', end_datetime),
        ])
        fiscal_total = sum(fiscal_orders.mapped('amount_total')) if fiscal_orders else 0.0
        
        # Search for paid orders in today's range for non-fiscal company
        non_fiscal_orders = self.env['pos.order'].search([
            ('id', 'in', cash_payment_ids),
            ('state', '=', 'paid'),
            ('company_id', '=', self.non_fiscal_company_id.id),
            ('date_order', '>=', start_datetime),
            ('date_order', '<=', end_datetime),
        ])
        non_fiscal_total = sum(non_fiscal_orders.mapped('amount_total')) if non_fiscal_orders else 0.0
        
        return {
            'fiscal': float(fiscal_total),
            'non_fiscal': float(non_fiscal_total)
        }

    def decide_company_for_amount(self, order_amount):
        """
        Decide which company (fiscal or non-fiscal) should receive the cash payment.
        
        Decision logic:
        1. If no totals exist yet (both are 0) → route to non-fiscal company
        2. Calculate current non-fiscal ratio
        3. If ratio < target percentage → route to non-fiscal company
        4. Otherwise → route to fiscal company
        
        Args:
            order_amount (float): The amount of the order being processed.
                                 Currently not used but available for future logic.
        
        Returns:
            res.company: The company record that should receive this cash payment
        
        Note:
            This method will be called from pos.order.create_from_ui in a future update
            to dynamically assign the company based on today's routing ratio.
        """
        self.ensure_one()
        
        # Get today's cash totals
        totals = self._get_today_cash_totals()
        
        fiscal_total = totals['fiscal']
        non_fiscal_total = totals['non_fiscal']
        total_today = fiscal_total + non_fiscal_total
        
        # If no orders today, route to non-fiscal company
        if total_today == 0.0:
            return self.non_fiscal_company_id
        
        # Calculate current non-fiscal ratio
        current_non_fiscal_ratio = (non_fiscal_total / total_today) * 100.0
        
        # If current ratio is below target, route to non-fiscal to increase it
        if current_non_fiscal_ratio < self.target_non_fiscal_percentage:
            return self.non_fiscal_company_id
        else:
            # Current ratio is at or above target, route to fiscal company
            return self.fiscal_company_id
