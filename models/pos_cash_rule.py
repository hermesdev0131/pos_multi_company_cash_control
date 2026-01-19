# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class PosCashCompanyRule(models.Model):
    """
    Model to define rules for routing cash payments to fiscal or non-fiscal companies.
    
    This model will later contain logic to determine which company should receive
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
        
        TODO: This constraint will be used in future decision logic
        to prevent routing errors.
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
        
        TODO: This percentage will be used in future decision logic
        to determine routing probability.
        """
        for rule in self:
            if rule.target_non_fiscal_percentage < 0.0 or rule.target_non_fiscal_percentage > 100.0:
                raise ValidationError(
                    'Target non-fiscal percentage must be between 0 and 100.'
                )
