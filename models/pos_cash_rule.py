# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import ValidationError
from datetime import datetime
from pytz import timezone, UTC
import logging

_logger = logging.getLogger(__name__)


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
    _check_company_auto = True

    name = fields.Char(
        string='Rule Name',
        required=True,
        help='Descriptive name for this routing rule'
    )
    
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        help='Company this rule belongs to'
    )
    
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help='Order in which rules are evaluated'
    )
    
    # NOTE: We use 'is_enabled' instead of 'active' because Odoo's 'active' field
    # has special "magic" behavior that causes records to be archived/hidden when False.
    # This caused issues with inline editing in One2many fields where toggling the
    # field would delete the record instead of just updating it.
    is_enabled = fields.Boolean(
        string='Enabled',
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
    
    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to set company_id from pos_config_id if not provided.
        """
        for vals in vals_list:
            if 'company_id' not in vals and 'pos_config_id' in vals:
                pos_config = self.env['pos.config'].browse(vals['pos_config_id'])
                if pos_config.company_id:
                    vals['company_id'] = pos_config.company_id.id
        return super().create(vals_list)
    
    @api.onchange('pos_config_id')
    def _onchange_pos_config_id(self):
        """
        Set company_id when pos_config_id changes.
        """
        if self.pos_config_id and self.pos_config_id.company_id:
            self.company_id = self.pos_config_id.company_id
    
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

    def _get_user_timezone(self):
        """
        Get the logged-in user's timezone.
        
        CRITICAL: Orders are stored in UTC in the database. This method returns the
        user's timezone to convert order dates from UTC to user timezone for filtering.
        All POS operations (creating orders, filtering, searching) use the same timezone
        (the logged-in user's timezone) to ensure consistency.
        
        Returns:
            tuple: (timezone_object, timezone_name, timezone_source)
        """
        # Get timezone from logged-in user
        user = self.env.user
        if user and user.tz:
            try:
                tz = timezone(user.tz)
                return tz, user.tz, 'user_tz'
            except Exception:
                # If timezone is invalid, fall back to UTC
                pass
        
        # If not set, default to UTC
        # NOTE: User timezone should be configured in User Preferences for accurate filtering
        return UTC, 'UTC', 'default_UTC_user_not_configured'
    
    def _get_today_date_range(self, session=None):
        """
        Get the start and end datetime for "today" in the logged-in user's timezone.
        
        CRITICAL: Uses logged-in user's timezone to ensure consistency across all POS operations.
        All operations (creating orders, filtering, searching) use the same timezone (user's timezone).
        
        Args:
            session (pos.session, optional): POS session record (for logging purposes only).
        
        Returns:
            tuple: (start_datetime, end_datetime) where:
                - start_datetime: Beginning of today (00:00:00) in user's timezone (converted to UTC)
                - end_datetime: Current datetime (now) in user's timezone (converted to UTC)
        
        The datetimes are returned as naive UTC datetimes for use in ORM queries.
        """
        self.ensure_one()
        
        # CRITICAL: Use logged-in user's timezone for all operations
        # This ensures all POS operations (creating orders, filtering, searching) use the same timezone
        tz, tz_name, tz_source = self._get_user_timezone()
        
        # Get current time in user's timezone
        now_tz = datetime.now(tz)
        
        # Start of today in user's timezone (00:00:00)
        start_today = now_tz.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # End of today is current time
        end_today = now_tz
        
        # Convert to UTC for ORM queries (Odoo stores datetimes in UTC)
        # CRITICAL: Odoo datetime fields are stored as naive datetimes in UTC
        # We need to convert to naive datetime (remove timezone info) for proper comparison
        start_utc = start_today.astimezone(UTC).replace(tzinfo=None)
        end_utc = end_today.astimezone(UTC).replace(tzinfo=None)
        
        return (start_utc, end_utc)

    def _get_today_cash_totals(self, session=None):
        """
        Compute today's cash totals for fiscal and non-fiscal companies.
        
        Searches for paid POS orders that:
        - Are in 'paid' state
        - Belong to either fiscal or non-fiscal company
        - Were created today (in logged-in user's timezone)
        - Include cash payment methods specified in the rule
          (or all cash payment methods if rule has no specific methods)
        
        Args:
            session (pos.session, optional): POS session record. Used to determine timezone.
        
        Returns:
            dict: {
                'fiscal': float,      # Total amount for fiscal company
                'non_fiscal': float    # Total amount for non-fiscal company
            }
        """
        self.ensure_one()
        
        # Get today's date range using logged-in user's timezone
        start_datetime, end_datetime = self._get_today_date_range(session=session)
        
        # Get cash payment method IDs to filter by
        # If rule has specific methods, use those; otherwise get all cash methods
        if self.cash_payment_method_ids:
            cash_method_ids = self.cash_payment_method_ids.ids
        else:
            # Get cash payment methods from the POS config
            # In Odoo, pos.config has payment_method_ids (not the other way around)
            config_payment_methods = self.pos_config_id.payment_method_ids
            # Filter for cash methods only
            cash_methods = config_payment_methods.filtered(lambda pm: pm.is_cash_count)
            cash_method_ids = cash_methods.ids if cash_methods else []
        
        # If no cash methods found, return zeros
        if not cash_method_ids:
            return {'fiscal': 0.0, 'non_fiscal': 0.0}
        
        # CRITICAL FIX: Filter cash payments by date BEFORE getting order IDs
        # The previous code was getting ALL cash payments ever, then filtering orders by date
        # This is inefficient and could cause incorrect totals if there are old unpaid orders
        # We need to filter cash payments by the order's date_order to ensure we only get
        # payments from orders created today
        # 
        # IMPORTANT: We need to search in both company contexts to get all cash payments
        # from both fiscal and non-fiscal companies
        # Use self.sudo().with_company().env to get sudo environment with company context
        sudo_env_fiscal = self.sudo().with_company(self.fiscal_company_id).env
        cash_payments_fiscal = sudo_env_fiscal['pos.payment'].search([
            ('payment_method_id', 'in', cash_method_ids),
            ('pos_order_id.date_order', '>=', start_datetime),
            ('pos_order_id.date_order', '<=', end_datetime),
        ])
        sudo_env_non_fiscal = self.sudo().with_company(self.non_fiscal_company_id).env
        cash_payments_non_fiscal = sudo_env_non_fiscal['pos.payment'].search([
            ('payment_method_id', 'in', cash_method_ids),
            ('pos_order_id.date_order', '>=', start_datetime),
            ('pos_order_id.date_order', '<=', end_datetime),
        ])
        # Combine and deduplicate order IDs
        cash_payment_ids = list(set(
            cash_payments_fiscal.mapped('pos_order_id').ids +
            cash_payments_non_fiscal.mapped('pos_order_id').ids
        ))

        if not cash_payment_ids:
            return {'fiscal': 0.0, 'non_fiscal': 0.0}
        
        # CRITICAL: Use with_company() to explicitly switch context for each company
        # This ensures we can query orders from both companies regardless of current session company
        # Even with sudo(), record rules might filter based on company context
        
        # Search for paid orders in today's range for fiscal company
        # Switch to fiscal company context to ensure we can see all fiscal orders
        # Use self.sudo().with_company().env to get sudo environment with company context
        fiscal_env = self.sudo().with_company(self.fiscal_company_id).env
        
        # CRITICAL: Use logged-in user's timezone for all operations
        # This ensures all POS operations (creating orders, filtering, searching) use the same timezone
        tz, tz_name, tz_source = self._get_user_timezone()
        
        # Get today's date in user timezone for filtering
        now_tz = datetime.now(tz)
        today_local_date = now_tz.date()
        
        # First, get orders within the UTC datetime range (for performance)
        fiscal_orders_candidate = fiscal_env['pos.order'].search([
            ('id', 'in', cash_payment_ids),
            ('state', '=', 'paid'),
            ('company_id', '=', self.fiscal_company_id.id),
            ('date_order', '>=', start_datetime),
            ('date_order', '<=', end_datetime),
        ])
        
        # CRITICAL: Filter by DATE in user timezone, not just UTC datetime range
        # This ensures orders that are "today" in user timezone are included,
        # even if they appear as "yesterday" in UTC
        # All orders are converted from UTC to user timezone before filtering
        fiscal_orders = fiscal_env['pos.order']
        for order in fiscal_orders_candidate:
            if order.date_order:
                # Convert order date from UTC to user timezone
                if order.date_order.tzinfo is None:
                    # Naive datetime - assume UTC
                    order_date_utc_aware = UTC.localize(order.date_order)
                else:
                    order_date_utc_aware = order.date_order
                order_date_local = order_date_utc_aware.astimezone(tz)
                order_date_local_date = order_date_local.date()
                
                # Only include if order date matches today's date in user timezone
                if order_date_local_date == today_local_date:
                    fiscal_orders |= order
        
        fiscal_total = sum(fiscal_orders.mapped('amount_total')) if fiscal_orders else 0.0
        
        # Search for paid orders in today's range for non-fiscal company
        # Switch to non-fiscal company context to ensure we can see all non-fiscal orders
        # Use self.sudo().with_company().env to get sudo environment with company context
        non_fiscal_env = self.sudo().with_company(self.non_fiscal_company_id).env
        
        # CRITICAL: Use logged-in user's timezone for all operations
        # This ensures all POS operations (creating orders, filtering, searching) use the same timezone
        tz, tz_name, tz_source = self._get_user_timezone()
        
        # Get today's date in user timezone for filtering
        now_tz = datetime.now(tz)
        today_local_date = now_tz.date()
        
        # First, get orders within the UTC datetime range (for performance)
        non_fiscal_orders_candidate = non_fiscal_env['pos.order'].search([
            ('id', 'in', cash_payment_ids),
            ('state', '=', 'paid'),
            ('company_id', '=', self.non_fiscal_company_id.id),
            ('date_order', '>=', start_datetime),
            ('date_order', '<=', end_datetime),
        ])
        
        # CRITICAL: Filter by DATE in user timezone, not just UTC datetime range
        # This ensures orders that are "today" in user timezone are included,
        # even if they appear as "yesterday" in UTC
        # All orders are converted from UTC to user timezone before filtering
        non_fiscal_orders = non_fiscal_env['pos.order']
        for order in non_fiscal_orders_candidate:
            if order.date_order:
                # Convert order date from UTC to user timezone
                if order.date_order.tzinfo is None:
                    # Naive datetime - assume UTC
                    order_date_utc_aware = UTC.localize(order.date_order)
                else:
                    order_date_utc_aware = order.date_order
                order_date_local = order_date_utc_aware.astimezone(tz)
                order_date_local_date = order_date_local.date()
                
                # Only include if order date matches today's date in user timezone
                if order_date_local_date == today_local_date:
                    non_fiscal_orders |= order
        
        non_fiscal_total = sum(non_fiscal_orders.mapped('amount_total')) if non_fiscal_orders else 0.0

        return {
            'fiscal': float(fiscal_total),
            'non_fiscal': float(non_fiscal_total)
        }

    def decide_company_for_amount(self, order_amount, session=None):
        """
        Decide which company (fiscal or non-fiscal) should receive the cash payment.

        Decision logic (Final Business Rules):
        1. First order of the day → ALWAYS fiscal company
        2. Calculate current non-fiscal ratio = (non_fiscal_total / total) * 100
        3. If ratio < target percentage → route to non-fiscal company
        4. If ratio >= target percentage → route to fiscal company

        This implements ticket-level assignment (never split tickets):
        - Each ticket is assigned to ONE company at creation time
        - Overshooting the target due to high-value tickets is accepted
        - No retroactive changes or end-of-day rebalancing
        - Totals are calculated from today's orders using logged-in user's timezone

        Args:
            order_amount (float): The amount of the order being processed.
                                 Currently not used but available for future logic.
            session (pos.session, optional): POS session record. Used to determine timezone
                                            for date filtering.

        Returns:
            res.company: The company record that should receive this cash payment

        Note:
            This method is called from pos.order.sync_from_ui() override
            to dynamically assign the company before order record creation.
        """
        self.ensure_one()
        
        # Get today's cash totals using logged-in user's timezone
        totals = self._get_today_cash_totals(session=session)
        
        fiscal_total = totals['fiscal']
        non_fiscal_total = totals['non_fiscal']
        total_today = fiscal_total + non_fiscal_total

        # BUSINESS RULE: First order of the day always goes to fiscal company
        if total_today == 0.0:
            return self.fiscal_company_id

        # Calculate current non-fiscal ratio
        current_non_fiscal_ratio = (non_fiscal_total / total_today) * 100.0

        # If current ratio is below target, route to non-fiscal to increase it
        if current_non_fiscal_ratio < self.target_non_fiscal_percentage:
            selected = self.non_fiscal_company_id
        else:
            # Current ratio is at or above target, route to fiscal company
            selected = self.fiscal_company_id

        return selected
